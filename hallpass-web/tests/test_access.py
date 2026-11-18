# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The access decision against a real database: entitlement, person status, terms and retirement.

Covers hallpass.AC5.3 (end to end), AC4.2 (desired state) and the terms reason.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hallpass import access, grants, registry, services, terms
from hallpass.db import Database
from hallpass.registry import Person
from hallpass.services import Service
from tests.helpers import make_service

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hp.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def admin(db: Database) -> Person:
    return await db.run(lambda c: registry.create_person(c, display_name="Admin", account_type="adult", now=NOW))


async def publish(db: Database, admin: Person, *, blocking: bool = True) -> int:
    version = await db.run(lambda c: terms.publish(
        c, tos_text="Terms text.", privacy_text="Privacy text.", blocking=blocking, actor_id=admin.id, now=NOW))
    return version.id


async def make_person(db: Database, *, account_type: str = "adult", name: str = "Ann") -> Person:
    return await db.run(lambda c: registry.create_person(c, display_name=name, account_type=account_type, now=NOW))


async def accept(db: Database, person_id: int, version_id: int) -> None:
    await db.run(lambda c: grants.accept_terms(c, person_id, version_id, NOW))


async def entitle(db: Database, admin: Person, service_id: int, subject_kind: str, subject: str,
                  effect: str = "grant") -> None:
    await db.run(lambda c: services.add_entitlement(c, service_id, subject_kind, subject, effect,
                                                    actor_id=admin.id, now=NOW))


async def decide(db: Database, person_id: int, service: Service) -> access.Access:
    person = await db.run(lambda c: registry.get_person(c, person_id))
    assert person is not None
    return await db.run(lambda c: access.evaluate(c, person, service))


async def active_adult_with_type_grant(db: Database, admin: Person) -> tuple[Person, Service]:
    version_id = await publish(db, admin)
    adult = await make_person(db)
    await accept(db, adult.id, version_id)
    calendar = await make_service(db, "calendar")
    await entitle(db, admin, calendar.id, "type", "adult")
    return adult, calendar


# AC5.3 (end to end) and the terms reason ------------------------------------------------------------

async def test_entitled_accepted_active_adult_is_allowed_with_no_reasons(db: Database, admin: Person) -> None:
    adult, calendar = await active_adult_with_type_grant(db, admin)
    decision = await decide(db, adult.id, calendar)
    assert decision == access.Access(desired="allowed", reasons=())


async def test_person_deny_overrides_a_type_grant(db: Database, admin: Person) -> None:
    adult, calendar = await active_adult_with_type_grant(db, admin)
    await entitle(db, admin, calendar.id, "person", str(adult.id), effect="deny")

    decision = await decide(db, adult.id, calendar)
    assert decision == access.Access(desired="suspended", reasons=("not_entitled",))


async def test_group_grant_reaches_its_members_only(db: Database, admin: Person) -> None:
    version_id = await publish(db, admin)
    member = await make_person(db, name="Member")
    outsider = await make_person(db, name="Outsider")
    await accept(db, member.id, version_id)
    await accept(db, outsider.id, version_id)
    group_id = await db.run(lambda c: registry.create_group(c, "Movie night"))
    await db.run(lambda c: registry.add_member(c, group_id, member.id))
    calendar = await make_service(db, "calendar")
    await entitle(db, admin, calendar.id, "group", str(group_id))

    assert (await decide(db, member.id, calendar)).desired == "allowed"
    assert await decide(db, outsider.id, calendar) == access.Access(
        desired="suspended", reasons=("not_entitled",))


async def test_unentitled_active_person_is_suspended_with_not_entitled(db: Database, admin: Person) -> None:
    version_id = await publish(db, admin)
    adult = await make_person(db)
    await accept(db, adult.id, version_id)
    calendar = await make_service(db, "calendar")

    assert await decide(db, adult.id, calendar) == access.Access(desired="suspended", reasons=("not_entitled",))


