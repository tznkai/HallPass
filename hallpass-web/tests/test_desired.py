# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Desired state: one row per monitored (service, subject) pair, its generation, and the row status the pages show.

Covers hallpass.AC9.1 (store), hallpass.AC9.8 (store) and the SSO-subject rule for services on the fluxer adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hallpass import desired, grants, holds, registry, services, terms, text
from hallpass.db import Database
from hallpass.registry import Person
from hallpass.services import Service

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
SSO_SUBJECT = "00000000-0000-4000-8000-0000000000a1"
OTHER_SUBJECT = "00000000-0000-4000-8000-0000000000a2"


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


async def make_person(db: Database, admin: Person, *, name: str, account_type: str = "adult") -> Person:
    """An adult or child who has accepted the current terms, so an entitlement makes them allowed."""
    def change(conn) -> Person:
        person = registry.create_person(conn, display_name=name, account_type=account_type, now=NOW)
        version = terms.current(conn) or terms.publish(conn, tos_text="Terms.", privacy_text="Privacy.",
                                                       blocking=True, actor_id=admin.id, now=NOW)
        grants.accept_terms(conn, person.id, version.id, NOW)
        return registry.get_person(conn, person.id)

    return await db.run(change)


async def make_service(db: Database, admin: Person, *, slug: str, kind: str = "game", adapter: str = "manual",
                       subject: str = "adult") -> Service:
    def change(conn) -> Service:
        service = services.create_service(conn, slug=slug, name=slug.title(), kind=kind, adapter=adapter,
                                          auto_suspendable=True, url=None, actor_id=admin.id, now=NOW)
        services.add_entitlement(conn, service.id, "type", subject, "grant", actor_id=admin.id, now=NOW)
        return service

    return await db.run(change)


async def add_identity(db: Database, person: Person, slug: str, kind: str, value: str) -> None:
    await db.run(lambda c: registry.add_identity(c, person.id, slug, kind, value))


async def bind(db: Database, person: Person, uuid: str) -> None:
    await db.run(lambda c: registry.bind_identity(c, person.id, uuid=uuid, authentik_pk=1, username="ann"))


async def refresh(db: Database, now: datetime = NOW) -> list[desired.DesiredRow]:
    return await db.run(lambda c: desired.refresh(c, now))


def generations(rows: list[desired.DesiredRow]) -> dict[tuple[str, int], int]:
    return {(row.service.slug, row.person.id): row.generation for row in rows}


# AC9.1 (store) --------------------------------------------------------------------------------------

async def test_each_subject_starts_at_generation_one(db: Database, admin: Person) -> None:
    await make_service(db, admin, slug="minecraft")
    ann = await make_person(db, admin, name="Ann")
    bo = await make_person(db, admin, name="Bo")
    unidentified = await make_person(db, admin, name="Cy")
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")
    await add_identity(db, bo, "minecraft", "java_name", "BuilderEight")

    rows = await refresh(db)

    assert [(row.service.slug, row.person.id, row.desired, row.generation) for row in rows] == [
        ("minecraft", ann.id, "allowed", 1),
        ("minecraft", bo.id, "allowed", 1),
    ]
    assert unidentified.id not in {row.person.id for row in rows}


async def test_a_hold_bumps_only_that_persons_generation(db: Database, admin: Person) -> None:
    await make_service(db, admin, slug="minecraft")
    ann = await make_person(db, admin, name="Ann")
    bo = await make_person(db, admin, name="Bo")
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")
    await add_identity(db, bo, "minecraft", "java_name", "BuilderEight")
    await refresh(db)

    def hold_ann(conn) -> None:
        holds.place(conn, actor=holds.Actor("admin", admin.id), target_id=ann.id, service_ids=None, now=LATER,
                     window=timedelta(hours=24), admin_email=None)

    await db.run(hold_ann)
    rows = await refresh(db, LATER)

    assert generations(rows) == {("minecraft", ann.id): 2, ("minecraft", bo.id): 1}
    assert [row.desired for row in rows if row.person.id == ann.id] == ["suspended"]


async def test_refresh_twice_with_no_change_keeps_generations(db: Database, admin: Person) -> None:
    await make_service(db, admin, slug="minecraft")
    ann = await make_person(db, admin, name="Ann")
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")

    first = await refresh(db)
    second = await refresh(db, LATER)

    assert generations(first) == generations(second) == {("minecraft", ann.id): 1}


async def test_rows_are_ordered_by_service_slug_then_person_id(db: Database, admin: Person) -> None:
    await make_service(db, admin, slug="valheim")
    await make_service(db, admin, slug="minecraft")
    bo = await make_person(db, admin, name="Bo")
    ann = await make_person(db, admin, name="Ann")
    await add_identity(db, ann, "valheim", "java_name", "ValheimOne")
    await add_identity(db, bo, "minecraft", "java_name", "BuilderEight")
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")

    rows = await refresh(db)

    assert [(row.service.slug, row.person.id) for row in rows] == [
        ("minecraft", min(bo.id, ann.id)), ("minecraft", max(bo.id, ann.id)), ("valheim", ann.id),
    ]


