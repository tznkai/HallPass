# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The parent approval page, driven through HTTP: a child waiting for a grant, the approval and its refusals.

Covers hallpass.AC4.1, AC4.3 (through HTTP) and AC4.4 (display).
"""

from __future__ import annotations

import httpx
from markupsafe import escape

from hallpass import authentik_sync, registry, services, text
from hallpass.registry import Person
from tests.fake_authentik import enrol
from tests.helpers import (
    ADMIN_SUB,
    FLUXER_GROUP,
    NOW,
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

# The notice as Jinja writes it into HTML (the apostrophe is escaped).
SHARED_NOTICE_HTML = str(escape(text.SHARED_IDENTITY_PAUSED))
ON_BEHALF_HTML = str(escape("on the child's behalf"))


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
    """An admin-linked parent who accepted v1, and a child who has signed in (so awaiting a grant). A child never
    accepts. Returns the ids and browsers."""
    await admin_client(env)
    parent = await create_person(env, "Mum", "parent")
    child = await create_person(env, "Kid", "child")
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303
    parent_browser = await enrol_and_sign_in(env, parent, "mum1")
    child_browser = await enrol_and_sign_in(env, child, "kid1")
    assert (await accept_current(env, parent_browser)).status_code == 303
    return parent, child, parent_browser, child_browser


# AC4.1: the full flow -------------------------------------------------------------------------------------

async def test_a_linked_parent_approves_an_awaiting_child_through_the_pages(env: Env):
    parent, child, parent_browser, _ = await family(env)

    waiting = (await parent_browser.get("/children")).text
    assert "Kid" in waiting and "awaiting_grant" in waiting and "Approve" in waiting
    assert "terms version 1" in waiting
    assert "Approving lets Kid use the services." in waiting.replace("&#39;", "'")

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


async def test_a_parent_sees_the_shared_identity_notice_on_a_child_row_without_the_other_persons_name(env: Env):
    # The child holds a gamertag that a non-entitled person also holds. The other person is suspended, so the child, who
    # is allowed once approved, is the holder whose identity is paused. Before the approval the child is suspended, and
    # a suspended holder gets no notice.
    parent, child, parent_browser, _ = await family(env)
    parent_person = await env.db.run(lambda c: registry.get_person(c, parent))
    minecraft = await env.db.run(lambda c: services.create_service(
        c, slug="minecraft", name="Minecraft", kind="game", adapter="minecraft", auto_suspendable=True, url=None,
        actor_id=parent_person.id, now=NOW))
    await env.db.run(lambda c: services.add_entitlement(c, minecraft.id, "type", "child", "grant",
                                                        actor_id=parent_person.id, now=NOW))
    other = await env.db.run(lambda c: registry.create_person(c, display_name="Zara Other", account_type="adult",
                                                               now=NOW))
    await env.db.run(lambda c: registry.add_identity(c, child, "minecraft", "java_name", "PlayerOne"))
    await env.db.run(lambda c: registry.add_identity(c, other.id, "minecraft", "java_name", "playerone"))

    waiting = await parent_browser.get(f"/children/{child}")
    assert waiting.status_code == 200
    assert SHARED_NOTICE_HTML not in waiting.text

    token = (await form(env, "/children", client=parent_browser))["csrf"]
    assert (await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})).status_code == 303
    response = await parent_browser.get(f"/children/{child}")

    assert response.status_code == 200
    assert SHARED_NOTICE_HTML in response.text
    assert "Zara Other" not in response.text


# Phase 8: the approval comes before the child enrols -------------------------------------------------------

async def test_the_approval_says_it_is_given_on_the_childs_behalf(env: Env):
    _, _, parent_browser, _ = await family(env)

    waiting = (await parent_browser.get("/children")).text

    assert ON_BEHALF_HTML in waiting


async def test_a_parent_can_approve_an_invited_child_before_the_child_enrols(env: Env):
    await admin_client(env)
    parent = await create_person(env, "Mum", "parent")
    child = await create_person(env, "Kid", "child")
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303
    parent_browser = await enrol_and_sign_in(env, parent, "mum1")
    assert (await accept_current(env, parent_browser)).status_code == 303

    before = (await parent_browser.get("/children")).text
    assert "Kid" in before and "Approve" in before
    token = (await form(env, "/children", client=parent_browser))["csrf"]
    done = await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})

    assert done.status_code == 303 and done.headers["location"] == "/children"
    assert (await person_row(env, child)).status == "invited"
    shown = (await parent_browser.get("/children")).text
    assert text.GRANT_APPROVED_WAITING in shown
    assert "Approve" not in shown

    child_browser = await enrol_and_sign_in(env, child, "kid1")
    assert (await person_row(env, child)).status == "active"
    assert (await child_browser.get("/")).status_code == 200


async def test_a_second_approval_of_an_invited_child_is_refused_as_already_granted(env: Env):
    await admin_client(env)
    parent = await create_person(env, "Mum", "parent")
    child = await create_person(env, "Kid", "child")
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303
    parent_browser = await enrol_and_sign_in(env, parent, "mum1")
    assert (await accept_current(env, parent_browser)).status_code == 303
    token = (await form(env, "/children", client=parent_browser))["csrf"]
    assert (await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})).status_code == 303

    again = await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})

    assert again.status_code == 403
    assert text.GRANT_REFUSED["already_granted"] in again.text
    assert await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM parent_grant").fetchone()[0]) == 1


# A grant counts only while its parent is linked: the administrator swaps one parent for another ---------------------

async def _mum_granted_then_unlinked(env: Env) -> tuple[int, int, int]:
    """Mum (linked, accepted) approves the invited child, then the administrator unlinks Mum. Returns the ids of Mum,
    Dad and the child; the caller links Dad."""
    await admin_client(env)
    mum = await create_person(env, "Mum", "parent")
    dad = await create_person(env, "Dad", "parent")
    child = await create_person(env, "Kid", "child")
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=mum)).status_code == 303
    mum_browser = await enrol_and_sign_in(env, mum, "mum1")
    assert (await accept_current(env, mum_browser)).status_code == 303
    token = (await form(env, "/children", client=mum_browser))["csrf"]
    assert (await mum_browser.post(f"/children/{child}/grant", data={"csrf": token})).status_code == 303
    assert (await post(env, f"/admin/people/{child}/parents/{mum}/remove")).status_code == 303
    return mum, dad, child


async def test_a_linked_parent_can_approve_after_an_unlinked_parent_already_granted(env: Env):
    _, dad, child = await _mum_granted_then_unlinked(env)
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=dad)).status_code == 303
    dad_browser = await enrol_and_sign_in(env, dad, "dad1")
    assert (await accept_current(env, dad_browser)).status_code == 303

    waiting = (await dad_browser.get("/children")).text
    assert "Kid" in waiting and "Approve" in waiting
    assert text.GRANT_APPROVED_WAITING not in waiting

    token = (await form(env, "/children", client=dad_browser))["csrf"]
    done = await dad_browser.post(f"/children/{child}/grant", data={"csrf": token})

    assert done.status_code == 303 and done.headers["location"] == "/children"
    assert (await person_row(env, child)).status == "invited"
    shown = (await dad_browser.get("/children")).text
    assert text.GRANT_APPROVED_WAITING in shown and "Approve" not in shown


async def test_an_unlinked_parents_grant_leaves_the_child_awaiting_a_grant_at_sign_in(env: Env):
    _, _, child = await _mum_granted_then_unlinked(env)

    child_browser = await enrol_and_sign_in(env, child, "kid1")

    assert (await person_row(env, child)).status == "awaiting_grant"
    assert (await child_browser.get("/")).status_code == 200


async def test_a_bound_invited_child_with_a_grant_can_be_approved_again_and_becomes_active(env: Env):
    parent, child, parent_browser, child_browser = await family(env)
    token = (await form(env, "/children", client=parent_browser))["csrf"]
    assert (await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})).status_code == 303
    await env.db.run(lambda c: c.execute("UPDATE person SET status = 'invited' WHERE id = ?", (child,)))

    waiting = (await parent_browser.get("/children")).text
    assert "Kid" in waiting and "Approve" in waiting
    assert text.GRANT_APPROVED_WAITING not in waiting

    token = (await form(env, "/children", client=parent_browser))["csrf"]
    done = await parent_browser.post(f"/children/{child}/grant", data={"csrf": token})

    assert done.status_code == 303
    assert (await person_row(env, child)).status == "active"


async def test_a_locked_parent_does_not_block_another_linked_parents_approval_on_the_page(env: Env):
    await admin_client(env)
    mum = await create_person(env, "Mum", "parent")
    dad = await create_person(env, "Dad", "parent")
    child = await create_person(env, "Kid", "child")
    for parent in (mum, dad):
        assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303
    mum_browser = await enrol_and_sign_in(env, mum, "mum1")
    dad_browser = await enrol_and_sign_in(env, dad, "dad1")
    assert (await accept_current(env, mum_browser)).status_code == 303
    assert (await accept_current(env, dad_browser)).status_code == 303
    token = (await form(env, "/children", client=mum_browser))["csrf"]
    assert (await mum_browser.post(f"/children/{child}/grant", data={"csrf": token})).status_code == 303

    assert (await post(env, f"/admin/people/{mum}/lock")).status_code == 303
    waiting = (await dad_browser.get("/children")).text
    assert "Approve" in waiting
    token = (await form(env, "/children", client=dad_browser))["csrf"]
    done = await dad_browser.post(f"/children/{child}/grant", data={"csrf": token})

    assert done.status_code == 303
    assert (await person_row(env, child)).status == "invited"


async def in_fluxer_group(env: Env, person_id: int) -> bool:
    row = await person_row(env, person_id)
    return row.authentik_pk in env.authentik.groups[FLUXER_GROUP]


async def test_an_admin_turns_an_active_adult_into_a_child_who_waits_for_a_parents_grant(env: Env):
    await admin_client(env)
    admin = await env.db.run(lambda c: registry.get_by_uuid(c, ADMIN_SUB))
    fluxer = await env.db.run(lambda c: services.create_service(
        c, slug="fluxer", name="Fluxer", kind="oidc", adapter="fluxer", auto_suspendable=True, url=None,
        actor_id=admin.id, now=NOW))
    def grant_by_type(conn) -> None:
        for subject in ("adult", "child"):
            services.add_entitlement(conn, fluxer.id, "type", subject, "grant", actor_id=admin.id, now=NOW)

    await env.db.run(grant_by_type)
    ana = await create_person(env, "Ana", "adult")
    # A Fluxer address is needed for group membership; an adult with no address is never a member.
    assert (await post(env, f"/admin/people/{ana}", display_name="Ana", account_type="adult",
                       email="ana.a@example.invalid", phone="", fluxer_email_override="")).status_code == 303
    ana_browser = await enrol_and_sign_in(env, ana, "ana1")
    assert (await accept_current(env, ana_browser)).status_code == 303
    mum = await create_person(env, "Mum", "parent")
    mum_browser = await enrol_and_sign_in(env, mum, "mum1")
    assert (await accept_current(env, mum_browser)).status_code == 303
    await authentik_sync.sync_people(env.application.state.deps, [ana])
    assert await in_fluxer_group(env, ana)

    changed = await post(env, f"/admin/people/{ana}", display_name="Ana", account_type="child", email="", phone="",
                         fluxer_email_override="")

    assert changed.status_code == 303
    assert (await person_row(env, ana)).status == "awaiting_grant"
    assert not await in_fluxer_group(env, ana)
    logged = await env.db.run(lambda c: c.execute(
        "SELECT COUNT(*) FROM decision_log WHERE action = 'status_changed' AND target_person_id = ?",
        (ana,)).fetchone()[0])
    assert logged == 1
    assert (await post(env, f"/admin/people/{ana}/parents", parent_id=mum)).status_code == 303
    waiting = (await mum_browser.get("/children")).text
    assert "Ana" in waiting and "Approve" in waiting
