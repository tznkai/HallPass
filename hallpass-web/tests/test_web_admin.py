# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin pages, driven through HTTP as the admin: people, invitations, locking, links, identities and groups.

Covers hallpass.AC1.1, AC1.4, AC1.6 (Authentik part), AC2.1 to AC2.7.
"""

from __future__ import annotations

import re

import httpx
import pytest

from hallpass import registry, text
from tests.fake_authentik import InvitationUsed, add_user, enrol
from tests.helpers import BASE_URL, Env, admin_client, api_routes, form, make_service, sign_in, signed_in_person

OTHER_SUB = "00000000-0000-4000-8000-0000000000c2"
INVITE_MARK = "/if/flow/invite/?itoken="


async def post(env: Env, path: str, **fields) -> httpx.Response:
    data = await form(env, "/admin/people", **{key: str(value) for key, value in fields.items()})
    return await env.client.post(path, data=data)


def new_id(response: httpx.Response) -> int:
    assert response.status_code == 303, response.status_code
    return int(response.headers["location"].rstrip("/").rsplit("/", 1)[1])


async def person_row(env: Env, person_id: int) -> registry.Person:
    return await env.db.run(lambda c: registry.get_person(c, person_id))


async def create_person(env: Env, name: str, account_type: str = "adult", email: str = "") -> int:
    return new_id(await post(env, "/admin/people", display_name=name, account_type=account_type, email=email,
                             phone=""))


async def bind_child(env: Env, person_id: int, username: str) -> dict:
    """Follows the person's invitation as a new Authentik user and signs in. Leaves the child signed in."""
    row = await person_row(env, person_id)
    user = enrol(env.authentik, row.invitation_pk, username)
    response = await sign_in(env, sub=user["uuid"], username=username,
                             enrollment_ref=user["attributes"]["hallpass_enrollment"])
    assert response.status_code == 303
    return user


# AC1.1 and AC1.4: invitations ----------------------------------------------------------------------------

async def test_creating_a_person_issues_a_single_use_invitation_that_enrols_them(env: Env):
    await admin_client(env)
    person_id = await create_person(env, "Kid One", account_type="child")

    shown = (await env.client.get(f"/admin/people/{person_id}")).text
    assert INVITE_MARK in shown
    [(invitation_pk, invitation)] = env.authentik.invitations.items()
    assert invitation["single_use"] is True
    row = await person_row(env, person_id)
    assert invitation["fixed_data"] == {"attributes.hallpass_enrollment": row.enrollment_ref}

    user = enrol(env.authentik, invitation_pk, "kid1")
    response = await sign_in(env, sub=user["uuid"], username="kid1",
                             enrollment_ref=user["attributes"]["hallpass_enrollment"])
    assert response.status_code == 303
    await admin_client(env)
    assert INVITE_MARK not in (await env.client.get(f"/admin/people/{person_id}")).text
    assert (await person_row(env, person_id)).authentik_uuid == user["uuid"]


async def test_an_invitation_cannot_be_used_twice_and_a_copied_ref_matches_nothing(env: Env):
    await admin_client(env)
    await create_person(env, "Kid", account_type="child")
    [invitation_pk] = env.authentik.invitations
    user = enrol(env.authentik, invitation_pk, "kid1")
    with pytest.raises(InvitationUsed):
        enrol(env.authentik, invitation_pk, "kid2")
    ref = user["attributes"]["hallpass_enrollment"]
    await sign_in(env, sub=user["uuid"], username="kid1", enrollment_ref=ref)

    other = add_user(env.authentik, uuid=OTHER_SUB, username="kid3", attributes={"hallpass_enrollment": ref})
    response = await sign_in(env, sub=other["uuid"], username="kid3", enrollment_ref=ref)
    assert response.status_code == 403
    assert text.NOT_REGISTERED in response.text


async def test_regenerating_an_invitation_retires_the_old_link(env: Env):
    await admin_client(env)
    person_id = await create_person(env, "Kid", account_type="child")
    [old_pk] = env.authentik.invitations
    old_ref = (await person_row(env, person_id)).enrollment_ref

    assert (await post(env, f"/admin/people/{person_id}/invite")).status_code == 303

    assert old_pk not in env.authentik.invitations
    [new_pk] = env.authentik.invitations
    new_ref = (await person_row(env, person_id)).enrollment_ref
    assert new_ref != old_ref
    with pytest.raises(InvitationUsed):
        enrol(env.authentik, old_pk, "kid0")
    user = enrol(env.authentik, new_pk, "kid1")
    response = await sign_in(env, sub=user["uuid"], username="kid1", enrollment_ref=new_ref)
    assert response.status_code == 303


