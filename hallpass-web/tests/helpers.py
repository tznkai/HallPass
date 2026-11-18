# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared web test environment: a real database, the fake Authentik, a fake OIDC client and a fixed clock."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from starlette.requests import Request

from hallpass import grants, registry, services, terms
from hallpass.auth import AuthlibOidc, Identity
from hallpass.authentik import AuthentikClient
from hallpass.config import Config
from hallpass.monitor_api import create_monitor_app
from hallpass.db import Database
from hallpass.registry import Person
from hallpass.web import app as web_app
from tests.fake_authentik import FakeAuthentikState, add_user, enrol, make_app

ADMIN_SUB = "00000000-0000-4000-8000-000000000001"
FLUXER_GROUP = "00000000-0000-4000-8000-0000000000f1"
BASE_URL = "https://hallpass.test"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TERMS_TOS = "Test terms of service text."
TERMS_PRIVACY = "Test privacy disclosure text."
MONITOR_TOKEN = "monitor-token-for-tests-0123456789abcdef"
ENV = {
    "HALLPASS_PUBLIC_URL": BASE_URL,
    "HALLPASS_SESSION_SECRET": "session-secret-for-tests-only-0123456789",
    "HALLPASS_ADMIN_SUBS": ADMIN_SUB,
    "AUTHENTIK_URL": "https://authentik.test",
    "AUTHENTIK_TOKEN": "test-token",
    "AUTHENTIK_INVITE_FLOW": "invite",
    "OIDC_METADATA_URL": "https://authentik.test/application/o/hallpass/.well-known/openid-configuration",
    "OIDC_CLIENT_ID": "hallpass-client",
    "OIDC_CLIENT_SECRET": "oidc-secret-for-tests-only",
    "HALLPASS_ADMIN_EMAIL": "admin@example.com",
    "AUTHENTIK_FLUXER_GROUP": FLUXER_GROUP,
    "HALLPASS_MONITOR_TOKEN": MONITOR_TOKEN,
}
CSRF_FIELD = re.compile(r'name="csrf" value="([^"]+)"')
VERSION_FIELD = re.compile(r'name="version_id" value="(\d+)"')


class FixedClock:
    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = moment


class FakeOidc:
    """Stands in for AuthlibOidc: begin redirects straight back with a fake code, finish returns the queued identity."""

    def __init__(self) -> None:
        self.next_identity: Identity | None = None

    async def begin(self, request: Request, redirect_uri: str):
        return RedirectResponse(redirect_uri + "?code=fake&state=x", 302)

    async def finish(self, request: Request) -> Identity:
        if self.next_identity is None:
            raise RuntimeError("no identity queued for this sign-in")
        return self.next_identity


class StubOidcClient:
    """Stands in for the Authlib client under AuthlibOidc. Each call raises its error, or returns its token. Used to
    run the real translation in AuthlibOidc against IdP failures."""

    def __init__(self, *, redirect_error: Exception | None = None, token: object = None,
                 token_error: Exception | None = None) -> None:
        self._redirect_error = redirect_error
        self._token = token
        self._token_error = token_error

    async def authorize_redirect(self, request, redirect_uri: str):
        if self._redirect_error is not None:
            raise self._redirect_error
        return RedirectResponse(redirect_uri + "?code=stub&state=x", 302)

    async def authorize_access_token(self, request):
        if self._token_error is not None:
            raise self._token_error
        return self._token


def use_oidc_client(env: Env, client: StubOidcClient) -> None:
    """Puts a real AuthlibOidc, built over the stub client, into the running app's dependencies."""
    oidc = AuthlibOidc(metadata_url=env.config.oidc_metadata_url, client_id=env.config.oidc_client_id,
                       client_secret=env.config.oidc_client_secret)
    oidc._client = client
    env.application.state.deps = dataclasses.replace(env.application.state.deps, oidc=oidc)


class RecordingSender:
    """Stands in for the SMTP sender: records each (to, subject, body) it is asked to send, and never fails."""

    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send(self, *, to_addr: str, subject: str, body: str) -> None:
        self.calls.append((to_addr, subject, body))


