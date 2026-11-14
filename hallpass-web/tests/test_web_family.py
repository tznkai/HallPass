# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The parent approval page, driven through HTTP: a child waiting for a grant, the approval and its refusals.

Covers hallpass.AC4.1, AC4.3 (through HTTP) and AC4.4 (display).
"""

from __future__ import annotations

import httpx

from hallpass import registry, text
from hallpass.registry import Person
from tests.fake_authentik import enrol
from tests.helpers import (
    Env,
    accept_current,
    admin_client,
    form,
    new_client,
    publish_terms,
    sign_in,
    signed_in_person,
    store_person,
)

NOW_TEXT = "2026-11-13T12:00:00+00:00"


async def post(env: Env, path: str, **fields) -> httpx.Response:
    data = await form(env, "/admin/people", **{key: str(value) for key, value in fields.items()})
    return await env.client.post(path, data=data)


async def create_person(env: Env, name: str, account_type: str) -> int:
    response = await post(env, "/admin/people", display_name=name, account_type=account_type, email="", phone="")
    assert response.status_code == 303, response.status_code
    return int(response.headers["location"].rsplit("/", 1)[1])


async def person_row(env: Env, person_id: int) -> Person:
    return await env.db.run(lambda c: registry.get_person(c, person_id))


async def enrol_and_sign_in(env: Env, person_id: int, username: str) -> httpx.AsyncClient:
    """Follows the person's invitation as a new Authentik user, and signs them in on a browser of their own."""
    row = await person_row(env, person_id)
    user = enrol(env.authentik, row.invitation_pk, username)
    browser = new_client(env)
    response = await sign_in(env, sub=user["uuid"], username=username,
                             enrollment_ref=user["attributes"]["hallpass_enrollment"], client=browser)
    assert response.status_code == 303
    return browser


async def family(env: Env) -> tuple[int, int, httpx.AsyncClient, httpx.AsyncClient]:
    """An admin-linked parent and child, both signed in and both having accepted v1. Returns the ids and browsers."""
    await admin_client(env)
    parent = await create_person(env, "Mum", "parent")
    child = await create_person(env, "Kid", "child")
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303
    parent_browser = await enrol_and_sign_in(env, parent, "mum1")
    child_browser = await enrol_and_sign_in(env, child, "kid1")
    assert (await accept_current(env, parent_browser)).status_code == 303
    assert (await accept_current(env, child_browser)).status_code == 303
    return parent, child, parent_browser, child_browser


# AC4.1: the full flow -------------------------------------------------------------------------------------

async def test_a_linked_parent_approves_an_awaiting_child_through_the_pages(env: Env):
    parent, child, parent_browser, _ = await family(env)

    waiting = (await parent_browser.get("/children")).text
    assert "Kid" in waiting and "awaiting_grant" in waiting and "Approve" in waiting
    assert "terms version 1" in waiting

    token = (await form(env, "/children", client=parent_browser))["csrf"]
    done = await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})

    assert done.status_code == 303 and done.headers["location"] == "/children"
    assert (await person_row(env, child)).status == "active"
    approved = (await parent_browser.get("/children")).text
    assert "awaiting_grant" not in approved and "Approve" not in approved


# AC4.4: what the approval records and shows -----------------------------------------------------------

async def test_the_approval_shows_the_parent_the_time_and_the_terms_version(env: Env):
    parent, child, parent_browser, _ = await family(env)
    token = (await form(env, "/children", client=parent_browser))["csrf"]
    await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})

    shown = (await parent_browser.get("/children")).text

    assert "Mum" in shown
    assert NOW_TEXT in shown
    assert "terms version 1" in shown
    stored = await env.db.run(lambda c: c.execute(
        "SELECT parent_id, granted_at FROM parent_grant WHERE child_id = ?", (child,)).fetchall())
    assert stored == [(parent, NOW_TEXT)]


# AC4.3: refusals --------------------------------------------------------------------------------------

async def test_an_unrelated_adult_cannot_approve_the_child(env: Env):
    _, child, _, _ = await family(env)
    _, stranger = await signed_in_person(env, account_type="adult", display_name="Ana")
    token = (await form(env, "/children", client=stranger))["csrf"]

    refused = await stranger.post(f"/children/{child}/grant", data={"csrf": token})

    assert refused.status_code == 403
    assert text.GRANT_REFUSED["not_linked_parent"] in refused.text
    assert (await person_row(env, child)).status == "awaiting_grant"
    assert await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM parent_grant").fetchone()[0]) == 0


async def test_a_missing_child_and_an_unlinked_child_get_the_same_refusal(env: Env):
    _, stranger = await signed_in_person(env, account_type="adult", display_name="Ana")
    unlinked = await store_person(env, display_name="Unlinked", account_type="child")
    token = (await form(env, "/children", client=stranger))["csrf"]

    missing = await stranger.post("/children/999999/grant", data={"csrf": token})
    existing = await stranger.post(f"/children/{unlinked.id}/grant", data={"csrf": token})

    assert missing.status_code == existing.status_code == 403
    assert missing.text == existing.text
    assert text.GRANT_REFUSED["not_linked_parent"] in missing.text
    assert await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM parent_grant").fetchone()[0]) == 0


async def test_a_linked_parent_who_has_not_accepted_the_current_version_is_refused(env: Env):
    _, child, parent_browser, _ = await family(env)
    await publish_terms(env, blocking=False)
    token = (await form(env, "/children", client=parent_browser))["csrf"]

    refused = await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})

    assert refused.status_code == 403
    assert text.GRANT_REFUSED["parent_terms"] in refused.text
    assert (await person_row(env, child)).status == "awaiting_grant"


async def test_the_grant_needs_the_csrf_token(env: Env):
    _, child, parent_browser, _ = await family(env)

    refused = await parent_browser.post(f"/children/{child}/grant", data={"csrf": "not-the-token"})

    assert refused.status_code == 403
    assert (await person_row(env, child)).status == "awaiting_grant"


# The nav link -------------------------------------------------------------------------------------------

async def test_the_children_link_shows_only_for_a_parent_with_linked_children(env: Env):
    _, _, parent_browser, child_browser = await family(env)

    assert 'href="/children"' in (await parent_browser.get("/")).text
    assert 'href="/children"' not in (await child_browser.get("/")).text