async def test_a_failed_invitation_leaves_the_person_and_offers_a_retry(env: Env):
    await admin_client(env)
    env.authentik.fail_next = 500
    person_id = await create_person(env, "Kid", account_type="child")

    page = (await env.client.get(f"/admin/people/{person_id}")).text
    assert text.INVITATION_NOT_CREATED in page
    assert INVITE_MARK not in page
    assert (await person_row(env, person_id)).invitation_pk is None

    assert (await post(env, f"/admin/people/{person_id}/invite")).status_code == 303
    assert INVITE_MARK in (await env.client.get(f"/admin/people/{person_id}")).text


async def test_binding_during_a_regeneration_keeps_the_identity_and_discards_the_new_invitation(
        env: Env, monkeypatch: pytest.MonkeyPatch):
    await admin_client(env)
    person_id = await create_person(env, "Kid", account_type="child")
    [old_pk] = env.authentik.invitations
    create = env.authentik_client.create_invitation

    async def bind_while_the_new_invitation_is_made(**kwargs):
        # The person follows the old link in the window between the unbound check and the store.
        user = enrol(env.authentik, old_pk, "kid1")
        await env.db.run(lambda c: registry.bind_identity(c, person_id, uuid=user["uuid"], authentik_pk=user["pk"],
                                                          username="kid1"))
        return await create(**kwargs)

    monkeypatch.setattr(env.authentik_client, "create_invitation", bind_while_the_new_invitation_is_made)
    assert (await post(env, f"/admin/people/{person_id}/invite")).status_code == 303

    bound = next(user for user in env.authentik.users.values() if user["username"] == "kid1")
    row = await person_row(env, person_id)
    assert row.authentik_uuid == bound["uuid"]
    assert row.enrollment_ref is None and row.invitation_pk is None and row.invite_url is None
    assert env.authentik.invitations == {}, "the invitation made during binding must be deleted"
    assert await logged(env, "invitation_created") == 1


# AC1.6: locking ---------------------------------------------------------------------------------------

async def test_locking_blocks_sign_in_and_unlocking_restores_it(env: Env):
    await admin_client(env)
    person_id = await create_person(env, "Kid", account_type="child")
    user = await bind_child(env, person_id, "kid1")
    await admin_client(env)
    env.authentik.sessions[user["pk"]] = ["s1", "s2"]

    assert (await post(env, f"/admin/people/{person_id}/lock")).status_code == 303

    assert env.authentik.users[user["pk"]]["is_active"] is False
    assert env.authentik.sessions.get(user["pk"], []) == []
    refused = await sign_in(env, sub=user["uuid"], username="kid1")
    assert refused.status_code == 403
    assert text.LOCKED in refused.text

    await admin_client(env)
    assert (await post(env, f"/admin/people/{person_id}/unlock")).status_code == 303
    assert env.authentik.users[user["pk"]]["is_active"] is True
    assert (await person_row(env, person_id)).status == "invited"


async def test_a_lock_survives_an_authentik_outage_with_a_warning(env: Env):
    await admin_client(env)
    person_id = await create_person(env, "Kid", account_type="child")
    user = await bind_child(env, person_id, "kid1")
    await admin_client(env)
    env.authentik.fail_next = 500

    response = await post(env, f"/admin/people/{person_id}/lock")

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/people/{person_id}?warning=authentik"
    assert (await person_row(env, person_id)).status == "locked"
    # The first call of the sync (the group removal) is the one refused. The deactivation is a separate step and still
    # runs, so the lock is enforced in Authentik even though the sync as a whole reports the failure.
    assert env.authentik.users[user["pk"]]["is_active"] is False
    shown = (await env.client.get(response.headers["location"])).text
    assert text.AUTHENTIK_NOT_UPDATED in shown


# AC2.1: people ----------------------------------------------------------------------------------------

async def test_the_admin_creates_and_edits_a_person(env: Env):
    await admin_client(env)
    person_id = await create_person(env, "Ana", email="ana@parent.invalid")

    response = await post(env, f"/admin/people/{person_id}", display_name="Ana B", account_type="parent",
                          email="ana.b@parent.invalid", phone="", fluxer_email_override="")

    assert response.status_code == 303
    row = await person_row(env, person_id)
    assert (row.display_name, row.account_type, row.email) == ("Ana B", "parent", "ana.b@parent.invalid")
    shown = (await env.client.get(f"/admin/people/{person_id}")).text
    assert "Ana B" in shown and "ana.b@parent.invalid" in shown


