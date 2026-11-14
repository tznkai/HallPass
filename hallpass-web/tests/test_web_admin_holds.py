# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The administrator's hold, mark, mode and review pages, driven through HTTP.

Covers hallpass.AC6.2, AC6.5, AC7.8, AC7.9 (through HTTP), and the admin-only gate on every new route.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import httpx

from hallpass import grants, holds, registry, services, text
from hallpass.registry import Person
from tests.helpers import (
    ADMIN_SUB,
    NOW,
    Env,
    admin_client,
    form,
    make_service,
    signed_in_person,
    store_person,
)
from tests.test_forward_auth import check

WINDOW = timedelta(hours=24)


async def grant_to_adults(env: Env, service: services.Service, actor: Person) -> None:
    await env.db.run(lambda c: services.add_entitlement(c, service.id, "type", "adult", "grant",
                                                        actor_id=actor.id, now=NOW))


async def create_service(env: Env, slug: str, *, auto: bool, actor: Person) -> services.Service:
    return await env.db.run(lambda c: services.create_service(
        c, slug=slug, name=slug.title(), kind="forward_auth", adapter="minecraft" if auto else "manual",
        auto_suspendable=auto, url=None,
        actor_id=actor.id, now=NOW))


async def person_row(env: Env, person_id: int) -> Person:
    return await env.db.run(lambda c: registry.get_person(c, person_id))


async def hold_kinds(env: Env, person_id: int, service_id: int) -> list[str]:
    return await env.db.run(lambda c: [hold.kind for hold in holds.services_with_holds(c, person_id).get(service_id, [])])


async def admin_post(env: Env, page: str, path: str, **fields: str) -> httpx.Response:
    data = await form(env, page, **fields)
    return await env.client.post(path, data=data)


# AC6.2: the administrator's hold reaches forward auth ---------------------------------------------------------

async def test_an_admin_hold_suspends_an_adult_on_forward_auth_until_it_is_lifted(env: Env):
    await admin_client(env)
    ada, browser = await signed_in_person(env, account_type="adult", display_name="Ada")
    calendar = await make_service(env.db, "calendar")
    await grant_to_adults(env, calendar, ada)
    assert (await check(browser, "calendar")).status_code == 200

    placed = await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/holds", service_id="all")
    assert placed.status_code == 303
    assert (await check(browser, "calendar")).status_code == 403

    lifted = await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/holds/lift",
                              service_id="all")
    assert lifted.status_code == 303
    assert (await check(browser, "calendar")).status_code == 200


async def test_the_administrator_places_a_hold_on_one_service_only(env: Env):
    await admin_client(env)
    ada, _ = await signed_in_person(env, account_type="adult", display_name="Ada")
    calendar = await make_service(env.db, "calendar")
    other = await create_service(env, "other", auto=True, actor=ada)

    await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/holds", service_id=str(calendar.id))

    assert await hold_kinds(env, ada.id, calendar.id) == ["admin"]
    assert await hold_kinds(env, ada.id, other.id) == []


# AC6.5: manual marks -------------------------------------------------------------------------------------------

async def test_the_administrator_marks_a_held_manual_service_and_the_mark_goes_with_the_last_hold(env: Env):
    await admin_client(env)
    ada, _ = await signed_in_person(env, account_type="adult", display_name="Ada")
    calendar = await make_service(env.db, "calendar")
    await grant_to_adults(env, calendar, ada)
    await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/holds", service_id="all")

    marked = await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/manual/{calendar.id}")
    assert marked.status_code == 303
    page = (await env.client.get(f"/admin/people/{ada.id}")).text
    assert "<td>manually suspended</td>" in page

    await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/holds/lift", service_id="all")

    assert await env.db.run(lambda c: holds.marked_service_ids(c, ada.id)) == set()


async def test_a_mark_is_refused_for_a_service_that_is_automatically_suspendable_or_not_held(env: Env):
    await admin_client(env)
    ada, _ = await signed_in_person(env, account_type="adult", display_name="Ada")
    manual = await make_service(env.db, "calendar")
    auto = await create_service(env, "fluxer", auto=True, actor=ada)

    not_held = await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/manual/{manual.id}")
    on_auto = await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/manual/{auto.id}")

    assert not_held.status_code == 400 and text.MARK_NO_HOLD in not_held.text
    assert on_auto.status_code == 400 and text.MARK_NOT_MANUAL in on_auto.text


