# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authentik sync of the Fluxer group, the fluxer_email attribute and the active flag: the wanted state, the calls
made, failures, and the sync that runs on each change through HTTP.

Covers hallpass.AC2.7 (the Fluxer sign-in part) and the sync-on-every-change rule."""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import pytest

from hallpass import authentik_sync, grants, holds, registry, services
from hallpass.registry import Person
from tests.helpers import (
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
)


def deps(env: Env):
    return env.application.state.deps


async def make_fluxer(env: Env, *, retired: bool = False) -> services.Service:
    """The Fluxer service as the seed data has it: OIDC, auto-suspendable, granted to children and adults."""
    def change(conn) -> services.Service:
        admin = registry.create_person(conn, display_name="Admin", account_type="adult", now=NOW)
        service = services.create_service(conn, slug="fluxer", name="Fluxer", kind="oidc", adapter="fluxer",
                                          auto_suspendable=True, url=None, actor_id=admin.id, now=NOW)
        for subject in ("child", "adult"):
            services.add_entitlement(conn, service.id, "type", subject, "grant", actor_id=admin.id, now=NOW)
        if retired:
            services.retire_service(conn, service.id, actor_id=admin.id, now=NOW)
        return services.get(conn, service.id)

    return await env.db.run(change)


async def linked_child(env: Env, *, child_email: str | None = None, parent_email: str | None = None,
                       granted: bool = True) -> tuple[Person, Person, httpx.AsyncClient]:
    """A child linked to one parent, the child signed in and, with `granted`, approved by the parent. Returns the
    child, the parent and the parent's browser."""
    parent, parent_browser = await signed_in_person(env, account_type="parent", display_name="Mum",
                                                    email=parent_email)
    child, _ = await signed_in_person(env, account_type="child", display_name="Kid", email=child_email)
    await env.db.run(lambda c: registry.link_parent(c, child.id, parent.id, NOW))
    if granted:
        await env.db.run(lambda c: grants.grant(c, child_id=child.id, parent_id=parent.id, now=NOW))
    child = await env.db.run(lambda c: registry.get_person(c, child.id))
    return child, parent, parent_browser


def authentik_user(env: Env, person: Person) -> dict:
    return env.authentik.users[person.authentik_pk]


def in_group(env: Env, person: Person) -> bool:
    return person.authentik_pk in env.authentik.groups[FLUXER_GROUP]


async def sync(env: Env, *person_ids: int) -> None:
    await authentik_sync.sync_people(deps(env), list(person_ids))


# wanted ------------------------------------------------------------------------------------------------

async def test_wanted_is_not_a_member_without_a_fluxer_service(env: Env) -> None:
    adult, _ = await signed_in_person(env, account_type="adult", email="dee@example.com")
    member, address, active = await env.db.run(
        lambda c: authentik_sync.wanted(c, adult, None))
    assert (member, address, active) == (False, "dee@example.com", True)


# AC2.7 -------------------------------------------------------------------------------------------------

async def test_child_with_no_address_is_not_a_member_and_has_no_attribute(env: Env) -> None:
    await make_fluxer(env)
    child, _, _ = await linked_child(env)
    await sync(env, child.id)
    assert not in_group(env, child)
    assert "fluxer_email" not in authentik_user(env, child)["attributes"]
    assert authentik_user(env, child)["is_active"] is True


async def test_parent_email_then_sync_of_affected_people_adds_the_child_with_plus_address(env: Env) -> None:
    await make_fluxer(env)
    child, parent, _ = await linked_child(env)
    await env.db.run(lambda c: registry.update_person(
        c, parent.id, display_name="Mum", account_type="parent", email="mum@example.com", phone=None,
        fluxer_email_override=None))
    affected = await env.db.run(lambda c: authentik_sync.affected_by(c, parent.id))
    assert child.id in affected
    await sync(env, *affected)
    username = authentik_user(env, child)["username"]
    assert in_group(env, child)
    assert authentik_user(env, child)["attributes"]["fluxer_email"] == f"mum+{username}@example.com"