async def test_an_empty_name_is_refused_and_nothing_is_created(env: Env):
    await admin_client(env)
    before = await env.db.run(registry.list_people)
    response = await post(env, "/admin/people", display_name="  ", account_type="adult", email="", phone="")
    assert response.status_code == 400
    assert text.INVALID_INPUT in response.text
    assert await env.db.run(registry.list_people) == before


async def test_admin_changes_are_written_to_the_decision_log(env: Env):
    await admin_client(env)
    person_id = await create_person(env, "Kid", account_type="child")
    rows = await env.db.run(lambda c: c.execute(
        "SELECT action, actor_kind, target_person_id FROM decision_log ORDER BY id").fetchall())
    assert ("person_created", "admin", person_id) in rows


# AC2.2: parent links ----------------------------------------------------------------------------------

async def test_a_child_is_linked_to_three_parents_and_one_is_unlinked(env: Env):
    await admin_client(env)
    child = await create_person(env, "Kid", account_type="child")
    parents = [await create_person(env, f"Parent {n}") for n in range(3)]
    for parent in parents:
        assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303
    linked = await env.db.run(lambda c: registry.parents_of(c, child))
    assert {p.id for p in linked} == set(parents)

    assert (await post(env, f"/admin/people/{child}/parents/{parents[1]}/remove")).status_code == 303

    left = await env.db.run(lambda c: registry.parents_of(c, child))
    assert {p.id for p in left} == {parents[0], parents[2]}


async def test_an_adult_can_gain_a_child_from_the_parent_page(env: Env):
    await admin_client(env)
    parent = await create_person(env, "Ana")
    child = await create_person(env, "Kid", account_type="child")
    assert (await post(env, f"/admin/people/{parent}/children", child_id=child)).status_code == 303
    kids = await env.db.run(lambda c: registry.children_of(c, parent))
    assert [k.id for k in kids] == [child]


# AC2.6: link refusals ---------------------------------------------------------------------------------

async def test_an_adult_cannot_be_linked_as_a_child_and_nothing_is_stored(env: Env):
    await admin_client(env)
    adult = await create_person(env, "Ana")
    parent = await create_person(env, "Ben")

    response = await post(env, f"/admin/people/{adult}/parents", parent_id=parent)

    assert response.status_code == 400
    assert text.LINK_REFUSED_CHILD_TYPE in response.text
    assert await env.db.run(lambda c: registry.parents_of(c, adult)) == []


async def test_a_child_cannot_be_linked_as_a_parent(env: Env):
    await admin_client(env)
    kid = await create_person(env, "Kid", account_type="child")
    other = await create_person(env, "Other", account_type="child")
    response = await post(env, f"/admin/people/{kid}/parents", parent_id=other)
    assert response.status_code == 400
    assert text.LINK_REFUSED_PARENT_TYPE in response.text


async def test_a_parent_cannot_become_a_child_while_linked_children_exist(env: Env):
    await admin_client(env)
    parent = await create_person(env, "Ana")
    child = await create_person(env, "Kid", account_type="child")
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303

    response = await post(env, f"/admin/people/{parent}", display_name="Ana", account_type="child", email="",
                          phone="", fluxer_email_override="")

    assert response.status_code == 400
    assert text.TYPE_CHANGE_REFUSED_IS_PARENT in response.text
    assert (await person_row(env, parent)).account_type == "adult"


# Removals: unknown ids and links that do not exist -----------------------------------------------------

async def logged(env: Env, action: str) -> int:
    return await env.db.run(lambda c: c.execute(
        "SELECT COUNT(*) FROM decision_log WHERE action = ?", (action,)).fetchone()[0])


async def test_removing_with_an_unknown_id_is_not_found_and_logs_nothing(env: Env):
    await admin_client(env)
    child = await create_person(env, "Kid", account_type="child")
    parent = await create_person(env, "Ana")
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303
    group = new_id(await post(env, "/admin/groups", name="Lunch club"))
    before = await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0])

    for path in [
        f"/admin/people/999/parents/{parent}/remove",
        f"/admin/people/{child}/parents/999/remove",
        f"/admin/groups/{group}/members/999/remove",
        f"/admin/groups/999/members/{child}/remove",
    ]:
        response = await post(env, path)
        assert response.status_code == 404, path
        assert text.NOT_FOUND in response.text

    after = await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0])
    assert after == before
    assert [p.id for p in await env.db.run(lambda c: registry.parents_of(c, child))] == [parent]


