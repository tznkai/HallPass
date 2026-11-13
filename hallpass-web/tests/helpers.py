# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared web test environment: a real database, the fake Authentik, a fake OIDC client and a fixed clock."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.requests import Request

from hallpass import registry
from hallpass.auth import Identity
from hallpass.authentik import AuthentikClient
from hallpass.config import Config
from hallpass.db import Database
from hallpass.web import app as web_app
from tests.fake_authentik import FakeAuthentikState, add_user, make_app

ADMIN_SUB = "00000000-0000-4000-8000-000000000001"
BASE_URL = "https://hallpass.test"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
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
}
CSRF_FIELD = re.compile(r'name="csrf" value="([^"]+)"')


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


@dataclass
class Env:
    app: FastAPI
    client: httpx.AsyncClient
    db: Database
    authentik: FakeAuthentikState
    authentik_client: AuthentikClient
    oidc: FakeOidc
    config: Config
    clock: FixedClock

    async def close(self) -> None:
        await self.client.aclose()
        await self.authentik_client.aclose()
        self.db.close()


async def make_env(tmp_path: Path, **overrides: str) -> Env:
    """Builds the app over a real database in tmp_path. Keyword overrides are environment variable names."""
    config = Config.from_env({**ENV, "HALLPASS_DATA_DIR": str(tmp_path / "data"), **overrides})
    state = FakeAuthentikState()
    authentik_client = AuthentikClient(config.authentik_url, config.authentik_token,
                                       transport=httpx.ASGITransport(app=make_app(state)))
    db = Database.open(config.data_dir / "hallpass.db")
    oidc = FakeOidc()
    clock = FixedClock(NOW)
    app = web_app.create_app(web_app.AppDeps(config=config, db=db, authentik=authentik_client, oidc=oidc,
                                             clock=clock.now))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL, follow_redirects=False)
    return Env(app=app, client=client, db=db, authentik=state, authentik_client=authentik_client,
               oidc=oidc, config=config, clock=clock)


async def sign_in(env: Env, *, sub: str, username: str | None = None, enrollment_ref: str | None = None,
                  next_path: str | None = None) -> httpx.Response:
    """Runs /login and the callback as the browser would. Returns the callback response."""
    env.oidc.next_identity = Identity(sub=sub, username=username, enrollment_ref=enrollment_ref)
    params = None if next_path is None else {"next": next_path}
    start = await env.client.get("/login", params=params)
    assert start.status_code == 302, start.status_code
    return await env.client.get(start.headers["location"])


async def admin_client(env: Env) -> httpx.Response:
    """Signs the admin in, adding the admin's Authentik user first if it is missing. Returns the callback response."""
    if not any(user["uuid"] == ADMIN_SUB for user in env.authentik.users.values()):
        add_user(env.authentik, uuid=ADMIN_SUB, username="root")
    return await sign_in(env, sub=ADMIN_SUB, username="root")


async def form(env: Env, page: str, **fields: str) -> dict[str, str]:
    """GETs `page`, reads its hidden csrf field and returns the form data with that token."""
    response = await env.client.get(page)
    match = CSRF_FIELD.search(response.text)
    assert match is not None, f"no csrf field on {page}"
    return {"csrf": match.group(1), **fields}


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
