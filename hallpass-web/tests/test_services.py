# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service catalogue and entitlement store tests against a real SQLite database under tmp_path.

Covers hallpass.AC5.1 and AC5.2 (store part).
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hallpass import registry, services, text
from hallpass.db import Database
from hallpass.registry import NoSuchGroup, NoSuchPerson
from hallpass.services import EntitlementRefused, NoSuchService

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hp.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def admin_id(db: Database) -> int:
    admin = await db.run(lambda c: registry.create_person(c, display_name="Admin", account_type="adult", now=NOW))
    return admin.id


async def make_service(db: Database, actor_id: int, slug: str = "calendar", kind: str = "forward_auth",
                       **fields) -> services.Service:
    kwargs = {"slug": slug, "name": "Calendar", "kind": kind, "adapter": "manual", "auto_suspendable": False,
              "url": None, "actor_id": actor_id, "now": NOW, **fields}
    return await db.run(lambda c: services.create_service(c, **kwargs))


@pytest.fixture
async def calendar(db: Database, admin_id: int) -> services.Service:
    return await make_service(db, admin_id, slug="calendar")


async def subject_for(db: Database, subject_kind: str) -> str:
    """A real subject id of the given kind, as the store stores it. A type subject is the account type itself."""
    if subject_kind == "type":
        return "adult"
    if subject_kind == "group":
        group_id = await db.run(lambda c: registry.create_group(c, "Movie night"))
        return str(group_id)
    person = await db.run(lambda c: registry.create_person(c, display_name="Pat", account_type="adult", now=NOW))
    return str(person.id)


# AC5.1 ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["oidc", "forward_auth", "game", "feed"])
async def test_each_kind_is_stored_with_its_adapter_and_suspend_flag(db: Database, admin_id: int, kind: str) -> None:
    slug = f"svc-{kind.replace('_', '-')}"
    created = await make_service(db, admin_id, slug=slug, kind=kind, adapter="minecraft-plugin",
                                 auto_suspendable=True, url="https://svc.invalid/")

    stored = await db.run(lambda c: services.get_by_slug(c, slug))
    assert stored == created
    assert stored.kind == kind
    assert stored.adapter == "minecraft-plugin"
    assert stored.auto_suspendable is True
    assert stored.url == "https://svc.invalid/"
    assert stored.retired is False


async def test_retired_service_leaves_the_default_list_and_stays_in_the_full_list(db: Database,
                                                                                  admin_id: int) -> None:
    await make_service(db, admin_id, slug="calendar")
    retired = await make_service(db, admin_id, slug="old-game", kind="game")

    await db.run(lambda c: services.retire_service(c, retired.id, actor_id=admin_id, now=NOW))

    active = await db.run(lambda c: services.list_services(c))
    everything = await db.run(lambda c: services.list_services(c, include_retired=True))
    assert [s.slug for s in active] == ["calendar"]
    assert sorted(s.slug for s in everything) == ["calendar", "old-game"]
    stored = await db.run(lambda c: services.get(c, retired.id))
    assert stored is not None and stored.retired is True


async def test_unretiring_puts_the_service_back_in_the_default_list(db: Database, admin_id: int) -> None:
    service = await make_service(db, admin_id, slug="calendar")
    await db.run(lambda c: services.retire_service(c, service.id, actor_id=admin_id, now=NOW))
    await db.run(lambda c: services.unretire_service(c, service.id, actor_id=admin_id, now=NOW))

    active = await db.run(lambda c: services.list_services(c))
    assert [s.slug for s in active] == ["calendar"]


async def test_update_changes_the_editable_fields_and_keeps_slug_and_kind(db: Database, admin_id: int,
                                                                          calendar: services.Service) -> None:
    updated = await db.run(lambda c: services.update_service(
        c, calendar.id, name="Family calendar", adapter="caddy", auto_suspendable=True,
        url="https://cal.invalid/", actor_id=admin_id, now=NOW))

    assert updated.name == "Family calendar"
    assert updated.adapter == "caddy"
    assert updated.auto_suspendable is True
    assert updated.url == "https://cal.invalid/"
    assert updated.slug == "calendar"
    assert updated.kind == "forward_auth"


