# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""POST /auth/backchannel-logout: a valid logout token ends the sessions it names, and every failure is a 400 that
records nothing. Each request goes through the app's own checks, with fake keys and a fake discovery document."""

from __future__ import annotations

import base64
import logging
from datetime import timedelta

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import OctKey

from hallpass import registry
from hallpass.auth import FORCED_REFRESH_COOLDOWN_SECONDS, SESSION_COOKIE
from hallpass.logs import FIELDS_ATTR
from hallpass.logout import CLOCK_SKEW_SECONDS, MAX_TOKEN_CHARS, ProviderError
from tests.helpers import (
    NOW,
    Env,
    StubOidcClient,
    accept_for,
    new_client,
    publish_terms,
    sign_in,
    store_person,
    use_oidc_client,
)
from tests.logout_tokens import (
    END_SESSION,
    ISSUER,
    SID,
    SUB,
    jwks,
    logout_claims,
    logout_token,
    sign,
    signing_key,
)

SECOND_SID = "sid-test-session-two"
ENDPOINT = "/auth/backchannel-logout"


async def bound_adult(env: Env) -> None:
    """A bound, adult person who has accepted the current terms. Sign-ins for SUB then reach the home page."""
    await publish_terms(env)
    person = await store_person(env, account_type="adult")
    await env.db.run(lambda c: registry.bind_identity(c, person.id, uuid=SUB, authentik_pk=7, username="kid1"))
    await accept_for(env, person.id)


async def browser_signed_in(env: Env, *, sid: str | None) -> httpx.AsyncClient:
    browser = new_client(env)
    response = await sign_in(env, sub=SUB, username="kid1", sid=sid, client=browser)
    assert response.status_code == 303
    return browser


async def is_signed_in(browser: httpx.AsyncClient) -> bool:
    return (await browser.get("/")).status_code == 200


async def deliver(env: Env, token: str):
    return await env.client.post(ENDPOINT, data={"logout_token": token})


async def revocation_rows(env: Env) -> tuple[int, int]:
    def count(conn) -> tuple[int, int]:
        sessions = conn.execute("SELECT count(*) FROM revoked_session").fetchone()[0]
        subjects = conn.execute("SELECT count(*) FROM revoked_subject").fetchone()[0]
        return sessions, subjects

    return await env.db.run(count)


async def test_a_valid_token_ends_only_the_session_it_names(env: Env):
    await bound_adult(env)
    ended = await browser_signed_in(env, sid=SID)
    survivor = await browser_signed_in(env, sid=SECOND_SID)

    response = await deliver(env, logout_token(env.clock.now()))

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert not await is_signed_in(ended)
    assert await is_signed_in(survivor)


async def test_the_next_request_of_the_ended_session_goes_to_sign_in(env: Env):
    await bound_adult(env)
    browser = await browser_signed_in(env, sid=SID)
    await deliver(env, logout_token(env.clock.now()))

    home = await browser.get("/")

    assert home.status_code == 303
    assert home.headers["location"].startswith("/login")


async def test_a_token_with_only_sub_ends_sessions_started_before_it_and_not_later(env: Env):
    await bound_adult(env)
    before = await browser_signed_in(env, sid=SID)
    revoked_at = env.clock.now() + timedelta(seconds=60)
    env.clock.set(revoked_at)

    response = await deliver(env, logout_token(revoked_at, sid=None))
    env.clock.set(revoked_at + timedelta(seconds=30))
    after = await browser_signed_in(env, sid=SECOND_SID)

    assert response.status_code == 200
    assert not await is_signed_in(before)
    assert await is_signed_in(after)


async def test_a_token_with_sid_and_sub_revokes_by_sid_and_keeps_the_others(env: Env):
    await bound_adult(env)
    ended = await browser_signed_in(env, sid=SID)
    survivor = await browser_signed_in(env, sid=SECOND_SID)

    await deliver(env, logout_token(env.clock.now()))

    assert await revocation_rows(env) == (1, 0)
    assert not await is_signed_in(ended)
    assert await is_signed_in(survivor)