async def test_removing_a_link_or_membership_that_is_not_there_logs_nothing(env: Env):
    await admin_client(env)
    child = await create_person(env, "Kid", account_type="child")
    parent = await create_person(env, "Ana")
    group = new_id(await post(env, "/admin/groups", name="Lunch club"))

    assert (await post(env, f"/admin/people/{child}/parents/{parent}/remove")).status_code == 303
    assert (await post(env, f"/admin/groups/{group}/members/{parent}/remove")).status_code == 303
    assert await logged(env, "parent_unlinked") == 0
    assert await logged(env, "member_removed") == 0


async def test_removing_a_real_link_and_membership_logs_each_once(env: Env):
    await admin_client(env)
    child = await create_person(env, "Kid", account_type="child")
    parent = await create_person(env, "Ana")
    group = new_id(await post(env, "/admin/groups", name="Lunch club"))
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303
    assert (await post(env, f"/admin/groups/{group}/members", person_id=parent)).status_code == 303

    assert (await post(env, f"/admin/people/{child}/parents/{parent}/remove")).status_code == 303
    assert (await post(env, f"/admin/groups/{group}/members/{parent}/remove")).status_code == 303
    assert (await post(env, f"/admin/people/{child}/parents/{parent}/remove")).status_code == 303

    assert await logged(env, "parent_unlinked") == 1
    assert await logged(env, "member_removed") == 1


# AC2.3: groups and identities -------------------------------------------------------------------------

async def test_the_admin_manages_groups_and_their_members(env: Env):
    await admin_client(env)
    group = new_id(await post(env, "/admin/groups", name="Lunch club"))
    member = await create_person(env, "Ana")

    assert (await post(env, f"/admin/groups/{group}/members", person_id=member)).status_code == 303
    assert [m.id for m in await env.db.run(lambda c: registry.members(c, group))] == [member]
    assert "Lunch club" in (await env.client.get(f"/admin/groups/{group}")).text

    assert (await post(env, f"/admin/groups/{group}/members/{member}/remove")).status_code == 303
    assert await env.db.run(lambda c: registry.members(c, group)) == []

    assert (await post(env, f"/admin/groups/{group}/delete")).status_code == 303
    assert await env.db.run(registry.list_groups) == []


async def test_a_duplicate_group_name_is_refused(env: Env):
    await admin_client(env)
    assert (await post(env, "/admin/groups", name="Lunch club")).status_code == 303
    response = await post(env, "/admin/groups", name="Lunch club")
    assert response.status_code == 400
    assert text.GROUP_TAKEN in response.text


async def test_the_admin_records_and_removes_external_identities(env: Env):
    await admin_client(env)
    person_id = await create_person(env, "Kid", account_type="child")
    for slug in ("fluxer", "minecraft", "bedrock", "steam"):
        await make_service(env.db, slug)
    entries = [("fluxer", "fluxer_user_id", "1001"), ("minecraft", "java_name", "BuilderSeven"),
               ("bedrock", "bedrock_gamertag", "GamerOne"), ("steam", "steam_id", str(registry.STEAM64_BASE + 1001))]
    for slug, kind, value in entries:
        response = await post(env, f"/admin/people/{person_id}/identities", service_slug=slug, kind=kind, value=value)
        assert response.status_code == 303

    stored = await env.db.run(lambda c: registry.identities_of(c, person_id))
    assert sorted(i.kind for i in stored) == sorted(kind for _, kind, _ in entries)

    removed = stored[0]
    response = await post(env, f"/admin/identities/{removed.id}/remove")
    assert response.headers["location"] == f"/admin/people/{person_id}"
    left = await env.db.run(lambda c: registry.identities_of(c, person_id))
    assert removed.id not in {i.id for i in left}
    assert len(left) == 3


@pytest.mark.parametrize(("kind", "value"), [("java_name", "@a"), ("java_name", "A" * 17),
                                             ("bedrock_gamertag", "@e[type=player]"), ("bedrock_gamertag", "Ste ve")])
async def test_a_minecraft_identity_outside_the_pattern_is_refused_and_stores_nothing(env: Env, kind: str, value: str):
    await admin_client(env)
    person_id = await create_person(env, "Kid", account_type="child")
    await make_service(env.db, "minecraft")
    response = await post(env, f"/admin/people/{person_id}/identities", service_slug="minecraft", kind=kind,
                          value=value)
    assert response.status_code == 400
    assert text.INVALID_INPUT in response.text
    assert await env.db.run(lambda c: registry.identities_of(c, person_id)) == []