async def test_retired_service_suspends_an_entitled_person_with_retired(db: Database, admin: Person) -> None:
    adult, calendar = await active_adult_with_type_grant(db, admin)
    await db.run(lambda c: services.retire_service(c, calendar.id, actor_id=admin.id, now=NOW))
    retired = await db.run(lambda c: services.get(c, calendar.id))
    assert retired is not None

    assert await decide(db, adult.id, retired) == access.Access(desired="suspended", reasons=("retired",))


async def test_active_adult_who_has_not_accepted_a_blocking_version_is_suspended_with_terms(
        db: Database, admin: Person) -> None:
    adult, calendar = await active_adult_with_type_grant(db, admin)
    await publish(db, admin, blocking=True)

    assert await decide(db, adult.id, calendar) == access.Access(desired="suspended", reasons=("terms",))


async def test_a_newer_non_blocking_version_does_not_suspend(db: Database, admin: Person) -> None:
    adult, calendar = await active_adult_with_type_grant(db, admin)
    await publish(db, admin, blocking=False)

    assert await decide(db, adult.id, calendar) == access.Access(desired="allowed", reasons=())


# AC4.2 (desired state) ------------------------------------------------------------------------------

async def test_awaiting_grant_child_is_suspended_on_every_service_until_the_parent_grants(
        db: Database, admin: Person) -> None:
    version_id = await publish(db, admin)
    parent = await make_person(db, account_type="parent", name="Mum")
    await accept(db, parent.id, version_id)
    child = await make_person(db, account_type="child", name="Kid")
    await db.run(lambda c: registry.link_parent(c, child.id, parent.id, NOW))
    calendar = await make_service(db, "calendar")
    valheim = await make_service(db, "valheim", kind="game")
    for service in (calendar, valheim):
        await entitle(db, admin, service.id, "type", "child")

    for service in (calendar, valheim):
        decision = await decide(db, child.id, service)
        assert decision.desired == "suspended"
        assert "not_active" in decision.reasons

    await db.run(lambda c: grants.grant(c, child_id=child.id, parent_id=parent.id, now=NOW))
    await db.run(lambda c: grants.settle_child_at_sign_in(c, child.id))

    for service in (calendar, valheim):
        assert await decide(db, child.id, service) == access.Access(desired="allowed", reasons=())


async def test_an_active_child_is_allowed_while_a_linked_parent_has_accepted(db: Database, admin: Person) -> None:
    version_id = await publish(db, admin)
    parent = await make_person(db, account_type="parent", name="Mum")
    await accept(db, parent.id, version_id)
    child = await make_person(db, account_type="child", name="Kid")
    await db.run(lambda c: registry.link_parent(c, child.id, parent.id, NOW))
    calendar = await make_service(db, "calendar")
    await entitle(db, admin, calendar.id, "type", "child")
    await db.run(lambda c: grants.grant(c, child_id=child.id, parent_id=parent.id, now=NOW))
    await db.run(lambda c: grants.settle_child_at_sign_in(c, child.id))

    assert await decide(db, child.id, calendar) == access.Access(desired="allowed", reasons=())


async def test_an_active_child_is_suspended_with_terms_when_no_linked_parent_has_accepted(
        db: Database, admin: Person) -> None:
    await publish(db, admin)
    parent = await make_person(db, account_type="parent", name="Mum")
    child = await make_person(db, account_type="child", name="Kid")
    await db.run(lambda c: registry.link_parent(c, child.id, parent.id, NOW))
    calendar = await make_service(db, "calendar")
    await entitle(db, admin, calendar.id, "type", "child")
    # Active without a grant, so that the terms reason is the only one that applies.
    await db.run(lambda c: c.execute("UPDATE person SET status = 'active' WHERE id = ?", (child.id,)))

    assert await decide(db, child.id, calendar) == access.Access(desired="suspended", reasons=("terms",))


# services_for --------------------------------------------------------------------------------------