@pytest.mark.parametrize("slug", ["Calendar", "-calendar", "cal/feed", "cal feed", "cal.feed", "", "the_ship",
                                  "a" * 65])
async def test_slug_outside_the_allowed_shape_is_refused(db: Database, admin_id: int, slug: str) -> None:
    with pytest.raises(services.ServiceRefused) as info:
        await make_service(db, admin_id, slug=slug)
    assert str(info.value) == text.SERVICE_SLUG_INVALID
    assert await db.run(lambda c: services.list_services(c, include_retired=True)) == []


@pytest.mark.parametrize("slug", ["the-ship", "a", "1-2", "a" * 64])
async def test_slug_in_the_allowed_shape_is_accepted(db: Database, admin_id: int, slug: str) -> None:
    created = await make_service(db, admin_id, slug=slug)
    assert created.slug == slug


@pytest.mark.parametrize("adapter", ["manual", "", "  manual  "])
async def test_automatic_suspension_on_the_manual_adapter_is_refused(db: Database, admin_id: int,
                                                                    adapter: str) -> None:
    with pytest.raises(services.ServiceRefused) as info:
        await make_service(db, admin_id, slug="calendar", adapter=adapter, auto_suspendable=True)
    assert str(info.value) == text.SERVICE_SUSPEND_NEEDS_ADAPTER
    assert await db.run(lambda c: services.list_services(c, include_retired=True)) == []


async def test_manual_adapter_without_automatic_suspension_is_allowed(db: Database, admin_id: int) -> None:
    created = await make_service(db, admin_id, slug="abiotic", adapter="manual", auto_suspendable=False)
    assert (created.adapter, created.auto_suspendable) == ("manual", False)


async def test_an_update_cannot_move_an_automatic_service_to_manual(db: Database, admin_id: int,
                                                                    calendar: services.Service) -> None:
    await db.run(lambda c: services.update_service(c, calendar.id, name="Calendar", adapter="minecraft",
                                                  auto_suspendable=True, url=None, actor_id=admin_id, now=NOW))

    with pytest.raises(services.ServiceRefused) as info:
        await db.run(lambda c: services.update_service(c, calendar.id, name="Calendar", adapter="manual",
                                                      auto_suspendable=True, url=None, actor_id=admin_id, now=NOW))

    assert str(info.value) == text.SERVICE_SUSPEND_NEEDS_ADAPTER
    stored = await db.run(lambda c: services.get(c, calendar.id))
    assert (stored.adapter, stored.auto_suspendable) == ("minecraft", True)


async def test_duplicate_slug_is_refused(db: Database, admin_id: int) -> None:
    await make_service(db, admin_id, slug="calendar")
    with pytest.raises(sqlite3.IntegrityError):
        await make_service(db, admin_id, slug="calendar", kind="feed")


async def test_unknown_service_kind_is_refused(db: Database, admin_id: int) -> None:
    with pytest.raises(ValueError, match="unknown service kind"):
        await make_service(db, admin_id, kind="ftp")


async def test_each_mutation_writes_one_decision_row(db: Database, admin_id: int, calendar: services.Service) -> None:
    group_id = await db.run(lambda c: registry.create_group(c, "Family"))
    await db.run(lambda c: services.update_service(c, calendar.id, name="Calendar", adapter="manual",
                                                  auto_suspendable=False, url=None, actor_id=admin_id, now=NOW))
    grant = await db.run(lambda c: services.add_entitlement(
        c, calendar.id, "group", str(group_id), "grant", actor_id=admin_id, now=NOW))
    await db.run(lambda c: services.remove_entitlement(c, grant.id, actor_id=admin_id, now=NOW))
    await db.run(lambda c: services.retire_service(c, calendar.id, actor_id=admin_id, now=NOW))

    rows = await db.run(lambda c: c.execute(
        "SELECT action, actor_kind, actor_person_id FROM decision_log ORDER BY id").fetchall())
    assert [row[0] for row in rows] == [
        "service_created", "service_updated", "entitlement_added", "entitlement_removed", "service_retired",
    ]
    assert {(row[1], row[2]) for row in rows} == {("admin", admin_id)}