# Held and locked ---------------------------------------------------------------------------------------

async def test_entitled_adult_is_a_member_until_a_hold_and_a_sync_remove_them(env: Env) -> None:
    fluxer = await make_fluxer(env)
    adult, _ = await signed_in_person(env, account_type="adult", email="dee@example.com")
    await sync(env, adult.id)
    assert in_group(env, adult)

    def hold(conn) -> None:
        admin = registry.create_person(conn, display_name="Admin", account_type="adult", now=NOW)
        holds.place(conn, actor=holds.Actor(kind="admin", person_id=admin.id), target_id=adult.id,
                    service_ids=[fluxer.id], now=NOW, window=timedelta(hours=24), admin_email=None)

    await env.db.run(hold)
    await sync(env, adult.id)
    assert not in_group(env, adult)


async def test_locked_person_is_set_inactive_and_leaves_the_group_on_sync(env: Env) -> None:
    await make_fluxer(env)
    adult, _ = await signed_in_person(env, account_type="adult", email="dee@example.com")
    await sync(env, adult.id)
    await env.db.run(lambda c: registry.set_locked(c, adult.id, True))
    await sync(env, adult.id)
    assert authentik_user(env, adult)["is_active"] is False
    assert not in_group(env, adult)


# Failures and missing services -------------------------------------------------------------------------

async def test_failed_authentik_call_is_logged_and_does_not_raise(
        env: Env, caplog: pytest.LogCaptureFixture) -> None:
    await make_fluxer(env)
    adult, _ = await signed_in_person(env, account_type="adult", email="dee@example.com")
    env.authentik.fail_next = 500
    caplog.set_level(logging.INFO)
    await authentik_sync.sync_person(deps(env), adult.id)
    records = [record for record in caplog.records if record.getMessage() == "authentik_sync_failed"]
    assert len(records) == 1
    fields = records[0].hallpass_fields
    assert fields["person_id"] == adult.id
    assert fields["reason"] == "http"
    assert "dee@example.com" not in str(fields)


async def test_sync_reports_whether_authentik_now_matches(env: Env) -> None:
    await make_fluxer(env)
    adult, _ = await signed_in_person(env, account_type="adult", email="dee@example.com")
    assert await authentik_sync.sync_people(deps(env), [adult.id]) is True
    env.authentik.fail_next = 500
    assert await authentik_sync.sync_people(deps(env), [adult.id]) is False


async def test_a_failed_attribute_call_still_removes_a_held_adult_from_the_group(
        env: Env, caplog: pytest.LogCaptureFixture) -> None:
    fluxer = await make_fluxer(env)
    adult, _ = await signed_in_person(env, account_type="adult", email="dee@example.com")
    await sync(env, adult.id)
    assert in_group(env, adult)

    def hold(conn) -> None:
        admin = registry.create_person(conn, display_name="Admin", account_type="adult", now=NOW)
        holds.place(conn, actor=holds.Actor(kind="admin", person_id=admin.id), target_id=adult.id,
                    service_ids=[fluxer.id], now=NOW, window=timedelta(hours=24), admin_email=None)

    await env.db.run(hold)
    # A hold sync makes the group removal first, then the attribute read (inside update_attributes), then the
    # activation. The removal passes; the attribute read is the next request, and it fails.
    env.authentik.fail_skip = 1
    env.authentik.fail_next = 500
    caplog.set_level(logging.INFO)

    assert await authentik_sync.sync_person(deps(env), adult.id) is False

    assert not in_group(env, adult)
    assert authentik_user(env, adult)["is_active"] is True
    failures = [record.hallpass_fields for record in caplog.records
                if record.getMessage() == "authentik_sync_failed"]
    assert [(f["person_id"], f["action"], f["http_status"]) for f in failures] == [(adult.id, "update_attributes", 500)]