async def test_services_for_lists_entitled_and_identified_services_by_name_and_skips_retired(
        db: Database, admin: Person) -> None:
    adult, calendar = await active_adult_with_type_grant(db, admin)
    await make_service(db, "valheim", kind="game")
    await db.run(lambda c: registry.add_identity(c, adult.id, "valheim", "java_name", "BuilderSeven"))
    old_game = await make_service(db, "old-game", kind="game")
    await entitle(db, admin, old_game.id, "type", "adult")
    await db.run(lambda c: services.retire_service(c, old_game.id, actor_id=admin.id, now=NOW))
    await make_service(db, "unrelated", kind="game")

    rows = await db.run(lambda c: access.services_for(c, registry.get_person(c, adult.id)))
    assert [(service.slug, decision.desired) for service, decision in rows] == [
        ("calendar", "allowed"), ("valheim", "suspended"),
    ]
    assert rows[1][1].reasons == ("not_entitled",)


# held reason (AC6.3, store level) ----------------------------------------------------------------

async def insert_hold(db: Database, *, person_id: int, service_id: int, kind: str, placed_by: int | None) -> None:
    await db.run(lambda c: c.execute(
        "INSERT INTO hold (person_id, service_id, kind, placed_by, placed_at) VALUES (?, ?, ?, ?, ?)",
        (person_id, service_id, kind, placed_by, "2026-11-13T12:00:00+00:00")))


async def remove_holds(db: Database, *, person_id: int, kind: str) -> None:
    await db.run(lambda c: c.execute("DELETE FROM hold WHERE person_id = ? AND kind = ?", (person_id, kind)))


async def test_parent_and_admin_holds_suspend_with_held_until_both_are_gone(db: Database, admin: Person) -> None:
    adult, calendar = await active_adult_with_type_grant(db, admin)
    parent = await make_person(db, account_type="parent", name="Mum")
    await insert_hold(db, person_id=adult.id, service_id=calendar.id, kind="parent", placed_by=parent.id)
    await insert_hold(db, person_id=adult.id, service_id=calendar.id, kind="admin", placed_by=admin.id)

    assert await decide(db, adult.id, calendar) == access.Access(desired="suspended", reasons=("held",))

    await remove_holds(db, person_id=adult.id, kind="parent")
    assert await decide(db, adult.id, calendar) == access.Access(desired="suspended", reasons=("held",))

    await remove_holds(db, person_id=adult.id, kind="admin")
    assert await decide(db, adult.id, calendar) == access.Access(desired="allowed", reasons=())


async def test_a_hold_on_one_service_does_not_suspend_another(db: Database, admin: Person) -> None:
    adult, calendar = await active_adult_with_type_grant(db, admin)
    valheim = await make_service(db, "valheim", kind="game")
    await entitle(db, admin, valheim.id, "type", "adult")
    await insert_hold(db, person_id=adult.id, service_id=calendar.id, kind="admin", placed_by=admin.id)

    assert (await decide(db, adult.id, calendar)).reasons == ("held",)
    assert await decide(db, adult.id, valheim) == access.Access(desired="allowed", reasons=())


async def test_holds_for_returns_only_the_holds_on_that_person_and_service(db: Database, admin: Person) -> None:
    adult, calendar = await active_adult_with_type_grant(db, admin)
    other = await make_person(db, name="Other")
    valheim = await make_service(db, "valheim", kind="game")
    parent = await make_person(db, account_type="parent", name="Mum")
    await insert_hold(db, person_id=adult.id, service_id=calendar.id, kind="parent", placed_by=parent.id)
    await insert_hold(db, person_id=adult.id, service_id=valheim.id, kind="admin", placed_by=admin.id)
    await insert_hold(db, person_id=other.id, service_id=calendar.id, kind="admin", placed_by=admin.id)

    found = await db.run(lambda c: access.holds_for(c, adult.id, calendar.id))
    assert [(h.kind, h.placed_by, h.review_id) for h in found] == [("parent", parent.id, None)]
    on_valheim = await db.run(lambda c: access.holds_for(c, adult.id, valheim.id))
    assert [(h.kind, h.placed_by, h.service_id) for h in on_valheim] == [("admin", admin.id, valheim.id)]
