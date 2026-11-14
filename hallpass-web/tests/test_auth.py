# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in resolution over a real database and the fake Authentik, plus sessions, CSRF and the request checks."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import httpx
import pytest
from authlib.integrations.base_client import OAuthError
from authlib.integrations.base_client.errors import MismatchingStateError
from fastapi import Depends, FastAPI, Request
from joserfc.errors import BadSignatureError, ExpiredTokenError, InvalidClaimError

from hallpass import grants, registry, terms
from hallpass.auth import (
    AuthlibOidc,
    Identity,
    RedirectToLogin,
    SignInError,
    add_session_middleware,
    csrf_token,
    current_person,
    end_session,
    require_admin,
    require_csrf,
    resolve_sign_in,
    start_session,
)
from hallpass.authentik import AuthentikClient
from hallpass.config import Config
from hallpass.db import Database
from hallpass.logs import FIELDS_ATTR
from hallpass.registry import Person
from tests.fake_authentik import FakeAuthentikState, add_user, enrol, make_app
from tests.helpers import StubOidcClient

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
ADMIN_SUB = "00000000-0000-4000-8000-000000000001"
KID_SUB = "00000000-0000-4000-8000-0000000000c1"
OTHER_SUB = "00000000-0000-4000-8000-0000000000c2"
UNKNOWN_SUB = "00000000-0000-4000-8000-0000000000c3"
ENV = {
    "HALLPASS_PUBLIC_URL": "https://hallpass.invalid",
    "HALLPASS_SESSION_SECRET": "session-secret-for-tests-only-0123456789",
    "HALLPASS_ADMIN_SUBS": ADMIN_SUB,
    "AUTHENTIK_URL": "https://authentik.invalid",
    "AUTHENTIK_TOKEN": "test-token",
    "AUTHENTIK_INVITE_FLOW": "invite",
    "AUTHENTIK_FLUXER_GROUP": "00000000-0000-4000-8000-0000000000f1",
    "OIDC_METADATA_URL": "https://authentik.invalid/application/o/hallpass/.well-known/openid-configuration",
    "OIDC_CLIENT_ID": "hallpass-client",
    "OIDC_CLIENT_SECRET": "oidc-secret-for-tests-only",
}
BASE = "https://authentik.test"


@pytest.fixture
def config() -> Config:
    return Config.from_env(ENV)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hp.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def state() -> FakeAuthentikState:
    return FakeAuthentikState()


@pytest.fixture
async def authentik(state: FakeAuthentikState) -> AsyncIterator[AuthentikClient]:
    client = AuthentikClient(BASE, ENV["AUTHENTIK_TOKEN"], transport=httpx.ASGITransport(app=make_app(state)))
    try:
        yield client
    finally:
        await client.aclose()


async def make_child(db: Database, **fields) -> Person:
    kwargs = {"display_name": "Kid", "account_type": "child", "now": NOW, **fields}
    return await db.run(lambda c: registry.create_person(c, **kwargs))


async def make_accepted(db: Database, **fields) -> Person:
    """A person who has accepted the current terms, so the page gate lets them through. v1 is published by an unbound
    publisher when no version exists."""
    person = await make_child(db, **fields)

    def accept(conn) -> None:
        version = terms.current(conn)
        if version is None:
            publisher = registry.create_person(conn, display_name="Publisher", account_type="adult", now=NOW)
            version = terms.publish(conn, tos_text="Terms", privacy_text="Privacy", blocking=True,
                                    actor_id=publisher.id, now=NOW)
        grants.accept_terms(conn, person.id, version.id, NOW)

    await db.run(accept)
    return person


async def invite_for(authentik: AuthentikClient, person: Person) -> str:
    created = await authentik.create_invitation(name="hallpass-test", flow_slug="invite",
                                                enrollment_ref=person.enrollment_ref, expires=EXPIRES)
    return created.pk


async def resolve(db, authentik, config, *, sub: str, username: str | None = None,
                  enrollment_ref: str | None = None) -> Person:
    identity = Identity(sub=sub, username=username, enrollment_ref=enrollment_ref)
    return await resolve_sign_in(db, authentik, config, identity, NOW)


async def test_known_uuid_returns_the_bound_person(db, authentik, config):
    person = await make_child(db)
    await db.run(lambda c: registry.bind_identity(c, person.id, uuid=KID_SUB, authentik_pk=7, username="kid1"))
    found = await resolve(db, authentik, config, sub=KID_SUB)
    assert found.id == person.id


