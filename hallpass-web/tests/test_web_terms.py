# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The terms gate on pages and the /terms page: first acceptance, later versions, stale versions and refusals.

Covers hallpass.AC3.2 and hallpass.AC3.4 (pages). The admin exemption is tested with the admin terms pages.
"""

from __future__ import annotations

import re

from hallpass import registry, text
from hallpass.auth import current_person_any_terms
from hallpass.registry import Person
from tests.fake_authentik import enrol
from tests.helpers import (
    TERMS_PRIVACY,
    TERMS_TOS,
    Env,
    accept_current,
    api_routes,
    form,
    invite_person,
    publish_terms,
    sign_in,
    signed_in_person,
    store_person,
)

NOW_TEXT = "2026-11-13T12:00:00+00:00"
# Pages that answer without a terms acceptance. Later phases add their own ungated machine endpoints here, such as
# /forward-auth in Phase 4.
UNGATED_PATHS = {"/terms", "/login", "/auth/callback", "/forward-auth"}


def gated_get_paths(env: Env) -> list[str]:
    """Every GET route with its path parameters filled by 1, except the ungated ones, the admin terms pages and static."""
    paths = []
    for route in api_routes(env.application):
        if "GET" not in route.methods:
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        if path in UNGATED_PATHS or path.startswith("/admin/terms") or path.startswith("/static"):
            continue
        paths.append(path)
    return paths


# AC3.2: first sign-in and acceptance -------------------------------------------------------------------------

async def test_a_new_person_is_sent_to_terms_and_the_acceptance_is_recorded(env: Env):
    version_id = await publish_terms(env)
    person = await store_person(env, display_name="Ana", account_type="adult")
    user = enrol(env.authentik, await invite_person(env, person), "ana1")

    response = await sign_in(env, sub=user["uuid"], username="ana1",
                             enrollment_ref=user["attributes"]["hallpass_enrollment"])

    assert response.status_code == 303
    assert response.headers["location"] == "/terms?next=%2F"
    shown = await env.client.get("/terms")
    assert shown.status_code == 200
    assert TERMS_TOS in shown.text and TERMS_PRIVACY in shown.text
    assert text.TERMS_NOT_NOW not in shown.text, "a blocking version offers no way out"

    accepted = await accept_current(env)
    assert accepted.status_code == 303 and accepted.headers["location"] == "/"
    rows = await env.db.run(lambda c: c.execute(
        "SELECT person_id, terms_version_id, accepted_at FROM terms_acceptance").fetchall())
    assert rows == [(person.id, version_id, NOW_TEXT)]
    assert "Ana" in (await env.client.get("/")).text


async def test_a_later_optional_version_prompts_with_not_now_but_does_not_block(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env)
    await publish_terms(env, blocking=False)

    response = await sign_in(env, sub=person.authentik_uuid, username=person.username, client=browser)

    assert response.status_code == 303 and response.headers["location"].startswith("/terms")
    prompt = await browser.get("/terms?next=/children")
    assert text.TERMS_NOT_NOW in prompt.text
    assert 'href="/children"' in prompt.text
    assert (await browser.get("/")).status_code == 200


async def test_a_later_blocking_version_stops_the_pages_until_it_is_accepted(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env)
    await publish_terms(env, blocking=True)

    blocked = await browser.get("/")
    assert blocked.status_code == 303 and blocked.headers["location"].startswith("/terms")
    prompt = await browser.get("/terms")
    assert text.TERMS_NOT_NOW not in prompt.text

    assert (await accept_current(env, browser)).status_code == 303
    assert (await browser.get("/")).status_code == 200


# AC3.4 (pages): nothing but the terms page and sign-out before a first acceptance ---------------------------

async def test_before_a_first_acceptance_every_page_redirects_to_terms(env: Env):
    await publish_terms(env)
    _, browser = await signed_in_person(env, accepted=False)

    paths = gated_get_paths(env)

    assert {"/", "/admin/people", "/admin/groups"} <= set(paths)
    for path in paths:
        response = await browser.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/terms"), path


async def test_before_a_first_acceptance_the_terms_page_and_sign_out_still_work(env: Env):
    await publish_terms(env)
    _, browser = await signed_in_person(env, accepted=False)

    assert (await browser.get("/terms")).status_code == 200
    token = (await form(env, "/terms", client=browser))["csrf"]
    done = await browser.post("/logout", data={"csrf": token})

    assert done.status_code == 303 and done.headers["location"] == "/"
    assert (await browser.get("/")).headers["location"].startswith("/login")


async def test_with_no_version_published_the_terms_page_says_so_and_pages_redirect(env: Env):
    _, browser = await signed_in_person(env, accepted=False)

    page = await browser.get("/terms")

    assert page.status_code == 200
    assert text.TERMS_NOT_PUBLISHED in page.text
    assert (await browser.get("/")).headers["location"].startswith("/terms")


# AC3.2: acceptance is checked against the current version ------------------------------------------------

async def test_a_person_already_locked_when_they_post_is_refused_by_the_auth_gate(env: Env):
    # The auth gate (current_person_any_terms) sees the lock and refuses before the route body runs. The route's own
    # AcceptanceLocked mapping is covered by test_a_person_locked_between_the_sign_in_check_and_the_write.
    await publish_terms(env)
    person, browser = await signed_in_person(env, accepted=False)
    page = await browser.get("/terms")
    token = (await form(env, "/terms", client=browser))["csrf"]
    version_id = re.search(r'name="version_id" value="(\d+)"', page.text).group(1)
    await env.db.run(lambda c: registry.set_locked(c, person.id, True))

    locked = await browser.post("/terms/accept", data={"csrf": token, "version_id": version_id})

    assert locked.status_code == 403
    assert text.LOCKED in locked.text
    count = await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM terms_acceptance").fetchone()[0])
    assert count == 0


async def test_a_person_locked_between_the_sign_in_check_and_the_write_is_refused_and_signed_out(env: Env):
    # The auth gate reads the person before the lock lands, so the route reaches the write and the write refuses.
    await publish_terms(env)
    person, browser = await signed_in_person(env, accepted=False)
    assert person.status != "locked"
    stale_person = person          # what the auth gate read before the lock was written

    async def read_before_the_lock() -> Person:
        return stale_person

    page = await browser.get("/terms")
    token = (await form(env, "/terms", client=browser))["csrf"]
    version_id = re.search(r'name="version_id" value="(\d+)"', page.text).group(1)
    env.application.dependency_overrides[current_person_any_terms] = read_before_the_lock
    try:
        await env.db.run(lambda c: registry.set_locked(c, person.id, True))
        locked = await browser.post("/terms/accept", data={"csrf": token, "version_id": version_id})
    finally:
        env.application.dependency_overrides.pop(current_person_any_terms, None)

    assert locked.status_code == 403
    assert text.LOCKED in locked.text
    count = await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM terms_acceptance").fetchone()[0])
    assert count == 0
    follow_up = await browser.get("/")
    assert follow_up.status_code == 303
    assert follow_up.headers["location"].startswith("/login")


async def test_accepting_a_stale_version_is_refused_and_records_nothing(env: Env):
    old_version = await publish_terms(env)
    person, browser = await signed_in_person(env, accepted=False)
    await publish_terms(env)
    token = (await form(env, "/terms", client=browser))["csrf"]

    stale = await browser.post("/terms/accept", data={"csrf": token, "version_id": str(old_version)})

    assert stale.status_code == 409
    assert text.TERMS_CHANGED in stale.text
    rows = await env.db.run(lambda c: c.execute(
        "SELECT person_id FROM terms_acceptance WHERE person_id = ?", (person.id,)).fetchall())
    assert rows == []


async def test_the_accept_form_needs_the_csrf_token(env: Env):
    await publish_terms(env)
    _, browser = await signed_in_person(env, accepted=False)
    version_page = await browser.get("/terms")
    version_id = re.search(r'name="version_id" value="(\d+)"', version_page.text).group(1)

    refused = await browser.post("/terms/accept", data={"csrf": "not-the-token", "version_id": version_id})

    assert refused.status_code == 403
    count = await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM terms_acceptance").fetchone()[0])
    assert count == 0