# AC5.2 ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("subject_kind", ["type", "group", "person"])
async def test_grant_is_added_listed_and_removed_for_each_subject_kind(db: Database, admin_id: int,
                                                                       calendar: services.Service,
                                                                       subject_kind: str) -> None:
    subject = await subject_for(db, subject_kind)

    grant = await db.run(lambda c: services.add_entitlement(
        c, calendar.id, subject_kind, subject, "grant", actor_id=admin_id, now=NOW))

    assert (grant.service_id, grant.subject_kind, grant.subject, grant.effect) == (
        calendar.id, subject_kind, subject, "grant")
    assert await db.run(lambda c: services.entitlements_for(c, calendar.id)) == [grant]
    assert await db.run(lambda c: services.remove_entitlement(c, grant.id, actor_id=admin_id, now=NOW)) is True
    assert await db.run(lambda c: services.entitlements_for(c, calendar.id)) == []


async def test_removing_a_missing_entitlement_reports_nothing_removed(db: Database, admin_id: int) -> None:
    assert await db.run(lambda c: services.remove_entitlement(c, 999, actor_id=admin_id, now=NOW)) is False


async def test_person_deny_is_stored(db: Database, admin_id: int, calendar: services.Service) -> None:
    subject = await subject_for(db, "person")

    deny = await db.run(lambda c: services.add_entitlement(
        c, calendar.id, "person", subject, "deny", actor_id=admin_id, now=NOW))

    assert deny.effect == "deny"
    assert await db.run(lambda c: services.entitlements_for(c, calendar.id)) == [deny]


@pytest.mark.parametrize("subject_kind", ["type", "group"])
async def test_deny_on_a_type_or_group_row_is_refused_and_stores_nothing(db: Database, admin_id: int,
                                                                         calendar: services.Service,
                                                                         subject_kind: str) -> None:
    subject = await subject_for(db, subject_kind)

    with pytest.raises(EntitlementRefused) as info:
        await db.run(lambda c: services.add_entitlement(
            c, calendar.id, subject_kind, subject, "deny", actor_id=admin_id, now=NOW))

    assert str(info.value) == text.ENTITLEMENT_DENY_NOT_PERSON
    assert await db.run(lambda c: services.entitlements_for(c, calendar.id)) == []


async def test_unknown_account_type_subject_is_refused(db: Database, admin_id: int,
                                                       calendar: services.Service) -> None:
    with pytest.raises(ValueError, match="unknown account type"):
        await db.run(lambda c: services.add_entitlement(
            c, calendar.id, "type", "guardian", "grant", actor_id=admin_id, now=NOW))


async def test_unknown_group_subject_is_refused(db: Database, admin_id: int, calendar: services.Service) -> None:
    with pytest.raises(NoSuchGroup):
        await db.run(lambda c: services.add_entitlement(
            c, calendar.id, "group", "999", "grant", actor_id=admin_id, now=NOW))


async def test_unknown_person_subject_is_refused(db: Database, admin_id: int, calendar: services.Service) -> None:
    with pytest.raises(NoSuchPerson):
        await db.run(lambda c: services.add_entitlement(
            c, calendar.id, "person", "999", "grant", actor_id=admin_id, now=NOW))


async def test_entitlement_on_a_missing_service_is_refused(db: Database, admin_id: int) -> None:
    with pytest.raises(NoSuchService):
        await db.run(lambda c: services.add_entitlement(
            c, 999, "type", "adult", "grant", actor_id=admin_id, now=NOW))


async def test_the_same_subject_twice_on_one_service_is_refused(db: Database, admin_id: int,
                                                               calendar: services.Service) -> None:
    await db.run(lambda c: services.add_entitlement(
        c, calendar.id, "type", "adult", "grant", actor_id=admin_id, now=NOW))
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: services.add_entitlement(
            c, calendar.id, "type", "adult", "grant", actor_id=admin_id, now=NOW))