async def test_matching_enrollment_ref_binds_the_person_and_clears_the_ref(db, authentik, config, state):
    person = await make_child(db)
    user = enrol(state, await invite_for(authentik, person), "kid1")
    ref = user["attributes"]["hallpass_enrollment"]

    bound = await resolve(db, authentik, config, sub=user["uuid"], username="kid1", enrollment_ref=ref)

    assert bound.id == person.id
    assert bound.authentik_uuid == user["uuid"]
    assert bound.authentik_pk == user["pk"]
    assert bound.enrollment_ref is None
    again = await resolve(db, authentik, config, sub=user["uuid"])
    assert again.id == person.id


async def test_a_second_account_with_the_same_ref_is_not_registered(db, authentik, config, state):
    person = await make_child(db)
    user = enrol(state, await invite_for(authentik, person), "kid1")
    ref = user["attributes"]["hallpass_enrollment"]
    await resolve(db, authentik, config, sub=user["uuid"], username="kid1", enrollment_ref=ref)

    other = add_user(state, uuid=OTHER_SUB, username="kid2", attributes={"hallpass_enrollment": ref})
    with pytest.raises(SignInError) as error:
        await resolve(db, authentik, config, sub=other["uuid"], username="kid2", enrollment_ref=ref)
    assert error.value.reason == "not_registered"


async def test_admin_sub_bootstraps_an_adult_person_once(db, authentik, config, state):
    admin = add_user(state, uuid=ADMIN_SUB, username="root")

    person = await resolve(db, authentik, config, sub=ADMIN_SUB, username="root")
    again = await resolve(db, authentik, config, sub=ADMIN_SUB, username="root")

    assert (person.display_name, person.account_type) == ("root", "adult")
    assert person.authentik_uuid == ADMIN_SUB
    assert person.authentik_pk == admin["pk"]
    assert again.id == person.id
    actions = await db.run(lambda c: [row[0] for row in c.execute("SELECT action FROM decision_log ORDER BY id")])
    assert actions == ["admin_bootstrapped"]


async def test_admin_bootstrap_without_a_username_is_named_administrator(db, authentik, config, state):
    add_user(state, uuid=ADMIN_SUB, username="root")
    person = await resolve(db, authentik, config, sub=ADMIN_SUB, username=None)
    assert person.display_name == "Administrator"


async def test_admin_bootstrap_with_authentik_down_creates_no_person(db, authentik, config, state):
    add_user(state, uuid=ADMIN_SUB, username="root")
    state.fail_next = 500
    with pytest.raises(SignInError) as error:
        await resolve(db, authentik, config, sub=ADMIN_SUB, username="root")
    assert error.value.reason == "unavailable"
    assert await db.run(registry.list_people) == []


async def test_unknown_sub_with_no_ref_is_not_registered(db, authentik, config):
    with pytest.raises(SignInError) as error:
        await resolve(db, authentik, config, sub=UNKNOWN_SUB)
    assert error.value.reason == "not_registered"


async def test_locked_person_is_refused(db, authentik, config):
    person = await make_child(db)
    await db.run(lambda c: registry.bind_identity(c, person.id, uuid=KID_SUB, authentik_pk=7, username="kid1"))
    await db.run(lambda c: registry.set_locked(c, person.id, True))
    with pytest.raises(SignInError) as error:
        await resolve(db, authentik, config, sub=KID_SUB)
    assert error.value.reason == "locked"


async def test_locked_unbound_person_is_refused_and_not_bound(db, authentik, config, state):
    person = await make_child(db)
    user = enrol(state, await invite_for(authentik, person), "kid1")
    ref = user["attributes"]["hallpass_enrollment"]
    await db.run(lambda c: registry.set_locked(c, person.id, True))
    requests_before = len(state.requests)

    with pytest.raises(SignInError) as error:
        await resolve(db, authentik, config, sub=user["uuid"], username="kid1", enrollment_ref=ref)

    assert error.value.reason == "locked"
    assert len(state.requests) == requests_before          # refused before any Authentik call
    stored = await db.run(lambda c: registry.get_person(c, person.id))
    assert stored.authentik_uuid is None
    assert stored.enrollment_ref == ref