@pytest.mark.parametrize(("make_token", "reason"), [
    pytest.param(lambda now: sign(logout_claims(now), key=signing_key("other")), "bad signature", id="bad-sig"),
    pytest.param(lambda now: logout_token(now, iss="https://elsewhere.invalid/"), "wrong iss", id="wrong-iss"),
    pytest.param(lambda now: logout_token(now, aud="another-client"), "wrong aud", id="wrong-aud"),
    pytest.param(lambda now: logout_token(now, exp=int(now.timestamp()) - CLOCK_SKEW_SECONDS), "expired",
                 id="expired"),
    pytest.param(lambda now: logout_token(now, iat=int(now.timestamp()) + 61, exp=int(now.timestamp()) + 181),
                 "issued in the future", id="issued-in-future"),
    pytest.param(lambda now: "x" * (MAX_TOKEN_CHARS + 1), "too large", id="too-large"),
    pytest.param(lambda now: logout_token(now, exp=None), "no exp", id="no-exp"),
    pytest.param(lambda now: logout_token(now, iat=None), "no iat", id="no-iat"),
    pytest.param(lambda now: logout_token(now, events={"http://schemas.openid.net/event/other": {}}),
                 "missing event", id="missing-event"),
    pytest.param(lambda now: logout_token(now, events=None), "no events", id="no-events"),
    pytest.param(lambda now: logout_token(now, nonce="n-1"), "nonce", id="nonce"),
    pytest.param(lambda now: logout_token(now, sid=None, sub=None), "no sid or sub", id="no-subject"),
    pytest.param(lambda now: "not.a.token", "malformed", id="malformed"),
    pytest.param(lambda now: sign(logout_claims(now), kid="never-published"), "unknown kid", id="unknown-kid"),
])
async def test_each_failed_check_is_a_400_that_records_nothing_and_ends_nothing(
        env: Env, caplog, make_token, reason: str):
    caplog.set_level(logging.INFO)
    await bound_adult(env)
    browser = await browser_signed_in(env, sid=SID)
    token = make_token(env.clock.now())

    response = await deliver(env, token)

    assert response.status_code == 400, reason
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b""
    assert await revocation_rows(env) == (0, 0)
    assert await is_signed_in(browser)
    assert token not in caplog.text and SID not in caplog.text and SUB not in caplog.text


async def test_a_request_with_no_logout_token_field_is_a_400(env: Env):
    response = await env.client.post(ENDPOINT, data={})

    assert response.status_code == 400
    assert await revocation_rows(env) == (0, 0)


async def test_a_key_rotated_at_the_provider_is_fetched_once_and_then_accepted(env: Env):
    await bound_adult(env)
    browser = await browser_signed_in(env, sid=SID)
    env.oidc.jwks_after_refresh = jwks("test-key", "rotated")

    response = await deliver(env, sign(logout_claims(env.clock.now()), kid="rotated"))

    assert response.status_code == 200
    assert env.oidc.provider_calls == [False, True]
    assert not await is_signed_in(browser)


async def test_a_kid_the_provider_still_does_not_publish_after_a_refetch_is_a_400(env: Env):
    await bound_adult(env)
    response = await deliver(env, sign(logout_claims(env.clock.now()), kid="never-published"))

    assert response.status_code == 400
    assert env.oidc.provider_calls == [False, True]


async def test_an_unreachable_provider_is_a_400_and_records_nothing(env: Env, caplog):
    caplog.set_level(logging.INFO)
    await bound_adult(env)
    env.oidc.provider_error = ProviderError("provider_unavailable")

    response = await deliver(env, logout_token(env.clock.now()))

    assert response.status_code == 400
    assert await revocation_rows(env) == (0, 0)
    [record] = [r for r in caplog.records if r.name == "hallpass.web.routes_auth"]
    assert getattr(record, FIELDS_ATTR) == {"outcome": "refused", "reason": "provider_unavailable"}