async def test_a_lock_whose_sync_fails_still_ends_sessions_and_shows_the_warning(env: Env) -> None:
    await admin_client(env)
    await make_fluxer(env)
    adult, _ = await signed_in_person(env, account_type="adult", email="dee@example.com")
    await sync(env, adult.id)
    env.authentik.sessions[adult.authentik_pk] = ["session-1"]
    csrf = await form(env, f"/admin/people/{adult.id}")
    # The lock's sync makes the group removal, then the deactivation, then the attribute calls. The removal passes and
    # the deactivation fails, so the fake never ends the sessions itself: only the route's end_sessions can.
    env.authentik.fail_skip = 1
    env.authentik.fail_next = 500

    response = await env.client.post(f"/admin/people/{adult.id}/lock", data=csrf)

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/people/{adult.id}?warning=authentik"
    assert env.authentik.sessions.get(adult.authentik_pk, []) == []
    assert (await env.db.run(lambda c: registry.get_person(c, adult.id))).status == "locked"


@pytest.mark.parametrize("retired", [False, True], ids=["missing", "retired"])
async def test_without_a_live_fluxer_service_nobody_is_a_member_but_fields_are_written(env: Env,
                                                                                     retired: bool) -> None:
    if retired:
        await make_fluxer(env, retired=True)
    adult, _ = await signed_in_person(env, account_type="adult", email="dee@example.com")
    await sync(env, adult.id)
    assert not in_group(env, adult)
    assert authentik_user(env, adult)["attributes"]["fluxer_email"] == "dee@example.com"
    assert authentik_user(env, adult)["is_active"] is True


async def test_resync_all_syncs_every_bound_person_and_skips_unbound_ones(env: Env) -> None:
    await make_fluxer(env)
    adult, _ = await signed_in_person(env, account_type="adult", email="dee@example.com")
    await env.db.run(lambda c: registry.create_person(c, display_name="Unbound", account_type="adult", now=NOW))
    await authentik_sync.resync_all(deps(env))
    assert in_group(env, adult)
    assert set(env.authentik.groups[FLUXER_GROUP]) == {adult.authentik_pk}


# Sync on every change, through HTTP ---------------------------------------------------------------------

async def test_a_parent_grant_makes_the_child_a_member_without_a_resync(env: Env) -> None:
    await make_fluxer(env)
    child, _, parent_browser = await linked_child(env, child_email="kid@example.com", granted=False)
    assert not in_group(env, child)

    token = (await form(env, "/children", client=parent_browser))["csrf"]
    response = await parent_browser.post(f"/children/{child.id}/grant", data={"csrf": token})
    assert response.status_code == 303
    assert in_group(env, child)


async def test_removing_the_child_type_entitlement_removes_the_child_from_the_group(env: Env) -> None:
    fluxer = await make_fluxer(env)
    child, _, _ = await linked_child(env, child_email="kid@example.com")
    await sync(env, child.id)
    assert in_group(env, child)

    admin = await admin_client(env)
    assert admin.status_code == 303
    entitlement = await env.db.run(lambda c: next(
        e.id for e in services.entitlements_for(c, fluxer.id) if e.subject == "child"))
    token = (await form(env, f"/admin/services/{fluxer.id}"))["csrf"]
    response = await env.client.post(f"/admin/entitlements/{entitlement}/remove", data={"csrf": token})
    assert response.status_code == 303
    assert not in_group(env, child)


async def test_admin_setting_a_parent_email_adds_the_child_with_the_plus_address(env: Env) -> None:
    await make_fluxer(env)
    child, parent, _ = await linked_child(env)
    assert not in_group(env, child)

    await admin_client(env)
    token = (await form(env, f"/admin/people/{parent.id}"))["csrf"]
    response = await env.client.post(f"/admin/people/{parent.id}", data={
        "csrf": token, "display_name": "Mum", "account_type": "parent", "email": "mum@example.com",
        "phone": "", "fluxer_email_override": "",
    })
    assert response.status_code == 303
    username = authentik_user(env, child)["username"]
    assert in_group(env, child)
    assert authentik_user(env, child)["attributes"]["fluxer_email"] == f"mum+{username}@example.com"