async def test_ref_whose_sub_authentik_does_not_know_is_unavailable_and_unbound(db, authentik, config, state):
    person = await make_child(db)
    await invite_for(authentik, person)
    ref = person.enrollment_ref

    with pytest.raises(SignInError) as error:
        await resolve(db, authentik, config, sub=UNKNOWN_SUB, enrollment_ref=ref)

    assert error.value.reason == "unavailable"
    stored = await db.run(lambda c: registry.get_person(c, person.id))
    assert stored.authentik_uuid is None
    assert stored.enrollment_ref == ref


async def test_ref_with_authentik_500_is_unavailable_and_a_retry_succeeds(db, authentik, config, state):
    person = await make_child(db)
    user = enrol(state, await invite_for(authentik, person), "kid1")
    ref = user["attributes"]["hallpass_enrollment"]
    state.fail_next = 500

    with pytest.raises(SignInError) as error:
        await resolve(db, authentik, config, sub=user["uuid"], username="kid1", enrollment_ref=ref)
    assert error.value.reason == "unavailable"
    stored = await db.run(lambda c: registry.get_person(c, person.id))
    assert stored.authentik_uuid is None and stored.enrollment_ref == ref

    bound = await resolve(db, authentik, config, sub=user["uuid"], username="kid1", enrollment_ref=ref)
    assert bound.id == person.id


def test_authlib_oidc_builds_without_contacting_the_provider():
    oidc = AuthlibOidc(metadata_url=ENV["OIDC_METADATA_URL"], client_id=ENV["OIDC_CLIENT_ID"],
                       client_secret=ENV["OIDC_CLIENT_SECRET"])
    assert oidc is not None


# OIDC failures: each becomes a SignInError with a reason. The IdP's text and any URL stay out of the error and the log.

LEAK = "idp-text-with-a-token-abc123-and-a-host-authentik-invalid"
REDIRECT = "https://hallpass.invalid/auth/callback"


def stub_oidc(client: StubOidcClient) -> AuthlibOidc:
    oidc = AuthlibOidc(metadata_url=ENV["OIDC_METADATA_URL"], client_id=ENV["OIDC_CLIENT_ID"],
                       client_secret=ENV["OIDC_CLIENT_SECRET"])
    oidc._client = client
    return oidc


@pytest.mark.parametrize(("call", "client", "reason"), [
    pytest.param("begin", StubOidcClient(redirect_error=httpx.ConnectError(LEAK)), "unavailable",
                 id="begin-unreachable"),
    pytest.param("finish", StubOidcClient(token_error=httpx.ConnectError(LEAK)), "unavailable",
                 id="finish-unreachable"),
    pytest.param("finish", StubOidcClient(token_error=httpx.HTTPStatusError(LEAK, request=None, response=None)),
                 "unavailable", id="finish-non-2xx"),
    pytest.param("finish", StubOidcClient(token_error=OAuthError(error="access_denied", description=LEAK)),
                 "sign_in_failed", id="finish-idp-refused"),
    pytest.param("finish", StubOidcClient(token_error=MismatchingStateError(description=LEAK)),
                 "sign_in_failed", id="finish-replayed-or-stale"),
    pytest.param("begin", StubOidcClient(redirect_error=OAuthError(error="server_error", description=LEAK)),
                 "sign_in_failed", id="begin-oauth-error"),
    pytest.param("finish", StubOidcClient(token_error=InvalidClaimError("iss", LEAK)), "sign_in_failed",
                 id="finish-id-token-claim-refused"),
    pytest.param("finish", StubOidcClient(token_error=ExpiredTokenError("exp", LEAK)), "sign_in_failed",
                 id="finish-id-token-expired"),
    pytest.param("finish", StubOidcClient(token_error=BadSignatureError(LEAK)), "sign_in_failed",
                 id="finish-id-token-bad-signature"),
    pytest.param("finish", StubOidcClient(token={}), "sign_in_failed", id="finish-no-userinfo"),
    pytest.param("finish", StubOidcClient(token={"userinfo": {"preferred_username": LEAK}}), "sign_in_failed",
                 id="finish-no-sub"),
])
async def test_oidc_failures_become_sign_in_errors_without_their_text(caplog, call: str, client, reason: str):
    caplog.set_level(logging.INFO)
    oidc = stub_oidc(client)
    pending = oidc.begin(None, REDIRECT) if call == "begin" else oidc.finish(None)

    with pytest.raises(SignInError) as error:
        await pending

    assert error.value.reason == reason
    assert LEAK not in str(error.value)
    assert error.value.__suppress_context__
    assert LEAK not in caplog.text and "https://" not in caplog.text
    [record] = [r for r in caplog.records if r.name == "hallpass.auth"]
    assert getattr(record, FIELDS_ATTR) == {"outcome": "oidc_failed", "reason": reason}