async def test_retired_and_forward_auth_services_are_not_monitored(db: Database, admin: Person) -> None:
    await make_service(db, admin, slug="calendar", kind="forward_auth")
    old = await make_service(db, admin, slug="old-game", kind="game")
    ann = await make_person(db, admin, name="Ann")
    await add_identity(db, ann, "calendar", "java_name", "BuilderSeven")
    await add_identity(db, ann, "old-game", "java_name", "BuilderSeven")
    await db.run(lambda c: services.retire_service(c, old.id, actor_id=admin.id, now=NOW))

    assert await refresh(db) == []


# SSO-subject services -----------------------------------------------------------------------------------

async def test_a_bound_person_without_an_identity_row_is_a_subject_on_a_fluxer_service(
        db: Database, admin: Person) -> None:
    await make_service(db, admin, slug="fluxer", kind="oidc", adapter="fluxer")
    ann = await make_person(db, admin, name="Ann")
    await bind(db, ann, SSO_SUBJECT)

    rows = await refresh(db)

    assert [(row.person.id, row.identities) for row in rows] == [(ann.id, (("sso_subject", SSO_SUBJECT),))]


async def test_an_unbound_person_is_not_a_subject_on_a_fluxer_service(db: Database, admin: Person) -> None:
    await make_service(db, admin, slug="fluxer", kind="oidc", adapter="fluxer")
    ann = await make_person(db, admin, name="Ann")
    await add_identity(db, ann, "fluxer", "fluxer_user_id", "1001")

    assert await refresh(db) == []


async def test_a_bound_person_with_a_fluxer_user_id_row_gets_sso_subject_first(db: Database, admin: Person) -> None:
    await make_service(db, admin, slug="fluxer", kind="oidc", adapter="fluxer")
    ann = await make_person(db, admin, name="Ann")
    await bind(db, ann, SSO_SUBJECT)
    await add_identity(db, ann, "fluxer", "fluxer_user_id", "1001")

    rows = await refresh(db)

    assert [row.identities for row in rows] == [(("sso_subject", SSO_SUBJECT), ("fluxer_user_id", "1001"))]


async def test_an_oidc_service_on_the_manual_adapter_lists_only_people_with_identity_rows(
        db: Database, admin: Person) -> None:
    await make_service(db, admin, slug="wiki", kind="oidc", adapter="manual")
    ann = await make_person(db, admin, name="Ann")
    await bind(db, ann, SSO_SUBJECT)
    bo = await make_person(db, admin, name="Bo")
    await add_identity(db, bo, "wiki", "java_name", "BuilderEight")

    rows = await refresh(db)

    assert [(row.person.id, row.identities) for row in rows] == [(bo.id, (("java_name", "BuilderEight"),))]


# AC9.8 (store): the row status for each report state ----------------------------------------------------

def report(observed: str, *, generation: int = 1, detail: str | None = None) -> desired.Report:
    return desired.Report(observed=observed, detail=detail, generation=generation, reported_at=NOW.isoformat())


@pytest.mark.parametrize(("desired_value", "found", "manual", "expected"), [
    pytest.param(("allowed", 1), None, False, text.NOT_YET_CONFIRMED, id="no-report"),
    pytest.param(("allowed", 2), report("allowed", generation=1), False, text.NOT_YET_CONFIRMED,
                 id="older-generation"),
    pytest.param(("allowed", 1), report("suspended"), False, text.NOT_YET_CONFIRMED, id="different-observed"),
    pytest.param(("allowed", 1), report("allowed"), False, text.STATUS_ALLOWED, id="match-allowed"),
    pytest.param(("suspended", 3), report("suspended", generation=3), False, text.STATUS_SUSPENDED,
                 id="match-suspended"),
    pytest.param(("suspended", 1), report("banned_external"), False, text.BANNED_IN_GAME, id="banned-external"),
    pytest.param(("suspended", 1), report("unsupported"), False, text.CANNOT_AUTO_SUSPEND, id="unsupported"),
    pytest.param(("suspended", 1), report("unsupported"), True, text.MANUALLY_SUSPENDED,
                 id="unsupported-marked"),
    pytest.param(("suspended", 1), report("error", detail="unreachable"), False, text.SERVICE_ERROR, id="error"),
    pytest.param(("suspended", 1), report("suspended", detail="pending_restart"), False, text.PENDING_RESTART,
                 id="pending-restart"),
])
def test_report_status_follows_the_row_rule(desired_value, found, manual, expected) -> None:
    assert desired.report_status(desired_value, found, manual) == expected


def test_report_status_is_none_for_an_unmonitored_service() -> None:
    assert desired.report_status(None, report("allowed"), False) is None


