# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Forward auth, driven through HTTP: the session branch, the feed-token branch, and the terms and status gates.

Covers hallpass.AC5.4 to AC5.8, and the forward-auth parts of AC3.4, AC3.5 and AC4.2.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from hallpass import registry, services, tokens
from hallpass.auth import Identity
from hallpass.logs import FIELDS_ATTR
from hallpass.registry import Person
from tests.fake_authentik import enrol
from tests.logout_tokens import SID, SUB, logout_token
from tests.helpers import (
    BASE_URL,
    NOW,
    Env,
    accept_current,
    accept_for,
    form,
    invite_person,
    make_service,
    new_client,
    publish_terms,
    sign_in,
    signed_in_person,
    store_person,
)
from tests.test_web_family import family

SESSION = "hallpass_session"
SECOND_SID = "sid-test-session-two"
FORWARDED_HOST = "calendar.hallpass.test"
PROTO = "https"


async def check(browser: httpx.AsyncClient, service: str, uri: str = "/") -> httpx.Response:
    """Asks forward auth about a service, as Caddy does: the forwarded host, proto and URI, and the browser's cookies."""
    return await browser.get("/forward-auth", params={"service": service},
                             headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": FORWARDED_HOST,
                                      "X-Forwarded-Uri": uri})


def _with_cookie(env: Env, value: str) -> httpx.AsyncClient:
    """A browser that carries only the given session cookie value."""
    browser = new_client(env)
    browser.cookies.set(SESSION, value)
    return browser


async def grant_type(env: Env, service: services.Service, account_type: str, actor: Person) -> services.Entitlement:
    return await env.db.run(lambda c: services.add_entitlement(
        c, service.id, "type", account_type, "grant", actor_id=actor.id, now=NOW))


async def person_deny(env: Env, service: services.Service, person: Person, actor: Person) -> services.Entitlement:
    return await env.db.run(lambda c: services.add_entitlement(
        c, service.id, "person", str(person.id), "deny", actor_id=actor.id, now=NOW))


# AC5.4: a signed-in, entitled, unsuspended person -------------------------------------------------------------

