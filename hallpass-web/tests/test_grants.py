# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Status transitions and parent grants against a real SQLite database under tmp_path."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hallpass import grants, registry, terms
from hallpass.db import Database
from hallpass.grants import GrantRefused

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-13T12:00:00+00:00"


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


async def publish(db: Database, actor_id: int, *, blocking: bool = True) -> terms.TermsVersion:
    return await db.run(lambda c: terms.publish(c, tos_text="Terms", privacy_text="Privacy", blocking=blocking,
                                                actor_id=actor_id, now=NOW))


async def accept_terms(db: Database, person_id: int, version_id: int) -> registry.Person:
    return await db.run(lambda c: grants.accept_terms(c, person_id, version_id, NOW))


async def link(db: Database, child_id: int, parent_id: int) -> None:
    await db.run(lambda c: registry.link_parent(c, child_id, parent_id, NOW))


async def grant(db: Database, child_id: int, parent_id: int) -> registry.Person:
    return await db.run(lambda c: grants.grant(c, child_id=child_id, parent_id=parent_id, now=NOW))


async def sign_in_settle(db: Database, child_id: int) -> registry.Person:
    return await db.run(lambda c: grants.settle_child_at_sign_in(c, child_id))


async def refusal_reason(db: Database, child_id: int, parent_id: int) -> str:
    with pytest.raises(GrantRefused) as info:
        await grant(db, child_id, parent_id)
    return info.value.reason


async def status_of(db: Database, person_id: int) -> str:
    person = await db.run(lambda c: registry.get_person(c, person_id))
    assert person is not None
    return person.status


async def accepted_rows(db: Database) -> int:
    return await db.run(lambda c: c.execute("SELECT COUNT(*) FROM terms_acceptance").fetchone()[0])


@dataclass
class Family:
    version: terms.TermsVersion
    parent: registry.Person
    child: registry.Person


@pytest.fixture
async def family(db: Database) -> Family:
    """An active parent who accepted v1, and a child who has signed in (so awaiting_grant), linked to the parent."""
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    parent = await make(db, display_name="Pam", account_type="parent")
    child = await make(db, display_name="Kit", account_type="child")
    parent = await accept_terms(db, parent.id, version.id)
    await link(db, child.id, parent.id)
    child = await sign_in_settle(db, child.id)
    return Family(version=version, parent=parent, child=child)


# AC4.1: status transitions on first acceptance ----------------------------------------------------------

@pytest.mark.parametrize(("account_type", "expected"), [
    ("adult", "active"),
    ("parent", "active"),
], ids=["adult-active", "parent-active"])
async def test_first_acceptance_moves_invited_person_by_account_type(db: Database, account_type: str,
                                                                    expected: str) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    person = await make(db, display_name="Someone", account_type=account_type)

    accepted = await accept_terms(db, person.id, version.id)

    assert accepted.status == expected