async def test_a_parent_accepting_the_terms_syncs_the_linked_child_into_the_fluxer_group(env: Env) -> None:
    # The child is covered only by the parent's acceptance. A new blocking version uncovers both until the parent
    # accepts it, and that acceptance must sync the child, whose group membership changes with it.
    await make_fluxer(env)
    child, _, parent_browser = await linked_child(env, parent_email="mum@example.com")
    await sync(env, child.id)
    assert in_group(env, child)

    await publish_terms(env, blocking=True)
    await sync(env, child.id)
    assert not in_group(env, child)

    assert (await accept_current(env, parent_browser)).status_code == 303
    assert in_group(env, child)


# A bound child set back to invited is settled at sign-in, and Authentik follows ----------------------------------

async def set_status(env: Env, person_id: int, status: str) -> None:
    await env.db.run(lambda c: c.execute("UPDATE person SET status = ? WHERE id = ?", (status, person_id)))


async def test_a_bound_child_set_back_to_invited_is_awaiting_and_out_of_the_group_after_sign_in(env: Env) -> None:
    await make_fluxer(env)
    child, _ = await signed_in_person(env, account_type="child", email="kid@example.com")
    assert not in_group(env, child)
    env.authentik.groups[FLUXER_GROUP].add(child.authentik_pk)   # Authentik still lists the child: a stale state
    await set_status(env, child.id, "invited")

    response = await sign_in(env, sub=child.authentik_uuid, username=authentik_user(env, child)["username"],
                             client=new_client(env))

    assert response.status_code == 303
    settled = await env.db.run(lambda c: registry.get_person(c, child.id))
    assert settled.status == "awaiting_grant"
    assert not in_group(env, settled)


async def test_a_bound_child_set_back_to_invited_with_a_grant_is_active_and_in_the_group_after_sign_in(
        env: Env) -> None:
    await make_fluxer(env)
    child, _, _ = await linked_child(env, child_email="kid@example.com")
    await sync(env, child.id)
    assert in_group(env, child)
    env.authentik.groups[FLUXER_GROUP].discard(child.authentik_pk)   # Authentik lost the child: a stale state
    await set_status(env, child.id, "invited")

    response = await sign_in(env, sub=child.authentik_uuid, username=authentik_user(env, child)["username"],
                             client=new_client(env))

    assert response.status_code == 303
    settled = await env.db.run(lambda c: registry.get_person(c, child.id))
    assert settled.status == "active"
    assert in_group(env, settled)


async def test_locking_the_only_covering_parent_takes_the_child_out_of_the_fluxer_group(env: Env) -> None:
    await make_fluxer(env)
    child, parent, _ = await linked_child(env, child_email="kid@example.com")
    await sync(env, child.id)
    assert in_group(env, child)

    await admin_client(env)
    token = (await form(env, f"/admin/people/{parent.id}"))["csrf"]
    response = await env.client.post(f"/admin/people/{parent.id}/lock", data={"csrf": token})

    assert response.status_code == 303
    assert not in_group(env, child)


async def test_unlinking_the_only_covering_parent_takes_the_child_out_of_the_fluxer_group(env: Env) -> None:
    await make_fluxer(env)
    child, parent, _ = await linked_child(env, child_email="kid@example.com")
    await sync(env, child.id)
    assert in_group(env, child)

    await admin_client(env)
    token = (await form(env, f"/admin/people/{child.id}"))["csrf"]
    response = await env.client.post(f"/admin/people/{child.id}/parents/{parent.id}/remove",
                                     data={"csrf": token})

    assert response.status_code == 303
    assert not in_group(env, child)