@dataclass
class Env:
    application: FastAPI
    client: httpx.AsyncClient
    db: Database
    authentik: FakeAuthentikState
    authentik_client: AuthentikClient
    oidc: FakeOidc
    config: Config
    clock: FixedClock
    sender: RecordingSender
    # The monitor API over its own transport, as hallmonitor reaches it. Requests carry no headers unless a test adds them.
    monitor_client: httpx.AsyncClient

    async def close(self) -> None:
        await self.client.aclose()
        await self.monitor_client.aclose()
        await self.authentik_client.aclose()
        self.db.close()


async def make_env(tmp_path: Path, **overrides: str) -> Env:
    """Builds the app over a real database in tmp_path. Keyword overrides are environment variable names."""
    config = Config.from_env({**ENV, "HALLPASS_DATA_DIR": str(tmp_path / "data"), **overrides})
    state = FakeAuthentikState()
    state.groups[FLUXER_GROUP] = set()
    authentik_client = AuthentikClient(config.authentik_url, config.authentik_token,
                                       transport=httpx.ASGITransport(app=make_app(state)))
    db = Database.open(config.data_dir / "hallpass.db")
    oidc = FakeOidc()
    clock = FixedClock(NOW)
    sender = RecordingSender()
    # The background loops stay off: tests run mail delivery and the resync by calling them directly.
    deps = web_app.AppDeps(config=config, db=db, authentik=authentik_client, oidc=oidc, clock=clock.now,
                           sender=sender, background=False)
    app = web_app.create_app(deps)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL, follow_redirects=False)
    monitor_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_monitor_app(deps)),
                                       base_url="http://monitor.test")
    return Env(application=app, client=client, db=db, authentik=state, authentik_client=authentik_client,
               oidc=oidc, config=config, clock=clock, sender=sender, monitor_client=monitor_client)


def api_routes(app: FastAPI) -> list[APIRoute]:
    """Every APIRoute in the app, with its full path. FastAPI keeps included routers in app.routes as wrappers whose
    original_router holds the routes, with the router's prefix already applied to each path."""
    found: list[APIRoute] = []
    for entry in app.routes:
        original = getattr(entry, "original_router", None)
        if original is not None:
            found.extend(route for route in original.routes if isinstance(route, APIRoute))
        elif isinstance(entry, APIRoute):
            found.append(entry)
    return found


def new_client(env: Env) -> httpx.AsyncClient:
    """A browser of its own: a fresh cookie jar on the same app."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=env.application), base_url=BASE_URL,
                             follow_redirects=False)


async def sign_in(env: Env, *, sub: str, username: str | None = None, enrollment_ref: str | None = None,
                  next_path: str | None = None, client: httpx.AsyncClient | None = None) -> httpx.Response:
    """Runs /login and the callback as the browser would. Returns the callback response."""
    browser = client or env.client
    env.oidc.next_identity = Identity(sub=sub, username=username, enrollment_ref=enrollment_ref)
    params = None if next_path is None else {"next": next_path}
    start = await browser.get("/login", params=params)
    assert start.status_code == 302, start.status_code
    return await browser.get(start.headers["location"])


async def publish_terms(env: Env, *, blocking: bool = True) -> int:
    """Publishes a terms version with fixed texts and returns its id. The actor is the admin when one exists, else an
    unbound publisher, since publishing needs an actor and tests may publish before anyone has signed in."""
    def change(conn) -> int:
        actor = registry.get_by_uuid(conn, ADMIN_SUB) or registry.create_person(
            conn, display_name="Publisher", account_type="adult", now=env.clock.now())
        version = terms.publish(conn, tos_text=TERMS_TOS, privacy_text=TERMS_PRIVACY, blocking=blocking,
                                actor_id=actor.id, now=env.clock.now())
        return version.id

    return await env.db.run(change)


async def accept_current(env: Env, client: httpx.AsyncClient | None = None) -> httpx.Response:
    """Reads /terms as the browser would, then posts the acceptance of the version shown. Returns the POST response."""
    browser = client or env.client
    page = await browser.get("/terms")
    csrf, version = CSRF_FIELD.search(page.text), VERSION_FIELD.search(page.text)
    assert csrf is not None and version is not None, f"terms form missing on /terms ({page.status_code})"
    return await browser.post("/terms/accept", data={"csrf": csrf.group(1), "version_id": version.group(1)})


async def make_service(db: Database, slug: str, kind: str = "forward_auth") -> services.Service:
    """Creates an active service through the store, so identity and entitlement tests can name it. The actor is the
    admin when one exists, else an unbound publisher, as publish_terms does."""
    def change(conn) -> services.Service:
        actor = registry.get_by_uuid(conn, ADMIN_SUB) or registry.create_person(
            conn, display_name="Publisher", account_type="adult", now=NOW)
        return services.create_service(conn, slug=slug, name=slug.title(), kind=kind, adapter="manual",
                                       auto_suspendable=False, url=None, actor_id=actor.id, now=NOW)

    return await db.run(change)


async def accept_for(env: Env, person_id: int) -> None:
    """Records the current version's acceptance directly in the database, for tests that need it in place."""
    def change(conn) -> None:
        version = terms.current(conn)
        assert version is not None, "no terms version is published"
        grants.accept_terms(conn, person_id, version.id, env.clock.now())

    await env.db.run(change)