# Store helpers ------------------------------------------------------------------------------------------

async def test_desired_for_and_report_for_read_back_the_stored_rows(db: Database, admin: Person) -> None:
    minecraft = await make_service(db, admin, slug="minecraft")
    ann = await make_person(db, admin, name="Ann")
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")
    await refresh(db)

    assert await db.run(lambda c: desired.desired_for(c, ann.id, minecraft.id)) == ("allowed", 1)
    assert await db.run(lambda c: desired.report_for(c, ann.id, minecraft.id)) is None
    other = await make_service(db, admin, slug="valheim")
    assert await db.run(lambda c: desired.desired_for(c, ann.id, other.id)) is None


# Pruning: rows for pairs that stop being subjects ---------------------------------------------------------

async def store_report(db: Database, person: Person, service: services.Service, *, observed: str,
                       generation: int) -> None:
    def change(conn) -> None:
        conn.execute("INSERT INTO service_report (person_id, service_id, observed, detail, generation, reported_at) "
                     "VALUES (?, ?, ?, NULL, ?, ?)", (person.id, service.id, observed, generation, NOW.isoformat()))

    await db.run(change)


async def stored_rows(db: Database, person: Person, service: services.Service) -> tuple[object, object]:
    return await db.run(lambda c: (desired.desired_for(c, person.id, service.id),
                                   desired.report_for(c, person.id, service.id)))


async def test_a_removed_identity_prunes_its_desired_and_report_rows(db: Database, admin: Person) -> None:
    minecraft = await make_service(db, admin, slug="minecraft")
    ann = await make_person(db, admin, name="Ann")
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")
    await refresh(db)
    await store_report(db, ann, minecraft, observed="error", generation=1)

    def unbind(conn) -> None:
        (identity,) = [found for found in registry.identities_of(conn, ann.id) if found.service_slug == "minecraft"]
        registry.remove_identity(conn, identity.id)

    await db.run(unbind)

    assert await refresh(db, LATER) == []
    assert await stored_rows(db, ann, minecraft) == (None, None)


async def test_a_retired_service_prunes_its_desired_and_report_rows_and_its_unmanaged_count(
        db: Database, admin: Person) -> None:
    minecraft = await make_service(db, admin, slug="minecraft")
    valheim = await make_service(db, admin, slug="valheim")
    ann = await make_person(db, admin, name="Ann")
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")
    await add_identity(db, ann, "valheim", "java_name", "ValheimOne")
    await refresh(db)
    await store_report(db, ann, minecraft, observed="error", generation=1)
    await store_report(db, ann, valheim, observed="allowed", generation=1)

    def count_unmanaged(conn) -> None:
        for service in (minecraft, valheim):
            conn.execute("INSERT INTO service_unmanaged (service_id, count, reported_at) VALUES (?, 2, ?)",
                         (service.id, NOW.isoformat()))

    await db.run(count_unmanaged)
    await db.run(lambda c: services.retire_service(c, minecraft.id, actor_id=admin.id, now=NOW))

    assert [(row.service.slug, row.person.id) for row in await refresh(db, LATER)] == [("valheim", ann.id)]
    assert await stored_rows(db, ann, minecraft) == (None, None)
    assert await db.run(lambda c: c.execute("SELECT service_id FROM service_unmanaged ORDER BY service_id").fetchall()) \
        == [(valheim.id,)]


async def test_a_service_whose_kind_becomes_forward_auth_prunes_its_rows(db: Database, admin: Person) -> None:
    minecraft = await make_service(db, admin, slug="minecraft")
    ann = await make_person(db, admin, name="Ann")
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")
    await refresh(db)
    await store_report(db, ann, minecraft, observed="error", generation=1)

    await db.run(lambda c: c.execute("UPDATE service SET kind = 'forward_auth' WHERE id = ?", (minecraft.id,)))

    assert await refresh(db, LATER) == []
    assert await stored_rows(db, ann, minecraft) == (None, None)


async def test_a_pair_that_returns_starts_at_generation_one_with_no_old_report(db: Database, admin: Person) -> None:
    minecraft = await make_service(db, admin, slug="minecraft")
    ann = await make_person(db, admin, name="Ann")
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")
    await refresh(db)
    await store_report(db, ann, minecraft, observed="error", generation=5)

    def unbind(conn) -> None:
        (identity,) = [found for found in registry.identities_of(conn, ann.id) if found.service_slug == "minecraft"]
        registry.remove_identity(conn, identity.id)

    await db.run(unbind)
    await refresh(db, LATER)
    await add_identity(db, ann, "minecraft", "java_name", "BuilderSeven")
    rows = await refresh(db, LATER)

    assert generations(rows) == {("minecraft", ann.id): 1}
    desired_row, found = await stored_rows(db, ann, minecraft)
    assert (desired_row, found) == (("allowed", 1), None)
    assert desired.report_status(desired_row, found, False) == text.NOT_YET_CONFIRMED
