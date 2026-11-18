# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sessions from before the start time was kept carry no started_at, so a subject-wide revocation cannot be compared
with them. They end for every request: the page and forward auth both send the person to sign-in, once, after the
upgrade. A session that has a start time is unaffected."""

from __future__ import annotations

import base64
import json

import httpx
from itsdangerous import TimestampSigner

from hallpass.auth import SESSION_COOKIE
from tests.helpers import Env, make_service, new_client, publish_terms, sign_in, signed_in_person
from tests.test_forward_auth import check, grant_type


def without_start_time(env: Env, browser: httpx.AsyncClient) -> httpx.AsyncClient:
    """A browser holding this browser's session as it was before the start time was kept: the same signed contents,
    with started_at taken out and the cookie signed again."""
    raw = browser.cookies.get(SESSION_COOKIE)
    assert raw is not None
    contents = json.loads(base64.b64decode(raw.split(".")[0]))
    del contents["started_at"]
    payload = base64.b64encode(json.dumps(contents).encode("utf-8"))
    value = TimestampSigner(str(env.config.session_secret)).sign(payload).decode()
    old = new_client(env)
    old.cookies.set(SESSION_COOKIE, value)
    return old


async def test_a_page_ends_a_session_with_no_start_time_and_sends_the_person_to_sign_in(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    assert (await browser.get("/")).status_code == 200

    response = await without_start_time(env, browser).get("/")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_a_person_can_sign_in_again_after_the_upgrade(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    old = without_start_time(env, browser)

    response = await sign_in(env, sub=person.authentik_uuid, client=old)

    assert response.status_code == 303
    assert (await old.get("/")).status_code == 200


async def test_forward_auth_ends_a_session_with_no_start_time_and_asks_for_sign_in(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    calendar = await make_service(env.db, "calendar")
    await grant_type(env, calendar, "adult", person)
    assert (await check(browser, "calendar")).status_code == 200

    response = await check(without_start_time(env, browser), "calendar")

    assert response.status_code == 302
    assert response.headers["location"].startswith(env.config.public_url + "/login")