async def signed_in_person(env: Env, *, account_type: str = "adult", accepted: bool = True,
                           display_name: str = "Test Person", email: str | None = None) -> tuple[Person, httpx.AsyncClient]:
    """Creates and enrols a person through the store and Authentik fake, and signs them in on a browser of their own.
    With `accepted`, publishes v1 if no version exists and accepts the current version. Returns the person, re-read
    after the status transition, and the browser."""
    person = await store_person(env, display_name=display_name, account_type=account_type, email=email)
    user = enrol(env.authentik, await invite_person(env, person), f"person{person.id}")
    browser = new_client(env)
    await sign_in(env, sub=user["uuid"], username=user["username"],
                  enrollment_ref=user["attributes"]["hallpass_enrollment"], client=browser)
    if accepted and account_type != "child":           # a child never accepts; a linked parent covers them
        if await env.db.run(terms.current) is None:
            await publish_terms(env)
        await accept_current(env, browser)
    return await env.db.run(lambda c: registry.get_person(c, person.id)), browser


async def admin_client(env: Env) -> httpx.Response:
    """Signs the admin in, adding the admin's Authentik user first if it is missing. Publishes v1 when no version
    exists, and accepts the current version. Returns the callback response."""
    if not any(user["uuid"] == ADMIN_SUB for user in env.authentik.users.values()):
        add_user(env.authentik, uuid=ADMIN_SUB, username="root")
    response = await sign_in(env, sub=ADMIN_SUB, username="root")
    if await env.db.run(terms.current) is None:
        await publish_terms(env)
    admin = await env.db.run(lambda c: registry.get_by_uuid(c, ADMIN_SUB))
    state = await env.db.run(lambda c: terms.state_for(c, admin.id))
    if not state.accepted_current:
        await accept_for(env, admin.id)
    return response


async def form(env: Env, page: str, *, client: httpx.AsyncClient | None = None, **fields: str) -> dict[str, str]:
    """GETs `page`, reads its hidden csrf field and returns the form data with that token."""
    response = await (client or env.client).get(page)
    match = CSRF_FIELD.search(response.text)
    assert match is not None, f"no csrf field on {page}"
    return {"csrf": match.group(1), **fields}


def section_of(page: str, child_id: int) -> str:
    """The part of the children page that belongs to one child: from its section to the next child's section."""
    start = page.index(f'data-child="{child_id}"')
    end = page.find('data-child="', start + 1)
    return page[start:] if end == -1 else page[start:end]


async def store_person(env: Env, **fields) -> registry.Person:
    kwargs = {"display_name": "Kid", "account_type": "child", "now": NOW, **fields}
    return await env.db.run(lambda c: registry.create_person(c, **kwargs))


async def invite_person(env: Env, person: registry.Person, *, expires_in: timedelta = timedelta(days=7)) -> str:
    """Creates the Authentik invitation for a person and stores it, as the admin's create action does."""
    created = await env.authentik_client.create_invitation(
        name=f"hallpass-{person.ref}", flow_slug=env.config.authentik_invite_flow,
        enrollment_ref=person.enrollment_ref, expires=NOW + expires_in)
    await env.db.run(lambda c: registry.set_invitation(
        c, person.id, invitation_pk=created.pk, invite_url=created.url, enrollment_ref=person.enrollment_ref))
    return created.pk