async def test_the_mark_can_be_removed_while_the_hold_stands(env: Env):
    await admin_client(env)
    ada, _ = await signed_in_person(env, account_type="adult", display_name="Ada")
    calendar = await make_service(env.db, "calendar")
    await grant_to_adults(env, calendar, ada)
    await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/holds", service_id="all")
    await admin_post(env, f"/admin/people/{ada.id}", f"/admin/people/{ada.id}/manual/{calendar.id}")

    response = await admin_post(env, f"/admin/people/{ada.id}",
                                f"/admin/people/{ada.id}/manual/{calendar.id}/unmark")

    assert response.status_code == 303
    assert await env.db.run(lambda c: holds.marked_service_ids(c, ada.id)) == set()
    assert await hold_kinds(env, ada.id, calendar.id) == ["admin"]


# Families, lockdowns and reviews -------------------------------------------------------------------------

@dataclass
class Family:
    kid: Person
    ann: Person
    ann_browser: httpx.AsyncClient
    ben: Person
    fluxer: services.Service
    abiotic: services.Service


async def make_family(env: Env, *, mode: str = "default", note: str | None = None) -> Family:
    """A child with two linked parents, Ann (signed in) and Ben, and two services the child may use."""
    ann, ann_browser = await signed_in_person(env, account_type="adult", display_name="Ann",
                                              email="ann@example.com")
    ben = await store_person(env, display_name="Ben", account_type="adult", email="ben@example.com")
    kid = await store_person(env, display_name="Kid")
    fluxer = await create_service(env, "fluxer", auto=True, actor=ann)
    abiotic = await create_service(env, "abiotic", auto=False, actor=ann)

    def setup(conn) -> None:
        registry.link_parent(conn, kid.id, ann.id, NOW)
        registry.link_parent(conn, kid.id, ben.id, NOW)
        holds.set_mode(conn, child_id=kid.id, mode=mode, agreement_note=note, apply_to_siblings=False,
                       admin_id=ann.id, now=NOW)
        for service in (fluxer, abiotic):
            services.add_entitlement(conn, service.id, "type", "child", "grant", actor_id=ann.id, now=NOW)

    await env.db.run(setup)
    return Family(kid=kid, ann=ann, ann_browser=ann_browser, ben=ben, fluxer=fluxer, abiotic=abiotic)


async def lock_down(env: Env, family: Family) -> None:
    """Two cross-parent reversals in the window: Ann places, Ben lifts, Ann places again."""
    def sequence(conn) -> None:
        for actor, action in ((family.ann, "place"), (family.ben, "lift"), (family.ann, "place")):
            act = holds.place if action == "place" else holds.lift
            act(conn, actor=holds.Actor("parent", actor.id), target_id=family.kid.id,
                service_ids=[family.fluxer.id], now=NOW, window=WINDOW, admin_email="admin@example.com")

    await env.db.run(sequence)


async def open_review_id(env: Env) -> int:
    open_items, _ = await env.db.run(holds.list_reviews)
    assert len(open_items) == 1, open_items
    return open_items[0]["id"]


async def test_a_lockdown_survives_a_parent_restore(env: Env):
    await admin_client(env)
    family = await make_family(env)
    await lock_down(env, family)

    csrf = await form(env, "/", client=family.ann_browser)
    restore = await family.ann_browser.post(
        f"/children/{family.kid.id}/holds/{family.fluxer.id}/lift", data=csrf)

    assert restore.status_code == 303
    assert await hold_kinds(env, family.kid.id, family.fluxer.id) == ["lockdown"]


async def test_the_administrators_lift_leaves_the_lockdown_holds_in_force(env: Env):
    await admin_client(env)
    family = await make_family(env)
    await lock_down(env, family)

    response = await admin_post(env, f"/admin/people/{family.kid.id}", f"/admin/people/{family.kid.id}/holds/lift",
                                service_id="all", include_parent="on")

    assert response.status_code == 303
    assert await hold_kinds(env, family.kid.id, family.fluxer.id) == ["lockdown"]
    assert await hold_kinds(env, family.kid.id, family.abiotic.id) == ["lockdown"]


async def test_resolving_with_lift_the_lockdown_removes_it_and_records_the_outcome(env: Env):
    await admin_client(env)
    family = await make_family(env)
    await lock_down(env, family)
    review_id = await open_review_id(env)

    response = await admin_post(env, f"/admin/reviews/{review_id}", f"/admin/reviews/{review_id}/resolve",
                                outcome="Agreed to pause for a week", lift_lockdown="on")

    assert response.status_code == 303
    # Ann's parent hold is not a lockdown, so it stays: only the lockdown holds go with the lift.
    assert await hold_kinds(env, family.kid.id, family.fluxer.id) == ["parent"]
    assert await hold_kinds(env, family.kid.id, family.abiotic.id) == []
    page = (await env.client.get(f"/admin/reviews/{review_id}")).text
    assert "Agreed to pause for a week" in page
    assert "Resolved" in page
    admin_id = (await env.db.run(lambda c: registry.get_by_uuid(c, ADMIN_SUB))).id
    resolved_by = await env.db.run(lambda c: c.execute(
        "SELECT resolved_by FROM review_item WHERE id = ?", (review_id,)).fetchone()[0])
    assert resolved_by == admin_id