# Sessions, CSRF and the request checks, through a small app that is not the hallpass app.

def make_test_app(config: Config, db: Database, authentik: AuthentikClient) -> FastAPI:
    app = FastAPI()
    add_session_middleware(app, config)
    app.state.deps = SimpleNamespace(config=config, db=db, authentik=authentik)

    @app.get("/csrf")
    def get_csrf(request: Request) -> dict:
        return {"csrf": csrf_token(request)}

    @app.post("/form", dependencies=[Depends(require_csrf)])
    def post_form() -> dict:
        return {"ok": True}

    @app.get("/start/{person_id}")
    def start(person_id: int, request: Request) -> dict:
        start_session(request, person_id)
        return {"ok": True}

    @app.get("/stop")
    def stop(request: Request) -> dict:
        end_session(request)
        return {"ok": True}

    @app.get("/me")
    async def me(person: Annotated[Person, Depends(current_person)]) -> dict:
        return {"id": person.id}

    @app.get("/admin")
    async def admin(person: Annotated[Person, Depends(require_admin)]) -> dict:
        return {"id": person.id}

    return app


@pytest.fixture
async def http(config, db, authentik) -> AsyncIterator[httpx.AsyncClient]:
    app = make_test_app(config, db, authentik)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hallpass.invalid") as c:
        yield c


async def token_of(http: httpx.AsyncClient) -> str:
    return (await http.get("/csrf")).json()["csrf"]


async def test_form_post_with_the_session_token_is_accepted(http):
    token = await token_of(http)
    assert (await http.post("/form", data={"csrf": token})).status_code == 200


async def test_form_post_with_a_wrong_or_missing_token_is_403(http):
    await token_of(http)
    assert (await http.post("/form", data={"csrf": "wrong"})).status_code == 403
    assert (await http.post("/form", data={})).status_code == 403


async def test_form_post_from_another_origin_is_403_even_with_the_right_token(http):
    token = await token_of(http)
    response = await http.post("/form", data={"csrf": token}, headers={"Origin": "https://evil.invalid"})
    assert response.status_code == 403


async def test_form_post_from_the_public_origin_is_accepted(http):
    token = await token_of(http)
    response = await http.post("/form", data={"csrf": token}, headers={"Origin": "https://hallpass.invalid"})
    assert response.status_code == 200


async def test_starting_a_session_rotates_the_csrf_token(http):
    before = await token_of(http)
    await http.get("/start/1")
    assert await token_of(http) != before


async def test_signed_out_request_is_redirected_to_login(http):
    with pytest.raises(RedirectToLogin):
        await http.get("/me")


async def test_signed_in_then_stopped_request_is_redirected_again(http, db):
    person = await make_accepted(db)
    await http.get(f"/start/{person.id}")
    assert (await http.get("/me")).json() == {"id": person.id}
    await http.get("/stop")
    with pytest.raises(RedirectToLogin):
        await http.get("/me")


async def test_locked_person_gets_403_and_the_session_is_cleared(http, db):
    person = await make_child(db)
    await db.run(lambda c: registry.set_locked(c, person.id, True))
    await http.get(f"/start/{person.id}")
    assert (await http.get("/me")).status_code == 403
    with pytest.raises(RedirectToLogin):
        await http.get("/me")


async def test_admin_check_refuses_a_non_admin_person(http, db):
    person = await make_accepted(db, account_type="adult")
    await db.run(lambda c: registry.bind_identity(c, person.id, uuid=KID_SUB, authentik_pk=7, username="kid1"))
    await http.get(f"/start/{person.id}")
    assert (await http.get("/admin")).status_code == 403


async def test_admin_check_admits_the_admin_person(http, db):
    person = await make_accepted(db, account_type="adult")
    await db.run(lambda c: registry.bind_identity(c, person.id, uuid=ADMIN_SUB, authentik_pk=1, username="root"))
    await http.get(f"/start/{person.id}")
    assert (await http.get("/admin")).json() == {"id": person.id}