async def test_a_non_canonical_steam_id_is_refused_by_the_admin_route_and_stores_nothing(env: Env):
    await admin_client(env)
    person_id = await create_person(env, "Kid", account_type="child")
    await make_service(env.db, "steam")
    response = await post(env, f"/admin/people/{person_id}/identities", service_slug="steam", kind="steam_id",
                          value="0" + str(registry.STEAM64_BASE + 1001))
    assert response.status_code == 400
    assert text.INVALID_INPUT in response.text
    assert await env.db.run(lambda c: registry.identities_of(c, person_id)) == []


async def test_a_duplicate_identity_is_refused(env: Env):
    await admin_client(env)
    person_id = await create_person(env, "Kid", account_type="child")
    await make_service(env.db, "fluxer")
    assert (await post(env, f"/admin/people/{person_id}/identities", service_slug="fluxer",
                       kind="fluxer_user_id", value="1001")).status_code == 303
    response = await post(env, f"/admin/people/{person_id}/identities", service_slug="fluxer",
                          kind="fluxer_user_id", value="1001")
    assert response.status_code == 400
    assert text.IDENTITY_TAKEN in response.text


async def test_a_case_variant_minecraft_name_held_by_another_person_is_refused_with_the_same_message(env: Env):
    await admin_client(env)
    first = await create_person(env, "First", account_type="child")
    second = await create_person(env, "Second", account_type="child")
    await make_service(env.db, "minecraft")
    assert (await post(env, f"/admin/people/{first}/identities", service_slug="minecraft", kind="java_name",
                       value="PlayerOne")).status_code == 303
    response = await post(env, f"/admin/people/{second}/identities", service_slug="minecraft", kind="java_name",
                          value="playerone")
    assert response.status_code == 400
    assert text.IDENTITY_TAKEN in response.text
    assert await env.db.run(lambda c: registry.identities_of(c, second)) == []


# AC2.4: the Fluxer address ----------------------------------------------------------------------------

async def test_a_bound_child_shows_the_plus_address_until_an_override_is_set(env: Env):
    await admin_client(env)
    parent = await create_person(env, "Mum", email="mum@parent.invalid")
    child = await create_person(env, "Kid", account_type="child")
    assert (await post(env, f"/admin/people/{child}/parents", parent_id=parent)).status_code == 303
    await bind_child(env, child, "kid1")
    await admin_client(env)

    assert "mum+kid1@parent.invalid" in (await env.client.get(f"/admin/people/{child}")).text

    response = await post(env, f"/admin/people/{child}", display_name="Kid", account_type="child", email="",
                          phone="", fluxer_email_override="kid.override@parent.invalid")
    assert response.status_code == 303
    shown = (await env.client.get(f"/admin/people/{child}")).text
    assert "kid.override@parent.invalid" in shown


# AC2.7: the missing-address flag ----------------------------------------------------------------------

async def test_a_child_with_no_address_is_flagged_on_the_people_page(env: Env):
    await admin_client(env)
    child = await create_person(env, "Lonely", account_type="child")
    page = (await env.client.get("/admin/people")).text
    assert text.NO_FLUXER_ADDRESS_FLAG in page
    assert f"/admin/people/{child}" in page


# AC2.5: non-admins and anonymous visitors -------------------------------------------------------------

def admin_routes(env: Env) -> list[tuple[str, str]]:
    found = []
    for route in api_routes(env.application):
        if route.path.startswith("/admin"):
            for method in sorted(route.methods - {"HEAD"}):
                found.append((method, re.sub(r"\{[^}]+\}", "1", route.path)))
    return found


async def test_a_non_admin_gets_403_on_every_admin_path_and_method(env: Env):
    await admin_client(env)
    _, browser = await signed_in_person(env, account_type="adult", display_name="Ana")
    token = (await form(env, "/terms", client=browser))["csrf"]

    routes = admin_routes(env)
    assert routes, "no admin routes were found"
    for method, path in routes:
        if method == "GET":
            response = await browser.get(path)
        else:
            response = await browser.post(path, data={"csrf": token})
        assert response.status_code == 403, (method, path, response.status_code)


async def test_an_anonymous_visitor_is_sent_to_login_from_every_admin_page(env: Env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.application), base_url=BASE_URL,
                                 follow_redirects=False) as anonymous:
        for method, path in admin_routes(env):
            if method != "GET":
                continue
            response = await anonymous.get(path)
            assert response.status_code == 303, path
            assert response.headers["location"].startswith("/login"), path


async def test_the_admin_pages_are_reachable_for_the_admin(env: Env):
    await admin_client(env)
    for path in ("/admin/people", "/admin/groups"):
        assert (await env.client.get(path)).status_code == 200
