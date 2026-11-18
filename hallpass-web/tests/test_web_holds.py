# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The parent's hold controls and the personal page's hold labels, driven through HTTP.

Covers hallpass.AC6.1, AC6.4, AC6.5 (row text), AC6.6, AC7.7 and AC8.2 through the pages.
"""

from __future__ import annotations

import re
from datetime import timedelta

import httpx
import pytest
from markupsafe import escape

from hallpass import authentik_sync, grants, holds, registry, services, text
from hallpass.registry import Person
from tests.fake_authentik import FakeAuthentikState
from tests.helpers import (
    FLUXER_GROUP,
    NOW,
    Env,
    admin_client,
    form,
    section_of,
    signed_in_person,
)

WINDOW = timedelta(hours=24)


async def new_service(env: Env, slug: str, *, auto: bool, publisher: Person) -> services.Service:
    def change(conn) -> services.Service:
        return services.create_service(conn, slug=slug, name=slug.title(), kind="forward_auth",
                                       adapter="minecraft" if auto else "manual",
                                       auto_suspendable=auto, url=None, actor_id=publisher.id, now=NOW)

    service = await env.db.run(change)
    await env.db.run(lambda c: services.add_entitlement(c, service.id, "type", "child", "grant",
                                                        actor_id=publisher.id, now=NOW))
    return service


class World:
    """A child with two services (fluxer, auto-suspendable; abiotic, manual), two linked parents with emails, and an
    unrelated adult. Everyone is signed in on a browser of their own."""

    def __init__(self) -> None:
        self.ann: Person
        self.ann_browser: httpx.AsyncClient
        self.ben: Person
        self.ben_browser: httpx.AsyncClient
        self.cy: Person
        self.cy_browser: httpx.AsyncClient
        self.kid: Person
        self.kid_browser: httpx.AsyncClient
        self.fluxer: services.Service
        self.abiotic: services.Service


async def build_world(env: Env) -> World:
    world = World()
    world.ann, world.ann_browser = await signed_in_person(env, account_type="adult", display_name="Ann",
                                                          email="ann@example.com")
    world.ben, world.ben_browser = await signed_in_person(env, account_type="adult", display_name="Ben",
                                                          email="ben@example.com")
    world.cy, world.cy_browser = await signed_in_person(env, account_type="adult", display_name="Cy")
    world.kid, world.kid_browser = await signed_in_person(env, account_type="child", display_name="Kid")
    for parent in (world.ann, world.ben):
        await env.db.run(lambda c, p=parent: registry.link_parent(c, world.kid.id, p.id, NOW))
    # Ann approves the child, so the child is active and may be a Fluxer member.
    await env.db.run(lambda c: grants.grant(c, child_id=world.kid.id, parent_id=world.ann.id, now=NOW))
    world.fluxer = await new_service(env, "fluxer", auto=True, publisher=world.ann)
    world.abiotic = await new_service(env, "abiotic", auto=False, publisher=world.ann)
    return world


async def hold_pairs(env: Env, child: Person) -> dict[int, list[str]]:
    """service id -> the kinds of hold on it, for the child."""
    grouped = await env.db.run(lambda c: holds.services_with_holds(c, child.id))
    return {service_id: [hold.kind for hold in found] for service_id, found in grouped.items()}


async def post_as(env: Env, browser: httpx.AsyncClient, path: str, **fields: str) -> httpx.Response:
    data = await form(env, "/", client=browser, **fields)
    return await browser.post(path, data=data)


async def sync_state(env: Env, person: Person) -> bool:
    """Whether the person is a member of the Fluxer group in the fake Authentik, after a sync."""
    await authentik_sync.sync_people(env.application.state.deps, [person.id])
    state: FakeAuthentikState = env.authentik
    row = await env.db.run(lambda c: registry.get_person(c, person.id))
    return row.authentik_pk in state.groups[FLUXER_GROUP]


# AC6.1: revoke everything, or one service ------------------------------------------------------------

async def test_revoke_everything_places_a_hold_on_every_service_and_removes_the_child_from_fluxer(env: Env):
    world = await build_world(env)
    assert await sync_state(env, world.kid) is True

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all")

    assert response.status_code == 303
    assert response.headers["location"] == f"/children/{world.kid.id}"
    assert await hold_pairs(env, world.kid) == {world.fluxer.id: ["parent"], world.abiotic.id: ["parent"]}
    assert await sync_state(env, world.kid) is False


async def test_a_single_revoke_places_one_hold(env: Env):
    world = await build_world(env)

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds",
                             service_id=str(world.fluxer.id))

    assert response.status_code == 303
    assert await hold_pairs(env, world.kid) == {world.fluxer.id: ["parent"]}


async def test_a_malformed_service_id_is_refused_and_places_nothing(env: Env):
    world = await build_world(env)

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="fluxer")

    assert response.status_code == 400
    assert text.INVALID_INPUT in response.text
    assert await hold_pairs(env, world.kid) == {}


# AC6.4 and AC6.5: the controls and the row text ------------------------------------------------------------

async def test_restore_is_offered_only_on_the_auto_suspendable_service(env: Env):
    world = await build_world(env)
    await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all")

    page = (await world.ann_browser.get(f"/children/{world.kid.id}")).text

    assert f"/children/{world.kid.id}/holds/{world.fluxer.id}/lift" in page
    assert f"/children/{world.kid.id}/holds/{world.abiotic.id}/lift" not in page
    assert "cannot be automatically suspended" in page


async def test_a_held_manual_service_reads_cannot_be_automatically_suspended_on_both_pages(env: Env):
    world = await build_world(env)
    await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all")

    parent_page = (await world.ann_browser.get(f"/children/{world.kid.id}")).text
    child_page = (await world.kid_browser.get("/")).text

    assert "cannot be automatically suspended" in parent_page
    assert "cannot be automatically suspended" in child_page


async def test_revoke_is_not_offered_where_the_callers_own_hold_already_stands(env: Env):
    world = await build_world(env)
    await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id=str(world.fluxer.id))

    page = (await world.ann_browser.get(f"/children/{world.kid.id}")).text

    assert f'name="service_id" value="{world.fluxer.id}"' not in page
    assert f'name="service_id" value="{world.abiotic.id}"' in page


# AC6.6: a parent who is not linked to the child changes nothing ------------------------------------------

async def test_an_unrelated_parent_is_refused_and_no_hold_is_created(env: Env):
    world = await build_world(env)

    response = await post_as(env, world.cy_browser, f"/children/{world.kid.id}/holds", service_id="all")

    assert response.status_code == 403
    assert text.NOT_YOUR_CHILD in response.text
    assert await hold_pairs(env, world.kid) == {}


async def test_an_unrelated_parent_cannot_restore_either(env: Env):
    world = await build_world(env)
    await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all")

    response = await post_as(env, world.cy_browser, f"/children/{world.kid.id}/holds/{world.fluxer.id}/lift")

    assert response.status_code == 403
    assert await hold_pairs(env, world.kid) == {world.fluxer.id: ["parent"], world.abiotic.id: ["parent"]}


# Restore: the refusals ---------------------------------------------------------------------------------

async def test_restoring_a_manual_service_is_refused_with_the_restore_message(env: Env):
    world = await build_world(env)
    await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all")

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds/{world.abiotic.id}/lift")

    assert response.status_code == 403
    assert text.RESTORE_NOT_AUTOMATIC in response.text
    assert world.abiotic.id in await hold_pairs(env, world.kid)


async def test_restoring_an_auto_service_with_only_an_admin_hold_is_refused(env: Env):
    world = await build_world(env)
    await env.db.run(lambda c: holds.place(c, actor=holds.Actor("admin", world.ben.id), target_id=world.kid.id,
                                           service_ids=[world.fluxer.id], now=NOW, window=WINDOW, admin_email=None))

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds/{world.fluxer.id}/lift")

    assert response.status_code == 403
    assert text.LOCKDOWN_ADMIN_ONLY in response.text
    assert await hold_pairs(env, world.kid) == {world.fluxer.id: ["admin"]}


async def test_a_parent_restore_leaves_the_lockdown_hold_in_force(env: Env):
    world = await build_world(env)
    await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all")
    await _lock_down(env, world)

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds/{world.fluxer.id}/lift")

    assert response.status_code == 303
    pairs = await hold_pairs(env, world.kid)
    assert pairs[world.fluxer.id] == ["lockdown"]
    assert "lockdown" in pairs[world.abiotic.id]


# AC7.7: strict mode names the other parent ------------------------------------------------------------

async def test_in_strict_mode_a_restore_of_another_parents_hold_is_403_with_their_name(env: Env):
    world = await build_world(env)
    await env.db.run(lambda c: holds.set_mode(c, child_id=world.kid.id, mode="strict", agreement_note=None,
                                              apply_to_siblings=False, admin_id=world.ben.id, now=NOW))
    await env.db.run(lambda c: holds.place(c, actor=holds.Actor("parent", world.ben.id), target_id=world.kid.id,
                                           service_ids=[world.fluxer.id], now=NOW, window=WINDOW, admin_email=None))

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds/{world.fluxer.id}/lift")

    assert response.status_code == 403
    assert str(escape(text.STRICT_NOT_YOURS.format(names="Ben"))) in response.text
    assert await hold_pairs(env, world.kid) == {world.fluxer.id: ["parent"]}


# AC8.2: the personal page names who placed each hold ---------------------------------------------------

async def test_the_child_page_names_the_parent_who_placed_the_hold(env: Env):
    world = await build_world(env)
    await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id=str(world.fluxer.id))

    page = (await world.kid_browser.get("/")).text

    assert "Suspended by Ann" in page


async def test_the_child_page_names_the_system_during_a_lockdown(env: Env):
    world = await build_world(env)
    await _lock_down(env, world)

    page = (await world.kid_browser.get("/")).text

    assert "Suspended by the system" in page


async def test_the_child_page_names_the_administrator_for_an_admin_hold(env: Env):
    world = await build_world(env)
    await admin_client(env)
    data = await form(env, f"/admin/people/{world.kid.id}", service_id="all")
    placed = await env.client.post(f"/admin/people/{world.kid.id}/holds", data=data)
    assert placed.status_code == 303

    page = (await world.kid_browser.get("/")).text

    assert "Suspended by the administrator" in page


# Phase 8 task 1: where a hold change returns -------------------------------------------------------------

async def test_revoke_from_the_children_page_returns_to_that_childs_section(env: Env):
    world = await build_world(env)

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds",
                             service_id=str(world.fluxer.id), back="children")

    assert response.status_code == 303
    assert response.headers["location"] == f"/children#child-{world.kid.id}"
    assert await hold_pairs(env, world.kid) == {world.fluxer.id: ["parent"]}


async def test_revoke_everything_from_the_children_page_returns_to_that_childs_section(env: Env):
    world = await build_world(env)

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all",
                             back="children")

    assert response.status_code == 303
    assert response.headers["location"] == f"/children#child-{world.kid.id}"
    assert await hold_pairs(env, world.kid) == {world.fluxer.id: ["parent"], world.abiotic.id: ["parent"]}


async def test_restore_from_the_children_page_returns_to_that_childs_section(env: Env):
    world = await build_world(env)
    await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all")

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds/{world.fluxer.id}/lift",
                             back="children")

    assert response.status_code == 303
    assert response.headers["location"] == f"/children#child-{world.kid.id}"
    assert world.fluxer.id not in await hold_pairs(env, world.kid)


async def test_the_same_forms_from_the_child_page_still_return_to_the_child_page(env: Env):
    world = await build_world(env)

    revoked = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all",
                            back="child")
    restored = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds/{world.fluxer.id}/lift",
                             back="child")

    assert revoked.headers["location"] == f"/children/{world.kid.id}"
    assert restored.headers["location"] == f"/children/{world.kid.id}"


@pytest.mark.parametrize("back", ["", "elsewhere", "children/../x", "//elsewhere.invalid/x",
                                  "https://elsewhere.invalid/", "Children"])
async def test_an_unknown_back_value_returns_to_the_child_page(env: Env, back: str):
    world = await build_world(env)

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="all",
                             back=back)

    assert response.status_code == 303
    assert response.headers["location"] == f"/children/{world.kid.id}"


async def test_a_refused_restore_from_the_children_page_rerenders_the_children_page_at_403(env: Env):
    world = await build_world(env)
    await env.db.run(lambda c: holds.set_mode(c, child_id=world.kid.id, mode="strict", agreement_note=None,
                                              apply_to_siblings=False, admin_id=world.ben.id, now=NOW))
    await env.db.run(lambda c: holds.place(c, actor=holds.Actor("parent", world.ben.id), target_id=world.kid.id,
                                           service_ids=[world.fluxer.id], now=NOW, window=WINDOW, admin_email=None))

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds/{world.fluxer.id}/lift",
                             back="children")

    assert response.status_code == 403
    assert "<h1>Children</h1>" in response.text
    assert str(escape(text.STRICT_NOT_YOURS.format(names="Ben"))) in response.text
    assert await hold_pairs(env, world.kid) == {world.fluxer.id: ["parent"]}


async def test_a_refused_revoke_from_the_children_page_rerenders_the_children_page_at_403(env: Env):
    world = await build_world(env)

    response = await post_as(env, world.cy_browser, f"/children/{world.kid.id}/holds", service_id="all",
                             back="children")

    assert response.status_code == 403
    assert "<h1>Children</h1>" in response.text
    assert text.NOT_YOUR_CHILD in response.text
    assert await hold_pairs(env, world.kid) == {}


FORM = re.compile(r'<form method="post" action="([^"]+)" class="[^"]*">(.*?)</form>', re.DOTALL)
FIELD = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')


def rendered_form(page: str, action: str, marker: str) -> dict[str, str]:
    """The hidden fields of the rendered form that posts to `action` and contains `marker`, as a browser sends them."""
    for found_action, body in FORM.findall(page):
        if found_action == action and marker in body:
            return dict(FIELD.findall(body))
    raise AssertionError(f"no form posting to {action} with {marker!r}")


async def test_the_forms_on_the_children_page_post_back_to_that_childs_section(env: Env):
    world = await build_world(env)
    kid, fluxer = world.kid.id, world.fluxer.id

    page = (await world.ann_browser.get("/children")).text
    revoke = rendered_form(section_of(page, kid), f"/children/{kid}/holds", f'value="{fluxer}"')
    assert revoke["back"] == "children"
    revoked = await world.ann_browser.post(f"/children/{kid}/holds", data=revoke)
    assert revoked.status_code == 303
    assert revoked.headers["location"] == f"/children#child-{kid}"
    assert await hold_pairs(env, world.kid) == {fluxer: ["parent"]}

    page = (await world.ann_browser.get("/children")).text
    restore = rendered_form(section_of(page, kid), f"/children/{kid}/holds/{fluxer}/lift", "")
    assert restore["back"] == "children"
    restored = await world.ann_browser.post(f"/children/{kid}/holds/{fluxer}/lift", data=restore)
    assert restored.status_code == 303
    assert restored.headers["location"] == f"/children#child-{kid}"
    assert await hold_pairs(env, world.kid) == {}

    page = (await world.ann_browser.get("/children")).text
    revoke_all = rendered_form(section_of(page, kid), f"/children/{kid}/holds", 'value="all"')
    assert revoke_all["back"] == "children"
    revoked_all = await world.ann_browser.post(f"/children/{kid}/holds", data=revoke_all)
    assert revoked_all.status_code == 303
    assert revoked_all.headers["location"] == f"/children#child-{kid}"
    assert fluxer in await hold_pairs(env, world.kid)


async def test_the_forms_on_the_child_page_post_back_to_the_child_page(env: Env):
    world = await build_world(env)
    kid, fluxer = world.kid.id, world.fluxer.id
    await post_as(env, world.ann_browser, f"/children/{kid}/holds", service_id=str(fluxer), back="children")

    page = (await world.ann_browser.get(f"/children/{kid}")).text
    revoke_all = rendered_form(page, f"/children/{kid}/holds", 'value="all"')
    assert revoke_all["back"] == "child"
    restore = rendered_form(page, f"/children/{kid}/holds/{fluxer}/lift", "")
    assert restore["back"] == "child"
    restored = await world.ann_browser.post(f"/children/{kid}/holds/{fluxer}/lift", data=restore)
    assert restored.status_code == 303
    assert restored.headers["location"] == f"/children/{kid}"
    assert await hold_pairs(env, world.kid) == {}


async def test_a_malformed_service_id_from_the_children_page_rerenders_the_children_page_at_400(env: Env):
    world = await build_world(env)

    response = await post_as(env, world.ann_browser, f"/children/{world.kid.id}/holds", service_id="fluxer",
                             back="children")

    assert response.status_code == 400
    assert "<h1>Children</h1>" in response.text
    assert text.INVALID_INPUT in response.text


# Helpers -----------------------------------------------------------------------------------------------

async def _lock_down(env: Env, world: World) -> None:
    """Two cross-parent reversals inside the window: Ann places, Ben lifts, Ann places again."""
    def sequence(conn) -> None:
        holds.place(conn, actor=holds.Actor("parent", world.ann.id), target_id=world.kid.id,
                    service_ids=[world.fluxer.id], now=NOW, window=WINDOW, admin_email=None)
        holds.lift(conn, actor=holds.Actor("parent", world.ben.id), target_id=world.kid.id,
                   service_ids=[world.fluxer.id], now=NOW, window=WINDOW, admin_email=None)
        holds.place(conn, actor=holds.Actor("parent", world.ann.id), target_id=world.kid.id,
                    service_ids=[world.fluxer.id], now=NOW, window=WINDOW, admin_email=None)

    await env.db.run(sequence)