async def test_resolving_without_the_lift_keeps_the_lockdown(env: Env):
    await admin_client(env)
    family = await make_family(env)
    await lock_down(env, family)
    review_id = await open_review_id(env)

    await admin_post(env, f"/admin/reviews/{review_id}", f"/admin/reviews/{review_id}/resolve",
                     outcome="Talked it through")

    assert await hold_kinds(env, family.kid.id, family.fluxer.id) == ["parent", "lockdown"]


async def test_the_review_queue_lists_open_and_resolved_items_and_the_nav_counts_them(env: Env):
    await admin_client(env)
    family = await make_family(env)
    await lock_down(env, family)

    queue = (await env.client.get("/admin/reviews")).text
    assert "Kid" in queue and "Reviews (1 open)" in (await env.client.get("/admin/people")).text

    review_id = await open_review_id(env)
    await admin_post(env, f"/admin/reviews/{review_id}", f"/admin/reviews/{review_id}/resolve",
                     outcome="Done", lift_lockdown="on")

    queue = (await env.client.get("/admin/reviews")).text
    assert "Recently resolved" in queue and "Done" in queue
    assert "Reviews (0 open)" in (await env.client.get("/admin/people")).text


async def test_a_resolve_of_an_unknown_review_is_404(env: Env):
    await admin_client(env)

    response = await admin_post(env, "/admin/reviews", "/admin/reviews/999/resolve", outcome="x")

    assert response.status_code == 404


# AC7.9: the mode form and the monitored note ------------------------------------------------------------

async def test_a_mode_change_with_siblings_reaches_the_children_with_the_same_parents_only(env: Env):
    await admin_client(env)
    family = await make_family(env)
    sibling = await store_person(env, display_name="Sibling", account_type="child")
    other = await store_person(env, display_name="Other", account_type="child")
    await env.db.run(lambda c: [registry.link_parent(c, sibling.id, parent.id, NOW)
                                for parent in (family.ann, family.ben)])
    await env.db.run(lambda c: registry.link_parent(c, other.id, family.ann.id, NOW))

    response = await admin_post(env, f"/admin/people/{family.kid.id}", f"/admin/people/{family.kid.id}/mode",
                                mode="monitored", agreement_note="Weekdays only", apply_to_siblings="on")

    assert response.status_code == 303
    assert (await person_row(env, family.kid.id)).conflict_mode == "monitored"
    assert (await person_row(env, sibling.id)).conflict_mode == "monitored"
    assert (await person_row(env, other.id)).conflict_mode == "default"
    assert (await person_row(env, family.kid.id)).agreement_note == "Weekdays only"


async def test_the_monitored_lockdown_review_shows_the_agreement_note(env: Env):
    await admin_client(env)
    family = await make_family(env, mode="monitored", note="Weekdays only")
    await lock_down(env, family)
    review_id = await open_review_id(env)

    page = (await env.client.get(f"/admin/reviews/{review_id}")).text

    assert "Weekdays only" in page


async def test_an_invalid_mode_is_refused(env: Env):
    await admin_client(env)
    family = await make_family(env)

    response = await admin_post(env, f"/admin/people/{family.kid.id}", f"/admin/people/{family.kid.id}/mode",
                                mode="lenient")

    assert response.status_code == 400
    assert (await person_row(env, family.kid.id)).conflict_mode == "default"


# The admin gate ----------------------------------------------------------------------------------------------

async def test_a_parent_is_refused_every_admin_hold_route_and_the_review_pages(env: Env):
    await admin_client(env)
    family = await make_family(env)
    csrf = await form(env, "/", client=family.ann_browser)

    placed = await family.ann_browser.post(f"/admin/people/{family.kid.id}/holds", data={**csrf, "service_id": "all"})
    lifted = await family.ann_browser.post(f"/admin/people/{family.kid.id}/holds/lift",
                                           data={**csrf, "service_id": "all"})
    mode = await family.ann_browser.post(f"/admin/people/{family.kid.id}/mode", data={**csrf, "mode": "strict"})
    reviews = await family.ann_browser.get("/admin/reviews")

    assert [placed.status_code, lifted.status_code, mode.status_code, reviews.status_code] == [403, 403, 403, 403]
    assert await hold_kinds(env, family.kid.id, family.fluxer.id) == []
    assert (await person_row(env, family.kid.id)).conflict_mode == "default"


async def test_a_parent_does_not_see_the_review_count_in_the_nav(env: Env):
    await admin_client(env)
    family = await make_family(env)
    await lock_down(env, family)

    page = (await family.ann_browser.get("/")).text

    assert "Reviews (" not in page