async def test_the_endpoint_needs_no_session_and_no_csrf_token(env: Env):
    await bound_adult(env)
    fresh = new_client(env)

    response = await fresh.post(ENDPOINT, data={"logout_token": logout_token(env.clock.now())})

    assert response.status_code == 200


async def test_an_empty_logout_token_is_a_400(env: Env):
    response = await deliver(env, "")

    assert response.status_code == 400
    assert await revocation_rows(env) == (0, 0)


def ticking(ticks: list[float]):
    """A monotonic clock the test moves by hand, for the forced-refetch cooldown."""
    return lambda: ticks[0]


async def test_two_unknown_kid_deliveries_in_a_row_force_one_key_fetch(env: Env):
    ticks = [0.0]
    stub = StubOidcClient(metadata={"issuer": ISSUER, "end_session_endpoint": END_SESSION}, jwks=jwks("test-key"))
    use_oidc_client(env, stub, monotonic=ticking(ticks))
    unknown = sign(logout_claims(env.clock.now()), kid="never-published")

    first = await deliver(env, unknown)
    second = await deliver(env, unknown)

    assert (first.status_code, second.status_code) == (400, 400)
    assert stub.jwks_calls.count(True) == 1


@pytest.mark.parametrize(("elapsed", "status", "forced_fetches"), [
    pytest.param(59, 400, 1, id="inside-the-cooldown"),
    pytest.param(60, 200, 2, id="at-the-cooldown"),
])
async def test_a_rotated_key_is_fetched_again_only_after_the_cooldown(env: Env, elapsed: int, status: int,
                                                                      forced_fetches: int):
    await bound_adult(env)
    browser = await browser_signed_in(env, sid=SID)
    ticks = [0.0]
    stub = StubOidcClient(metadata={"issuer": ISSUER, "end_session_endpoint": END_SESSION}, jwks=jwks("test-key"))
    use_oidc_client(env, stub, monotonic=ticking(ticks))
    await deliver(env, sign(logout_claims(env.clock.now()), kid="rotated"))
    stub.rotate(jwks("test-key", "rotated"))

    ticks[0] = float(elapsed)
    response = await deliver(env, sign(logout_claims(env.clock.now()), kid="rotated"))

    assert response.status_code == status
    assert stub.jwks_calls.count(True) == forced_fetches
    assert await is_signed_in(browser) is (status == 400)


async def test_a_token_is_stamped_with_its_issue_time_so_a_session_started_after_the_event_survives(env: Env):
    await bound_adult(env)
    env.clock.set(NOW - timedelta(seconds=20))
    before_the_event = await browser_signed_in(env, sid=None)
    env.clock.set(NOW + timedelta(seconds=10))
    after_the_event = await browser_signed_in(env, sid=SECOND_SID)
    env.clock.set(NOW + timedelta(seconds=30))

    response = await deliver(env, logout_token(NOW, sid=None))

    assert response.status_code == 200
    assert not await is_signed_in(before_the_event)
    assert await is_signed_in(after_the_event)


async def test_a_replayed_token_changes_nothing(env: Env):
    await bound_adult(env)
    token = logout_token(NOW, sid=None)
    env.clock.set(NOW + timedelta(seconds=30))
    await deliver(env, token)
    env.clock.set(NOW + timedelta(seconds=40))
    survivor = await browser_signed_in(env, sid=SECOND_SID)

    env.clock.set(NOW + timedelta(seconds=50))
    response = await deliver(env, token)

    assert response.status_code == 200
    assert await is_signed_in(survivor)


async def test_an_oversized_token_is_refused_before_the_provider_is_asked_for_keys(env: Env):
    response = await deliver(env, "x" * (MAX_TOKEN_CHARS + 1))

    assert response.status_code == 400
    assert env.oidc.provider_calls == []


def _claims_now(env: Env) -> dict:
    return logout_claims(env.clock.now())


