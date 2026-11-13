# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in through the web app: cookie flags, enrollment, refusal pages, the redirect check and logout."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from hallpass import registry, text
from tests.fake_authentik import enrol
from tests.helpers import Env, admin_client, form, invite_person, make_env, sign_in, store_person

KID_SUB = "00000000-0000-4000-8000-0000000000c1"
UNKNOWN_SUB = "00000000-0000-4000-8000-0000000000c3"


def session_cookie(response: httpx.Response) -> list[str]:
    """The session cookie's attributes, lower-cased, as separate tokens."""
    headers = [h for h in response.headers.get_list("set-cookie") if h.startswith("hallpass_session=")]
    assert headers, "no session cookie was set"
    return [part.strip() for part in headers[-1].lower().split(";")]


async def test_session_cookie_is_secure_httponly_and_lax(env: Env):
    response = await admin_client(env)
    attributes = session_cookie(response)
    assert "secure" in attributes
    assert "httponly" in attributes
    assert "samesite=lax" in attributes


async def test_session_cookie_carries_the_configured_domain(tmp_path: Path):
    environment = await make_env(tmp_path, HALLPASS_COOKIE_DOMAIN=".hallpass.test")
    try:
        response = await admin_client(environment)
        assert "domain=.hallpass.test" in session_cookie(response)
    finally:
        await environment.close()


@pytest.mark.parametrize("fields", [
    {"account_type": "child"},                                       # AC1.3: a child with no email
    {"account_type": "adult", "email": "ana@parent.invalid"},        # AC1.2 with an address on file
], ids=["child-without-email", "adult-with-email"])
async def test_enrolled_person_signs_in_and_then_by_sub_alone(env: Env, fields):
    person = await store_person(env, display_name="Kid", **fields)
    invitation_pk = await invite_person(env, person)
    user = enrol(env.authentik, invitation_pk, "kid1")
    ref = user["attributes"]["hallpass_enrollment"]

    first = await sign_in(env, sub=user["uuid"], username="kid1", enrollment_ref=ref)
    assert first.status_code == 303
    assert first.headers["location"] == "/"
    assert "Kid" in (await env.client.get("/")).text
    stored = await env.db.run(lambda c: registry.get_person(c, person.id))
    assert (stored.authentik_uuid, stored.authentik_pk) == (user["uuid"], user["pk"])

    token = (await form(env, "/"))["csrf"]
    assert (await env.client.post("/logout", data={"csrf": token})).status_code == 303
    again = await sign_in(env, sub=user["uuid"], username="kid1")
    assert again.status_code == 303
    assert "Kid" in (await env.client.get("/")).text


async def test_unknown_account_gets_not_registered_and_no_session(env: Env):
    response = await sign_in(env, sub=UNKNOWN_SUB, username="stranger")
    assert response.status_code == 403
    assert text.NOT_REGISTERED in response.text
    home = await env.client.get("/")
    assert home.status_code == 303
    assert home.headers["location"].startswith("/login")


async def test_locked_person_gets_the_locked_page(env: Env):
    person = await store_person(env)
    await env.db.run(lambda c: registry.bind_identity(c, person.id, uuid=KID_SUB, authentik_pk=7, username="kid1"))
    await env.db.run(lambda c: registry.set_locked(c, person.id, True))
    response = await sign_in(env, sub=KID_SUB, username="kid1")
    assert response.status_code == 403
    assert text.LOCKED in response.text


async def test_authentik_outage_gives_503_and_leaves_the_person_unbound(env: Env):
    person = await store_person(env)
    user = enrol(env.authentik, await invite_person(env, person), "kid1")
    ref = user["attributes"]["hallpass_enrollment"]
    env.authentik.fail_next = 500

    response = await sign_in(env, sub=user["uuid"], username="kid1", enrollment_ref=ref)

    assert response.status_code == 503
    assert text.SIGNIN_UNAVAILABLE in response.text
    stored = await env.db.run(lambda c: registry.get_person(c, person.id))
    assert stored.authentik_uuid is None and stored.enrollment_ref == ref
    retry = await sign_in(env, sub=user["uuid"], username="kid1", enrollment_ref=ref)
    assert retry.status_code == 303


@pytest.mark.parametrize("bad", [
    "https://evil.invalid/",
    "//evil.invalid/",
    "/\\evil.invalid/",
    "/\t/evil.invalid/",
    "http://hallpass.test/",
])
async def test_unsafe_next_values_are_ignored(env: Env, bad: str):
    person = await store_person(env, account_type="adult")
    await env.db.run(lambda c: registry.bind_identity(c, person.id, uuid=KID_SUB, authentik_pk=7, username="kid1"))
    response = await sign_in(env, sub=KID_SUB, username="kid1", next_path=bad)
    assert response.headers["location"] == "/"


async def test_a_safe_next_value_is_kept(env: Env):
    person = await store_person(env, account_type="adult")
    await env.db.run(lambda c: registry.bind_identity(c, person.id, uuid=KID_SUB, authentik_pk=7, username="kid1"))
    response = await sign_in(env, sub=KID_SUB, username="kid1", next_path="/children")
    assert response.headers["location"] == "/children"


async def test_logout_ends_the_session_and_needs_the_csrf_token(env: Env):
    await admin_client(env)
    refused = await env.client.post("/logout", data={"csrf": "wrong"})
    assert refused.status_code == 403
    assert (await env.client.get("/")).status_code == 200

    token = (await form(env, "/"))["csrf"]
    done = await env.client.post("/logout", data={"csrf": token})
    assert done.status_code == 303 and done.headers["location"] == "/"
    assert (await env.client.get("/")).headers["location"].startswith("/login")


async def test_signed_in_home_names_the_person(env: Env):
    response = await admin_client(env)
    assert response.status_code == 303
    assert "root" in (await env.client.get("/")).text


async def test_next_is_dropped_when_sign_in_is_refused(env: Env):
    response = await sign_in(env, sub=UNKNOWN_SUB, next_path="/children")
    assert response.status_code == 403
    assert "/children" not in response.headers.get("location", "")