# Lifting a lockdown from a resolved review -------------------------------------------------------------

async def make_signed_family(env: Env) -> tuple[Family, httpx.AsyncClient]:
    """Like make_family, but the child signs in and is approved by Ann, so forward auth can answer for the child."""
    ann, ann_browser = await signed_in_person(env, account_type="adult", display_name="Ann",
                                              email="ann@example.com")
    kid, kid_browser = await signed_in_person(env, account_type="child", display_name="Kid")
    ben = await store_person(env, display_name="Ben", account_type="adult", email="ben@example.com")
    fluxer = await create_service(env, "fluxer", auto=True, actor=ann)
    abiotic = await create_service(env, "abiotic", auto=False, actor=ann)

    def setup(conn) -> None:
        registry.link_parent(conn, kid.id, ann.id, NOW)
        registry.link_parent(conn, kid.id, ben.id, NOW)
        for service in (fluxer, abiotic):
            services.add_entitlement(conn, service.id, "type", "child", "grant", actor_id=ann.id, now=NOW)
        grants.grant(conn, child_id=kid.id, parent_id=ann.id, now=NOW)

    await env.db.run(setup)
    family = Family(kid=kid, ann=ann, ann_browser=ann_browser, ben=ben, fluxer=fluxer, abiotic=abiotic)
    return family, kid_browser


async def resolve_keeping_lockdown(env: Env, review_id: int) -> None:
    await admin_post(env, f"/admin/reviews/{review_id}", f"/admin/reviews/{review_id}/resolve",
                     outcome="Talked it through")


async def test_lifting_a_resolved_reviews_lockdown_lets_the_child_use_the_services_again(env: Env):
    await admin_client(env)
    family, kid_browser = await make_signed_family(env)
    assert (await check(kid_browser, "fluxer")).status_code == 200
    await lock_down(env, family)
    review_id = await open_review_id(env)
    await resolve_keeping_lockdown(env, review_id)
    assert (await check(kid_browser, "fluxer")).status_code == 403

    lifted = await admin_post(env, f"/admin/reviews/{review_id}", f"/admin/reviews/{review_id}/lift")
    assert lifted.status_code == 303
    assert lifted.headers["location"] == f"/admin/reviews/{review_id}"
    # Ann's own parent hold is still on the child; the administrator lifts it too, as an admin action.
    await admin_post(env, f"/admin/people/{family.kid.id}", f"/admin/people/{family.kid.id}/holds/lift",
                     service_id="all", include_parent="on")

    assert await hold_kinds(env, family.kid.id, family.fluxer.id) == []
    assert await hold_kinds(env, family.kid.id, family.abiotic.id) == []
    assert (await check(kid_browser, "fluxer")).status_code == 200
    logged = await env.db.run(lambda c: c.execute(
        "SELECT actor_kind, target_person_id FROM decision_log WHERE action = 'lockdown_lifted'").fetchall())
    assert [tuple(row) for row in logged] == [("admin", family.kid.id)]


async def test_a_parent_cannot_lift_a_lockdown_from_a_review(env: Env):
    await admin_client(env)
    family, _ = await make_signed_family(env)
    await lock_down(env, family)
    review_id = await open_review_id(env)
    await resolve_keeping_lockdown(env, review_id)
    csrf = await form(env, "/", client=family.ann_browser)

    response = await family.ann_browser.post(f"/admin/reviews/{review_id}/lift", data=csrf)

    assert response.status_code == 403
    assert await hold_kinds(env, family.kid.id, family.fluxer.id) == ["parent", "lockdown"]


async def test_an_open_review_cannot_have_its_lockdown_lifted(env: Env):
    await admin_client(env)
    family, _ = await make_signed_family(env)
    await lock_down(env, family)
    review_id = await open_review_id(env)

    response = await admin_post(env, f"/admin/reviews/{review_id}", f"/admin/reviews/{review_id}/lift")

    assert response.status_code == 400
    assert await hold_kinds(env, family.kid.id, family.fluxer.id) == ["parent", "lockdown"]


async def test_the_review_page_offers_the_lift_only_when_resolved_and_a_lockdown_remains(env: Env):
    await admin_client(env)
    family, _ = await make_signed_family(env)
    await lock_down(env, family)
    review_id = await open_review_id(env)
    lift_form = f'action="/admin/reviews/{review_id}/lift"'

    assert lift_form not in (await env.client.get(f"/admin/reviews/{review_id}")).text

    await resolve_keeping_lockdown(env, review_id)
    assert lift_form in (await env.client.get(f"/admin/reviews/{review_id}")).text

    await admin_post(env, f"/admin/reviews/{review_id}", f"/admin/reviews/{review_id}/lift")
    assert lift_form not in (await env.client.get(f"/admin/reviews/{review_id}")).text