async def test_an_entitled_signed_in_adult_gets_200_with_the_person_headers(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    calendar = await make_service(env.db, "calendar")
    await grant_type(env, calendar, "adult", person)

    response = await check(browser, "calendar")

    assert response.status_code == 200
    assert response.headers["x-hallpass-person"] == person.ref
    assert response.headers["x-hallpass-type"] == "adult"


# AC5.5: no session, not entitled, suspended ------------------------------------------------------------------

async def test_a_request_with_no_session_is_sent_to_sign_in(env: Env):
    await publish_terms(env)
    await make_service(env.db, "calendar")

    response = await check(new_client(env), "calendar", uri="/events?week=2")

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{BASE_URL}/login?next=")
    expected = urlencode({"next": f"{PROTO}://{FORWARDED_HOST}/events?week=2"})
    assert location == f"{BASE_URL}/login?{expected}"


async def test_a_tampered_session_cookie_is_treated_as_no_session(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    calendar = await make_service(env.db, "calendar")
    await grant_type(env, calendar, "adult", person)
    cookie = browser.cookies.get(SESSION)
    assert cookie is not None
    assert (await check(_with_cookie(env, cookie), "calendar")).status_code == 200
    # The first character is inside the signed payload. The last one can be padding bits, which change nothing.
    tampered = ("A" if cookie[0] != "A" else "B") + cookie[1:]

    response = await check(_with_cookie(env, tampered), "calendar")

    assert response.status_code == 302
    assert response.headers["location"].startswith(f"{BASE_URL}/login?next=")


async def test_a_signed_in_person_without_the_entitlement_gets_403(env: Env):
    await publish_terms(env)
    _, browser = await signed_in_person(env, account_type="adult")
    await make_service(env.db, "calendar")

    response = await check(browser, "calendar")

    assert response.status_code == 403
    assert response.content == b""


async def test_a_person_level_deny_gets_403_after_an_allow(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    calendar = await make_service(env.db, "calendar")
    await grant_type(env, calendar, "adult", person)
    assert (await check(browser, "calendar")).status_code == 200
    await person_deny(env, calendar, person, person)

    assert (await check(browser, "calendar")).status_code == 403


# AC5.6: unknown and retired services ---------------------------------------------------------------------------

async def test_an_unknown_service_slug_gets_403_even_with_a_session(env: Env):
    await publish_terms(env)
    _, browser = await signed_in_person(env, account_type="adult")

    response = await check(browser, "no-such-service")

    assert response.status_code == 403


async def test_an_unknown_service_slug_gets_403_with_no_session(env: Env):
    response = await check(new_client(env), "no-such-service")

    assert response.status_code == 403


async def test_a_retired_service_gets_403_even_when_the_person_is_entitled(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    calendar = await make_service(env.db, "calendar")
    await grant_type(env, calendar, "adult", person)
    assert (await check(browser, "calendar")).status_code == 200
    await env.db.run(lambda c: services.retire_service(c, calendar.id, actor_id=person.id, now=NOW))

    assert (await check(browser, "calendar")).status_code == 403


# AC5.7: feed tokens ----------------------------------------------------------------------------------------------

async def _feed_setup(env: Env) -> tuple[Person, services.Service]:
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    feed = await make_service(env.db, "cal-feed", kind="feed")
    await grant_type(env, feed, "adult", person)
    return person, feed


async def _issue(env: Env, person: Person, feed: services.Service) -> str:
    return await env.db.run(lambda c: tokens.issue_feed(c, person_id=person.id, service_id=feed.id, now=NOW))


async def _feed_check(env: Env, token: str | None) -> httpx.Response:
    uri = "/feed.ics" if token is None else f"/feed.ics?token={token}"
    return await check(new_client(env), "cal-feed", uri=uri)


async def test_a_valid_feed_token_gets_200_with_no_cookie(env: Env):
    person, feed = await _feed_setup(env)
    raw = await _issue(env, person, feed)

    response = await _feed_check(env, raw)

    assert response.status_code == 200
    assert response.headers["x-hallpass-person"] == person.ref
    assert response.headers["x-hallpass-type"] == "adult"


async def test_an_unknown_feed_token_gets_403(env: Env):
    await _feed_setup(env)

    response = await _feed_check(env, "not-a-real-token")

    assert response.status_code == 403


async def test_a_regenerated_feed_token_refuses_the_old_one(env: Env):
    person, feed = await _feed_setup(env)
    old = await _issue(env, person, feed)
    new = await _issue(env, person, feed)

    assert (await _feed_check(env, old)).status_code == 403
    assert (await _feed_check(env, new)).status_code == 200


async def test_a_feed_request_with_no_token_gets_403_never_a_redirect(env: Env):
    person, feed = await _feed_setup(env)
    await _issue(env, person, feed)

    response = await _feed_check(env, None)

    assert response.status_code == 403
    assert "location" not in response.headers


async def test_a_feed_token_for_a_person_without_the_entitlement_gets_403(env: Env):
    person, feed = await _feed_setup(env)
    raw = await _issue(env, person, feed)
    entitlement_id = await env.db.run(lambda c: services.entitlements_for(c, feed.id)[0].id)
    await env.db.run(lambda c: services.remove_entitlement(c, entitlement_id, actor_id=person.id, now=NOW))

    assert (await _feed_check(env, raw)).status_code == 403


# AC5.8: a suspension takes effect on the next request ----------------------------------------------------------

async def test_removing_the_entitlement_takes_effect_on_the_next_request(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    calendar = await make_service(env.db, "calendar")
    entitlement = await grant_type(env, calendar, "adult", person)
    assert (await check(browser, "calendar")).status_code == 200

    await env.db.run(lambda c: services.remove_entitlement(c, entitlement.id, actor_id=person.id, now=NOW))

    assert (await check(browser, "calendar")).status_code == 403


# AC3.4 (forward auth): the terms gate answers 403, never a redirect to /terms -----------------------------------

async def test_a_signed_in_person_who_never_accepted_the_terms_gets_403(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult", accepted=False)
    calendar = await make_service(env.db, "calendar")
    await grant_type(env, calendar, "adult", person)

    response = await check(browser, "calendar")

    assert response.status_code == 403
    assert "location" not in response.headers


# AC3.5 (forward auth): optional versions allow, blocking versions deny until accepted --------------------------

async def test_forward_auth_follows_the_terms_versions(env: Env):
    await publish_terms(env, blocking=False)
    person, browser = await signed_in_person(env, account_type="adult")
    calendar = await make_service(env.db, "calendar")
    await grant_type(env, calendar, "adult", person)

    await publish_terms(env, blocking=False)
    assert (await check(browser, "calendar")).status_code == 200

    await publish_terms(env, blocking=True)
    assert (await check(browser, "calendar")).status_code == 403

    assert (await accept_current(env, browser)).status_code == 303
    assert (await check(browser, "calendar")).status_code == 200


# AC4.2 (forward auth): a child waiting for a grant is denied until the parent approves ----------------------------

async def test_a_child_awaiting_a_grant_is_denied_until_a_parent_approves(env: Env):
    parent, child, parent_browser, child_browser = await family(env)
    calendar = await make_service(env.db, "calendar")
    await env.db.run(lambda c: services.add_entitlement(
        c, calendar.id, "type", "child", "grant", actor_id=parent, now=NOW))
    assert (await check(child_browser, "calendar")).status_code == 403

    token = (await form(env, "/children", client=parent_browser))["csrf"]
    assert (await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})).status_code == 303

    assert (await check(child_browser, "calendar")).status_code == 200


# Logs: no token, URI or host, and no service slug for an unknown one ----------------------------------------------

async def test_forward_auth_logs_leave_out_the_token_the_uri_the_host_and_the_unknown_slug(env: Env, caplog):
    await make_service(env.db, "calendar")
    person, feed = await _feed_setup(env)
    raw = await _issue(env, person, feed)
    caplog.set_level(logging.INFO)
    caplog.clear()

    allowed = await check(new_client(env), "cal-feed", uri=f"/x.ics?token={raw}")
    signin = await check(new_client(env), "calendar", uri="/events?week=2")
    unknown = await check(new_client(env), "no-such-service", uri="/x.ics")

    assert (allowed.status_code, signin.status_code, unknown.status_code) == (200, 302, 403)
    records = [record for record in caplog.records if record.name.startswith("hallpass")]
    events = [record for record in records if record.getMessage() == "forward_auth"]
    assert [getattr(record, FIELDS_ATTR)["outcome"] for record in events] == ["allow", "signin", "deny"]
    # The whole record, message, arguments and extra fields, must hold none of the forwarded values.
    forbidden = (raw, FORWARDED_HOST, "/x.ics", "/events")
    for record in records:
        blob = repr(record.__dict__)
        for value in forbidden:
            assert value not in blob, f"a {record.getMessage()} record holds a forbidden value"
    unknown_record = next(record for record in events
                          if getattr(record, FIELDS_ATTR).get("reason") == "unknown_service")
    assert "service" not in getattr(unknown_record, FIELDS_ATTR)


# AC5.5: a person locked after sign-in ------------------------------------------------------------------------------

async def test_a_person_locked_after_sign_in_gets_403_on_the_same_browser_with_no_redirect(env: Env, caplog):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    calendar = await make_service(env.db, "calendar")
    await grant_type(env, calendar, "adult", person)
    assert (await check(browser, "calendar")).status_code == 200
    await env.db.run(lambda c: registry.set_locked(c, person.id, True))
    caplog.set_level(logging.INFO)
    caplog.clear()

    response = await check(browser, "calendar")

    assert response.status_code == 403
    assert "location" not in response.headers
    # The lock check in forward auth, not the general access check, is what refuses here: the reason is "locked".
    events = [record for record in caplog.records if record.getMessage() == "forward_auth"]
    last = getattr(events[-1], FIELDS_ATTR)
    assert last["outcome"] == "deny"
    assert last["reason"] == "locked"


# AC5.5 with the login route: a hostile forwarded host is not kept as the place to return to ------------------------

async def test_the_forward_auth_sign_in_redirect_does_not_keep_a_hostile_next(env: Env):
    await publish_terms(env)
    await make_service(env.db, "calendar")
    person = await store_person(env, account_type="adult")
    user = enrol(env.authentik, await invite_person(env, person), f"person{person.id}")
    await accept_for(env, person.id)
    browser = new_client(env)

    hostile = await browser.get("/forward-auth", params={"service": "calendar"},
                                headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.invalid",
                                         "X-Forwarded-Uri": "/x"})
    assert hostile.status_code == 302
    assert parse_qs(urlsplit(hostile.headers["location"]).query)["next"] == ["https://evil.invalid/x"]

    login = await browser.get(hostile.headers["location"])
    assert login.status_code == 302
    env.oidc.next_identity = Identity(sub=user["uuid"], username=user["username"],
                                      enrollment_ref=user["attributes"]["hallpass_enrollment"])
    callback = await browser.get(login.headers["location"])

    assert callback.status_code == 303
    assert callback.headers["location"] == "/"


# Phase 8: an active, entitled child is denied while no linked parent covers them ----------------------------------

async def test_an_active_entitled_child_is_denied_when_no_linked_parent_has_accepted(env: Env):
    parent, child, parent_browser, child_browser = await family(env)
    calendar = await make_service(env.db, "calendar")
    await env.db.run(lambda c: services.add_entitlement(
        c, calendar.id, "type", "child", "grant", actor_id=parent, now=NOW))
    token = (await form(env, "/children", client=parent_browser))["csrf"]
    assert (await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})).status_code == 303
    assert (await check(child_browser, "calendar")).status_code == 200

    await publish_terms(env, blocking=True)     # the parent has not accepted this version

    response = await check(child_browser, "calendar")
    assert response.status_code == 403
    assert "location" not in response.headers
    assert (await person_row_status(env, child)) == "active"
    assert (await check(parent_browser, "calendar")).status_code == 403

    assert (await accept_current(env, parent_browser)).status_code == 303
    assert (await check(child_browser, "calendar")).status_code == 200


async def person_row_status(env: Env, person_id: int) -> str:
    person = await env.db.run(lambda c: registry.get_person(c, person_id))
    return person.status


# Phase 8: a revoked Authentik session is no session at forward auth ---------------------------------------------

async def bound_adult_granted_calendar(env: Env) -> Person:
    await publish_terms(env)
    person = await store_person(env, account_type="adult")
    await env.db.run(lambda c: registry.bind_identity(c, person.id, uuid=SUB, authentik_pk=7, username="kid1"))
    await accept_for(env, person.id)
    await grant_type(env, await make_service(env.db, "calendar"), "adult", person)
    return person


async def signed_in_with_sid(env: Env, sid: str) -> httpx.AsyncClient:
    browser = new_client(env)
    response = await sign_in(env, sub=SUB, username="kid1", sid=sid, client=browser)
    assert response.status_code == 303
    return browser


async def deliver_logout(env: Env, **overrides) -> httpx.Response:
    return await env.client.post("/auth/backchannel-logout",
                                 data={"logout_token": logout_token(env.clock.now(), **overrides)})


async def test_a_session_whose_sid_was_revoked_is_sent_to_sign_in_at_forward_auth(env: Env):
    await bound_adult_granted_calendar(env)
    ended = await signed_in_with_sid(env, SID)
    survivor = await signed_in_with_sid(env, SECOND_SID)
    assert (await check(ended, "calendar")).status_code == 200

    assert (await deliver_logout(env)).status_code == 200

    response = await check(ended, "calendar")
    assert response.status_code == 302
    assert response.headers["location"].startswith(f"{BASE_URL}/login?")
    assert "x-hallpass-person" not in response.headers
    assert (await check(survivor, "calendar")).status_code == 200


async def test_a_subject_revocation_sends_older_sessions_to_sign_in_at_forward_auth(env: Env):
    await bound_adult_granted_calendar(env)
    older = await signed_in_with_sid(env, SID)
    revoked_at = env.clock.now() + timedelta(seconds=60)
    env.clock.set(revoked_at)

    assert (await deliver_logout(env, sid=None, iat=int(revoked_at.timestamp()),
                                 exp=int(revoked_at.timestamp()) + 120)).status_code == 200
    env.clock.set(revoked_at + timedelta(seconds=30))
    newer = await signed_in_with_sid(env, SECOND_SID)

    assert (await check(older, "calendar")).status_code == 302
    assert (await check(newer, "calendar")).status_code == 200


async def test_a_session_past_its_max_age_is_sent_to_sign_in_at_forward_auth_with_nothing_revoked(env: Env):
    await bound_adult_granted_calendar(env)
    browser = await signed_in_with_sid(env, SECOND_SID)
    assert (await check(browser, "calendar")).status_code == 200
    env.clock.set(env.clock.now() + timedelta(seconds=env.config.session_max_age_seconds + 1))

    response = await check(browser, "calendar")

    assert response.status_code == 302
    assert response.headers["location"].startswith(f"{BASE_URL}/login?")
