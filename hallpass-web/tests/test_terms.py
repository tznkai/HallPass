# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Terms store tests against a real SQLite database under tmp_path."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hallpass import registry, terms
from hallpass.db import Database

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hp.db")
    try:
        yield database
    finally:
        database.close()


async def make(db: Database, **fields) -> registry.Person:
    kwargs = {"display_name": "Someone", "account_type": "adult", "now": NOW, **fields}
    return await db.run(lambda c: registry.create_person(c, **kwargs))


async def make_bound(db: Database, name: str, number: int, *, status: str | None = None) -> registry.Person:
    person = await make(db, display_name=name)
    uuid = f"00000000-0000-4000-8000-{number:012d}"
    await db.run(lambda c: registry.bind_identity(c, person.id, uuid=uuid, authentik_pk=number, username=name.lower()))
    if status is not None:
        await db.run(lambda c: c.execute("UPDATE person SET status = ? WHERE id = ?", (status, person.id)))
    return person


async def publish(db: Database, actor_id: int, *, blocking: bool = True, tos: str = "Terms text",
                  privacy: str = "Privacy text") -> terms.TermsVersion:
    return await db.run(lambda c: terms.publish(c, tos_text=tos, privacy_text=privacy, blocking=blocking,
                                                actor_id=actor_id, now=NOW))


async def accept(db: Database, person_id: int, version_id: int, when: datetime = NOW) -> bool:
    return await db.run(lambda c: terms.accept(c, person_id, version_id, when))


# AC3.1 ----------------------------------------------------------------------------------------------

async def test_publishing_twice_makes_the_second_version_current(db: Database) -> None:
    admin = await make(db, display_name="Publisher")

    first = await publish(db, admin.id, blocking=True, tos="First")
    second = await publish(db, admin.id, blocking=False, tos="Second")

    assert (first.version, second.version) == (1, 2)
    assert second.blocking is False
    current = await db.run(terms.current)
    assert current is not None
    assert (current.id, current.version, current.tos_text) == (second.id, 2, "Second")
    assert await db.run(lambda c: terms.get(c, first.id)) == first


async def test_version_one_is_blocking_even_when_published_as_optional(db: Database) -> None:
    admin = await make(db, display_name="Publisher")

    first = await publish(db, admin.id, blocking=False)

    assert first.blocking is True
    stored = await db.run(terms.current)
    assert stored is not None and stored.blocking is True


async def test_publish_writes_a_decision_log_row(db: Database) -> None:
    admin = await make(db, display_name="Publisher")

    version = await publish(db, admin.id)

    rows = await db.run(lambda c: c.execute(
        "SELECT actor_kind, actor_person_id, target_person_id, action, detail FROM decision_log "
        "WHERE action = 'terms_published'").fetchall())
    assert rows == [("admin", admin.id, None, "terms_published", json.dumps({"version": version.version}))]


async def test_blank_terms_or_privacy_text_is_refused_and_stores_nothing(db: Database) -> None:
    admin = await make(db, display_name="Publisher")

    with pytest.raises(ValueError):
        await publish(db, admin.id, tos="   ")
    with pytest.raises(ValueError):
        await publish(db, admin.id, privacy="")

    assert await db.run(terms.current) is None


# Acceptance ------------------------------------------------------------------------------------------

async def test_accept_records_once_and_reports_only_the_first_time(db: Database) -> None:
    admin = await make(db, display_name="Publisher")
    person = await make(db, display_name="Ann")
    version = await publish(db, admin.id)

    assert await accept(db, person.id, version.id) is True
    assert await accept(db, person.id, version.id, LATER) is False

    rows = await db.run(lambda c: c.execute(
        "SELECT accepted_at FROM terms_acceptance WHERE person_id = ? AND terms_version_id = ?",
        (person.id, version.id)).fetchall())
    assert rows == [("2026-11-13T12:00:00+00:00",)]


# AC3.3 ----------------------------------------------------------------------------------------------

async def test_acceptance_report_splits_each_version_into_accepted_and_not_accepted(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")           # unbound: never counted
    ann = await make_bound(db, "Ann", 1)
    ben = await make_bound(db, "Ben", 2)
    cat = await make_bound(db, "Cat", 3)
    await make_bound(db, "Dan", 4, status="locked")                # locked: never counted as not accepted
    await make(db, display_name="Eve")                              # unbound: never counted
    v1 = await publish(db, publisher.id, blocking=True)
    v2 = await publish(db, publisher.id, blocking=False)
    await accept(db, ann.id, v1.id, NOW)
    await accept(db, ben.id, v1.id, LATER)
    await accept(db, cat.id, v2.id, NOW)

    accepted_1, missing_1 = await db.run(lambda c: terms.acceptance_report(c, v1.id))
    accepted_2, missing_2 = await db.run(lambda c: terms.acceptance_report(c, v2.id))

    assert [(p.display_name, at) for p, at in accepted_1] == [("Ann", NOW.isoformat()), ("Ben", LATER.isoformat())]
    assert [p.display_name for p in missing_1] == ["Cat"]
    assert [(p.display_name, at) for p, at in accepted_2] == [("Cat", NOW.isoformat())]
    assert [p.display_name for p in missing_2] == ["Ann", "Ben"]


# AC3.5 ----------------------------------------------------------------------------------------------

async def state_after(db: Database, publishes: list[bool], accepts: list[int]) -> terms.TermsState:
    """Publishes one version per flag, then has one adult accept the listed version numbers."""
    publisher = await make(db, display_name="Publisher")
    person = await make(db, display_name="Ann")
    ids = [(await publish(db, publisher.id, blocking=flag)).id for flag in publishes]
    for number in accepts:
        await accept(db, person.id, ids[number - 1])
    return await db.run(lambda c: terms.state_for(c, person.id))


GATE_CASES = [
    pytest.param([], [], True, id="no-version"),
    pytest.param([True], [], True, id="never-accepted"),
    pytest.param([True, False], [1], False, id="accepted-v1-then-optional-v2"),
    pytest.param([True, True], [1], True, id="accepted-v1-then-blocking-v2"),
    pytest.param([True, True], [2], False, id="accepted-v2"),
]


@pytest.mark.parametrize(("publishes", "accepts", "expected"), GATE_CASES)
async def test_services_blocked_follows_the_terms_rule(db: Database, publishes: list[bool], accepts: list[int],
                                                       expected: bool) -> None:
    state = await state_after(db, publishes, accepts)

    assert terms.services_blocked(state) is expected


@pytest.mark.parametrize(("publishes", "accepts", "expected"), GATE_CASES)
async def test_pages_blocked_follows_the_terms_rule(db: Database, publishes: list[bool], accepts: list[int],
                                                    expected: bool) -> None:
    state = await state_after(db, publishes, accepts)

    assert terms.pages_blocked(state) is expected