def _with_raw_header(env: Env, header_json: str) -> str:
    """A valid token's payload and signature under a header written by hand, for headers joserfc will not encode."""
    _, payload, signature = logout_token(env.clock.now()).split(".")
    header = base64.urlsafe_b64encode(header_json.encode()).decode().rstrip("=")
    return f"{header}.{payload}.{signature}"


def _with_empty_payload(env: Env) -> str:
    """A valid RS256 header with a kid and a valid signature, but no payload between the dots."""
    header, _, signature = logout_token(env.clock.now()).split(".")
    return f"{header}..{signature}"


@pytest.mark.parametrize("build", [
    pytest.param(lambda env: "not-a-jwt", id="not-a-jws"),
    pytest.param(lambda env: "aaa.bbb", id="two-segments"),
    pytest.param(lambda env: "aaa..bbb", id="empty-segment"),
    pytest.param(lambda env: "bm90LWpzb24.e30.c2ln", id="header-not-json"),
    pytest.param(lambda env: _with_raw_header(env, "[]"), id="header-is-json-array"),
    pytest.param(_with_empty_payload, id="empty-payload"),
    pytest.param(lambda env: jwt.encode({"alg": "HS256", "kid": "test-key"}, _claims_now(env),
                                        OctKey.generate_key(256)), id="hs256"),
    pytest.param(lambda env: jwt.encode({"alg": "RS256"}, _claims_now(env), signing_key()), id="no-kid"),
    pytest.param(lambda env: _with_raw_header(env, '{"alg": "RS256", "kid": 7}'), id="kid-not-a-string"),
])
async def test_a_token_that_is_not_an_rs256_jws_with_a_kid_gets_no_key_fetch(env: Env, build):
    await bound_adult(env)

    response = await deliver(env, build(env))

    assert response.status_code == 400
    assert env.oidc.provider_calls == []
    assert await revocation_rows(env) == (0, 0)


async def test_failed_key_fetches_are_spaced_by_the_cooldown_even_for_unauthenticated_traffic(env: Env):
    ticks = [0.0]
    stub = StubOidcClient(metadata={"issuer": ISSUER, "end_session_endpoint": END_SESSION}, jwks=jwks("test-key"),
                          jwks_error=httpx.ConnectError("unreachable"))
    use_oidc_client(env, stub, monotonic=ticking(ticks))
    token = logout_token(env.clock.now())

    statuses = [(await deliver(env, token)).status_code for _ in range(10)]

    assert statuses == [400] * 10
    assert stub.jwks_calls == [True]

    ticks[0] = float(FORCED_REFRESH_COOLDOWN_SECONDS)
    assert (await deliver(env, token)).status_code == 400
    assert stub.jwks_calls == [True, True]


async def test_a_revoked_cookie_stays_ended_after_its_revocation_row_is_pruned(env: Env):
    """The replay: the holder keeps the cookie of a revoked session, the revocation row is pruned by a later delivery,
    and the old cookie must still be sent to sign-in."""
    await bound_adult(env)
    browser = await browser_signed_in(env, sid=SID)
    old_cookie = browser.cookies.get(SESSION_COOKIE)
    assert old_cookie is not None
    env.clock.set(NOW + timedelta(minutes=1))
    assert (await deliver(env, logout_token(env.clock.now()))).status_code == 200
    env.clock.set(env.clock.now() + timedelta(seconds=env.config.session_max_age_seconds + 1))
    assert (await deliver(env, logout_token(env.clock.now(), sid=SECOND_SID))).status_code == 200
    pruned = await env.db.run(lambda c: c.execute("SELECT count(*) FROM revoked_session WHERE sid = ?",
                                                  (SID,)).fetchone()[0])
    assert pruned == 0

    replay = new_client(env)
    replay.cookies.set(SESSION_COOKIE, old_cookie)
    home = await replay.get("/")

    assert home.status_code == 303
    assert home.headers["location"].startswith("/login")