async def test_a_child_cannot_accept_the_terms_and_nothing_is_recorded(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    child = await make(db, display_name="Kit", account_type="child")

    with pytest.raises(grants.ChildCannotAccept):
        await accept_terms(db, child.id, version.id)

    assert await accepted_rows(db) == 0
    assert await status_of(db, child.id) == "invited"


async def test_accepting_again_records_nothing_new_and_keeps_the_status(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    adult = await make(db, display_name="Ann")

    await accept_terms(db, adult.id, version.id)
    again = await accept_terms(db, adult.id, version.id)

    assert again.status == "active"
    accepted = await db.run(lambda c: c.execute(
        "SELECT detail FROM decision_log WHERE action = 'terms_accepted' AND target_person_id = ?",
        (adult.id,)).fetchall())
    assert accepted == [(json.dumps({"version": 1}, sort_keys=True),)]


async def test_later_acceptance_does_not_move_an_active_child_back(db: Database, family: Family) -> None:
    await grant(db, family.child.id, family.parent.id)
    publisher = await make(db, display_name="Second publisher")
    second = await publish(db, publisher.id)

    await accept_terms(db, family.parent.id, second.id)

    assert await status_of(db, family.child.id) == "active"


async def test_locked_person_acceptance_is_refused_and_not_stored(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    child = await make(db, display_name="Kit", account_type="child")
    await db.run(lambda c: registry.set_locked(c, child.id, True))

    with pytest.raises(grants.AcceptanceLocked):
        await accept_terms(db, child.id, version.id)

    stored = await db.run(lambda c: c.execute("SELECT COUNT(*) FROM terms_acceptance").fetchone()[0])
    logged = await db.run(lambda c: c.execute(
        "SELECT COUNT(*) FROM decision_log WHERE action = 'terms_accepted'").fetchone()[0])
    after = await db.run(lambda c: registry.get_person(c, child.id))
    assert stored == 0 and logged == 0
    assert after is not None and after.status == "locked"


@pytest.mark.parametrize(("account_type", "expected"), [
    ("adult", "active"),
    ("parent", "active"),
], ids=["adult-active", "parent-active"])
async def test_refused_locked_attempt_does_not_block_the_first_acceptance_after_unlock(
        db: Database, account_type: str, expected: str) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    person = await make(db, display_name="Someone", account_type=account_type)
    await db.run(lambda c: registry.set_locked(c, person.id, True))
    with pytest.raises(grants.AcceptanceLocked):
        await accept_terms(db, person.id, version.id)

    await db.run(lambda c: registry.set_locked(c, person.id, False))
    accepted = await accept_terms(db, person.id, version.id)

    assert accepted.status == expected
    assert await accepted_rows(db) == 1


async def test_accepting_an_unknown_version_is_refused(db: Database) -> None:
    person = await make(db, display_name="Someone")

    with pytest.raises(ValueError):
        await accept_terms(db, person.id, 999)


# Sign-in settles an invited child (enrolment is the sign-in) ----------------------------------------------

async def test_first_sign_in_without_a_grant_makes_the_child_awaiting_grant(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    await publish(db, publisher.id)
    child = await make(db, display_name="Kit", account_type="child")

    settled = await sign_in_settle(db, child.id)

    assert settled.status == "awaiting_grant"


async def test_first_sign_in_after_a_grant_makes_the_child_active(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    parent = await accept_terms(db, (await make(db, display_name="Pam", account_type="parent")).id, version.id)
    child = await make(db, display_name="Kit", account_type="child")
    await link(db, child.id, parent.id)
    await grant(db, child.id, parent.id)

    settled = await sign_in_settle(db, child.id)

    assert settled.status == "active"


async def test_sign_in_settle_leaves_a_locked_child_alone(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    await publish(db, publisher.id)
    child = await make(db, display_name="Kit", account_type="child")
    await db.run(lambda c: registry.set_locked(c, child.id, True))

    settled = await sign_in_settle(db, child.id)

    assert settled.status == "locked"


async def test_sign_in_settle_leaves_an_adult_alone(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    await publish(db, publisher.id)
    adult = await make(db, display_name="Ann")

    settled = await sign_in_settle(db, adult.id)

    assert settled.status == "invited"


# AC4.2 (status part): awaiting_grant holds until a grant -------------------------------------------------

async def test_child_stays_awaiting_grant_until_a_linked_parent_grants(db: Database, family: Family) -> None:
    assert await status_of(db, family.child.id) == "awaiting_grant"

    await accept_terms(db, family.parent.id, family.version.id)
    assert await status_of(db, family.child.id) == "awaiting_grant"


# AC4.1 and AC4.4: a linked parent grants -------------------------------------------------------------------

async def test_linked_parent_grant_makes_the_child_active_and_records_it(db: Database, family: Family) -> None:
    child = await grant(db, family.child.id, family.parent.id)

    assert child.status == "active"
    rows = await db.run(lambda c: c.execute(
        "SELECT child_id, parent_id, terms_version_id, granted_at FROM parent_grant").fetchall())
    assert rows == [(family.child.id, family.parent.id, family.version.id, NOW_TEXT)]


async def test_grant_writes_a_parent_decision_row(db: Database, family: Family) -> None:
    await grant(db, family.child.id, family.parent.id)

    rows = await db.run(lambda c: c.execute(
        "SELECT actor_kind, actor_person_id, target_person_id FROM decision_log WHERE action = 'granted'").fetchall())
    assert rows == [("parent", family.parent.id, family.child.id)]


async def test_grants_for_lists_the_parent_time_and_version(db: Database, family: Family) -> None:
    await grant(db, family.child.id, family.parent.id)

    listed = await db.run(lambda c: grants.grants_for(c, family.child.id))

    assert listed == [{"parent_id": family.parent.id, "display_name": "Pam", "granted_at": NOW_TEXT, "version": 1}]


# Grant before enrolment (Phase 8) ---------------------------------------------------------------------------

async def test_a_grant_before_enrolment_keeps_the_child_invited_and_is_recorded(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    parent = await accept_terms(db, (await make(db, display_name="Pam", account_type="parent")).id, version.id)
    child = await make(db, display_name="Kit", account_type="child")
    await link(db, child.id, parent.id)

    granted = await grant(db, child.id, parent.id)

    assert granted.status == "invited"
    assert len(await db.run(lambda c: grants.grants_for(c, child.id))) == 1


async def test_a_second_grant_on_an_invited_granted_child_is_refused_as_already_granted(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    parent = await accept_terms(db, (await make(db, display_name="Pam", account_type="parent")).id, version.id)
    child = await make(db, display_name="Kit", account_type="child")
    await link(db, child.id, parent.id)
    await grant(db, child.id, parent.id)

    reason = await refusal_reason(db, child.id, parent.id)

    assert reason == "already_granted"
    assert len(await db.run(lambda c: grants.grants_for(c, child.id))) == 1


async def test_a_grant_before_enrolment_then_sign_in_is_invited_to_active(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    parent = await accept_terms(db, (await make(db, display_name="Pam", account_type="parent")).id, version.id)
    child = await make(db, display_name="Kit", account_type="child")
    await link(db, child.id, parent.id)
    await grant(db, child.id, parent.id)

    settled = await sign_in_settle(db, child.id)

    assert settled.status == "active"


async def test_a_grant_after_sign_in_makes_the_awaiting_child_active(db: Database, family: Family) -> None:
    assert await status_of(db, family.child.id) == "awaiting_grant"

    child = await grant(db, family.child.id, family.parent.id)

    assert child.status == "active"


async def test_granting_an_active_child_is_refused_as_not_awaiting(db: Database, family: Family) -> None:
    await grant(db, family.child.id, family.parent.id)

    reason = await refusal_reason(db, family.child.id, family.parent.id)

    assert reason == "child_not_awaiting"


# AC4.3: only a linked parent can grant ------------------------------------------------------------------

async def test_unrelated_active_adult_is_refused_and_the_child_is_unchanged(db: Database, family: Family) -> None:
    stranger = await make(db, display_name="Stranger")
    await accept_terms(db, stranger.id, family.version.id)

    reason = await refusal_reason(db, family.child.id, stranger.id)

    assert reason == "not_linked_parent"
    assert await status_of(db, family.child.id) == "awaiting_grant"
    assert await db.run(lambda c: grants.grants_for(c, family.child.id)) == []


async def test_linked_parent_who_has_not_accepted_the_current_version_is_refused(db: Database,
                                                                                 family: Family) -> None:
    publisher = await make(db, display_name="Second publisher")
    await publish(db, publisher.id)   # the parent accepted v1 only; v2 is now current

    reason = await refusal_reason(db, family.child.id, family.parent.id)

    assert reason == "parent_terms"


async def test_linked_parent_who_has_never_accepted_is_refused(db: Database, family: Family) -> None:
    pat = await make(db, display_name="Pat", account_type="parent")
    await link(db, family.child.id, pat.id)

    reason = await refusal_reason(db, family.child.id, pat.id)

    assert reason == "parent_terms"


# Edge cases -------------------------------------------------------------------------------------------

async def test_a_locked_parent_is_refused_and_the_child_is_unchanged(db: Database, family: Family) -> None:
    await db.run(lambda c: registry.set_locked(c, family.parent.id, True))

    reason = await refusal_reason(db, family.child.id, family.parent.id)

    assert reason == "parent_inactive"
    assert await status_of(db, family.child.id) == "awaiting_grant"


async def test_granting_a_missing_child_is_refused_as_not_linked(db: Database, family: Family) -> None:
    reason = await refusal_reason(db, 999, family.parent.id)

    assert reason == "not_linked_parent"
