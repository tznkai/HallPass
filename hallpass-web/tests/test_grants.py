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

from hallpass import grants, holds, registry, terms, text
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
    return await db.run(lambda c: grants.settle_child_at_sign_in(c, child_id, NOW))


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


@pytest.mark.parametrize(("account_type", "display_name"), [
    ("adult", "Ann"),
    ("parent", "Pam"),
], ids=["adult", "parent"])
async def test_locked_person_acceptance_is_refused_and_not_stored(db: Database, account_type: str,
                                                                  display_name: str) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    person = await make(db, display_name=display_name, account_type=account_type)
    await db.run(lambda c: registry.set_locked(c, person.id, True))

    with pytest.raises(grants.AcceptanceLocked):
        await accept_terms(db, person.id, version.id)

    stored = await db.run(lambda c: c.execute("SELECT COUNT(*) FROM terms_acceptance").fetchone()[0])
    logged = await db.run(lambda c: c.execute(
        "SELECT COUNT(*) FROM decision_log WHERE action = 'terms_accepted'").fetchone()[0])
    after = await db.run(lambda c: registry.get_person(c, person.id))
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


# Only grants by a parent who is linked to the child now count ----------------------------------------------

async def test_an_unlinked_parents_grant_does_not_settle_the_child_to_active(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    mum = await accept_terms(db, (await make(db, display_name="Mum", account_type="parent")).id, version.id)
    child = await make(db, display_name="Kit", account_type="child")
    await link(db, child.id, mum.id)
    await grant(db, child.id, mum.id)
    await db.run(lambda c: registry.unlink_parent(c, child.id, mum.id))

    settled = await sign_in_settle(db, child.id)

    assert settled.status == "awaiting_grant"


async def test_a_linked_parent_can_approve_after_an_unlinked_parent_granted_before_enrolment(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    mum = await accept_terms(db, (await make(db, display_name="Mum", account_type="parent")).id, version.id)
    dad = await accept_terms(db, (await make(db, display_name="Dad", account_type="parent")).id, version.id)
    child = await make(db, display_name="Kit", account_type="child")
    await link(db, child.id, mum.id)
    await grant(db, child.id, mum.id)
    await db.run(lambda c: registry.unlink_parent(c, child.id, mum.id))
    await link(db, child.id, dad.id)

    granted = await grant(db, child.id, dad.id)

    assert granted.status == "invited"
    assert await db.run(lambda c: grants.grants_for(c, child.id)) == [
        {"parent_id": dad.id, "display_name": "Dad", "granted_at": NOW_TEXT, "version": 1}]


async def test_granting_a_missing_child_is_refused_as_not_linked(db: Database, family: Family) -> None:
    reason = await refusal_reason(db, 999, family.parent.id)

    assert reason == "not_linked_parent"


# The sign-in settle is logged when it changes a status -------------------------------------------------------

async def settled_rows(db: Database, child_id: int) -> list[tuple]:
    return await db.run(lambda c: c.execute(
        "SELECT at, actor_kind, actor_person_id, target_person_id, action, detail FROM decision_log "
        "WHERE action = 'status_settled' AND target_person_id = ? ORDER BY id", (child_id,)).fetchall())


async def test_sign_in_settle_logs_a_status_settled_row_when_the_status_changes(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    await publish(db, publisher.id)
    child = await make(db, display_name="Kit", account_type="child")

    await sign_in_settle(db, child.id)

    assert await settled_rows(db, child.id) == [
        (NOW_TEXT, "system", None, child.id, "status_settled", json.dumps({"status": "awaiting_grant"}, sort_keys=True)),
    ]


async def test_sign_in_settle_logs_active_when_a_grant_is_already_counted(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    parent = await accept_terms(db, (await make(db, display_name="Pam", account_type="parent")).id, version.id)
    child = await make(db, display_name="Kit", account_type="child")
    await link(db, child.id, parent.id)
    await grant(db, child.id, parent.id)

    await sign_in_settle(db, child.id)

    rows = await settled_rows(db, child.id)
    assert [json.loads(row[5]) for row in rows] == [{"status": "active"}]


async def test_sign_in_settle_writes_no_row_when_nothing_changes(db: Database, family: Family) -> None:
    await grant(db, family.child.id, family.parent.id)   # the child is now active
    before = await settled_rows(db, family.child.id)
    adult = await make(db, display_name="Ann")          # an adult is never settled
    locked = await make(db, display_name="Kit", account_type="child")
    await db.run(lambda c: registry.set_locked(c, locked.id, True))

    await sign_in_settle(db, family.child.id)
    await sign_in_settle(db, adult.id)
    await sign_in_settle(db, locked.id)

    assert await settled_rows(db, family.child.id) == before
    assert await settled_rows(db, adult.id) == []
    assert await settled_rows(db, locked.id) == []


async def test_a_status_settled_row_is_not_parent_history_and_shows_to_the_administrator(db: Database) -> None:
    publisher = await make(db, display_name="Publisher")
    await publish(db, publisher.id)
    child = await make(db, display_name="Kit", account_type="child")
    await sign_in_settle(db, child.id)

    history = await db.run(lambda c: holds._history(c, child.id))
    shown = await db.run(lambda c: holds.recent_decisions(c, child.id))

    assert history == []
    assert shown[0]["action"] == "status_settled"
    assert shown[0]["actor"] == text.ACTOR_SYSTEM


# A bound child who is invited again (set back by the administrator) is approved like an awaiting child -----------

async def set_status(db: Database, person_id: int, status: str) -> None:
    await db.run(lambda c: c.execute("UPDATE person SET status = ? WHERE id = ?", (status, person_id)))


@pytest.mark.parametrize("prior_grant", [False, True], ids=["no-prior-grant", "prior-grant"])
async def test_a_grant_on_a_bound_invited_child_makes_it_active(db: Database, family: Family,
                                                               prior_grant: bool) -> None:
    if prior_grant:
        await grant(db, family.child.id, family.parent.id)        # the child is active; the setup below reverts it
    await db.run(lambda c: registry.bind_identity(c, family.child.id, uuid="uuid-kit", authentik_pk=7,
                                                  username="kit"))
    await set_status(db, family.child.id, "invited")

    child = await grant(db, family.child.id, family.parent.id)

    assert child.status == "active"
    assert len(await db.run(lambda c: grants.grants_for(c, family.child.id))) == (2 if prior_grant else 1)


# A locked parent's grant does not count (Phase 8, Task 2) ---------------------------------------------------

@dataclass
class TwoParents:
    mum: registry.Person
    dad: registry.Person
    child: registry.Person


@pytest.fixture
async def two_parents(db: Database) -> TwoParents:
    """Two active parents who accepted v1, both linked to a child who has not signed in (invited)."""
    publisher = await make(db, display_name="Publisher")
    version = await publish(db, publisher.id)
    mum = await accept_terms(db, (await make(db, display_name="Mum", account_type="parent")).id, version.id)
    dad = await accept_terms(db, (await make(db, display_name="Dad", account_type="parent")).id, version.id)
    child = await make(db, display_name="Kit", account_type="child")
    await link(db, child.id, mum.id)
    await link(db, child.id, dad.id)
    return TwoParents(mum=mum, dad=dad, child=child)


async def lock(db: Database, person_id: int, locked: bool) -> None:
    await db.run(lambda c: registry.set_locked(c, person_id, locked))


async def test_a_locked_parents_grant_does_not_settle_the_child_to_active(db: Database,
                                                                         two_parents: TwoParents) -> None:
    await grant(db, two_parents.child.id, two_parents.mum.id)
    await lock(db, two_parents.mum.id, True)

    settled = await sign_in_settle(db, two_parents.child.id)

    assert settled.status == "awaiting_grant"


async def test_unlocking_the_parent_makes_their_grant_count_again_at_sign_in(db: Database,
                                                                             two_parents: TwoParents) -> None:
    await grant(db, two_parents.child.id, two_parents.mum.id)
    await lock(db, two_parents.mum.id, True)
    await lock(db, two_parents.mum.id, False)

    settled = await sign_in_settle(db, two_parents.child.id)

    assert settled.status == "active"


async def test_a_locked_parents_grant_does_not_block_another_linked_parents_approval(db: Database,
                                                                                    two_parents: TwoParents) -> None:
    await grant(db, two_parents.child.id, two_parents.mum.id)
    await lock(db, two_parents.mum.id, True)

    granted = await grant(db, two_parents.child.id, two_parents.dad.id)

    assert granted.status == "invited"
    assert await db.run(lambda c: grants.grants_for(c, two_parents.child.id)) == [
        {"parent_id": two_parents.dad.id, "display_name": "Dad", "granted_at": NOW_TEXT, "version": 1}]


async def test_unlocking_the_granting_parent_blocks_a_second_approval_again(db: Database,
                                                                           two_parents: TwoParents) -> None:
    await grant(db, two_parents.child.id, two_parents.mum.id)
    await lock(db, two_parents.mum.id, True)
    await lock(db, two_parents.mum.id, False)

    reason = await refusal_reason(db, two_parents.child.id, two_parents.dad.id)

    assert reason == "already_granted"


async def test_a_locked_parent_leaves_an_already_active_child_active(db: Database, family: Family) -> None:
    await grant(db, family.child.id, family.parent.id)

    await lock(db, family.parent.id, True)

    assert await status_of(db, family.child.id) == "active"


# An admin changes a person between child and another account type (Phase 8, Task 2) -----------------------

@pytest.fixture
async def published(db: Database) -> tuple[registry.Person, terms.TermsVersion]:
    publisher = await make(db, display_name="Publisher")
    return publisher, await publish(db, publisher.id)


async def active_adult(db: Database, version: terms.TermsVersion, *, bound: bool) -> registry.Person:
    adult = await accept_terms(db, (await make(db, display_name="Ann")).id, version.id)
    if bound:
        await db.run(lambda c: registry.bind_identity(c, adult.id, uuid=f"uuid-{adult.id}", authentik_pk=8,
                                                      username="ann"))
    person = await db.run(lambda c: registry.get_person(c, adult.id))
    assert person is not None
    return person


async def retype(db: Database, person_id: int, new_type: str, actor_id: int) -> registry.Person:
    """The admin route's change, in one transaction: the account type is updated, then the status moves."""
    def change(conn):
        before = registry.get_person(conn, person_id)
        assert before is not None
        registry.update_person(conn, person_id, display_name=before.display_name, account_type=new_type,
                               email=before.email, phone=before.phone,
                               fluxer_email_override=before.fluxer_email_override)
        grants.change_account_type(conn, person_id, old_type=before.account_type, new_type=new_type,
                                   actor_id=actor_id, now=NOW)
        person = registry.get_person(conn, person_id)
        assert person is not None
        return person
    return await db.run(change)


async def status_changes(db: Database, person_id: int) -> list[tuple]:
    rows = await db.run(lambda c: c.execute(
        "SELECT actor_kind, actor_person_id, action, detail FROM decision_log "
        "WHERE target_person_id = ? AND action = 'status_changed' ORDER BY id", (person_id,)).fetchall())
    return [(kind, actor, action, json.loads(detail)) for kind, actor, action, detail in rows]


async def test_an_active_bound_adult_made_a_child_is_awaiting_a_grant_and_logged(db: Database,
                                                                                 published) -> None:
    admin, version = published
    ann = await active_adult(db, version, bound=True)

    changed = await retype(db, ann.id, "child", admin.id)

    assert changed.status == "awaiting_grant"
    assert await status_changes(db, ann.id) == [
        ("admin", admin.id, "status_changed", {"prior_status": None, "status": "awaiting_grant"})]


async def test_an_active_unbound_adult_made_a_child_is_invited(db: Database, published) -> None:
    admin, version = published
    ann = await active_adult(db, version, bound=False)

    changed = await retype(db, ann.id, "child", admin.id)

    assert changed.status == "invited"


async def test_an_invited_adult_made_a_child_stays_invited_with_no_row(db: Database, published) -> None:
    admin, _ = published
    ann = await make(db, display_name="Ann")

    changed = await retype(db, ann.id, "child", admin.id)

    assert changed.status == "invited"
    assert await status_changes(db, ann.id) == []


async def test_changing_between_adult_and_parent_moves_nothing(db: Database, published) -> None:
    admin, version = published
    ann = await active_adult(db, version, bound=True)

    changed = await retype(db, ann.id, "parent", admin.id)

    assert changed.status == "active"
    assert await status_changes(db, ann.id) == []


@pytest.mark.parametrize(("bound", "waiting"), [(True, "awaiting_grant"), (False, "invited")],
                         ids=["bound", "unbound"])
async def test_a_locked_adult_made_a_child_stays_locked_and_unlocks_to_the_waiting_status(
        db: Database, published, bound: bool, waiting: str) -> None:
    admin, version = published
    ann = await active_adult(db, version, bound=bound)
    await lock(db, ann.id, True)

    changed = await retype(db, ann.id, "child", admin.id)
    assert changed.status == "locked"
    assert changed.prior_status == waiting
    assert await status_changes(db, ann.id) == [
        ("admin", admin.id, "status_changed", {"prior_status": waiting, "status": "locked"})]

    await lock(db, ann.id, False)
    assert await status_of(db, ann.id) == waiting


async def test_a_parents_grant_after_the_change_makes_the_child_active(db: Database, published) -> None:
    admin, version = published
    ann = await active_adult(db, version, bound=True)
    parent = await accept_terms(db, (await make(db, display_name="Pam", account_type="parent")).id, version.id)
    await retype(db, ann.id, "child", admin.id)
    await link(db, ann.id, parent.id)

    granted = await grant(db, ann.id, parent.id)

    assert granted.status == "active"


async def test_an_awaiting_child_made_an_adult_becomes_active_and_logged(db: Database, published) -> None:
    admin, _ = published
    kit = await make(db, display_name="Kit", account_type="child")
    await sign_in_settle(db, kit.id)

    changed = await retype(db, kit.id, "adult", admin.id)

    assert changed.status == "active"
    assert await status_changes(db, kit.id) == [
        ("admin", admin.id, "status_changed", {"prior_status": None, "status": "active"})]


async def test_a_locked_awaiting_child_made_an_adult_unlocks_to_active(db: Database, published) -> None:
    admin, _ = published
    kit = await make(db, display_name="Kit", account_type="child")
    await sign_in_settle(db, kit.id)
    await lock(db, kit.id, True)

    changed = await retype(db, kit.id, "adult", admin.id)
    assert changed.status == "locked"
    assert changed.prior_status == "active"

    await lock(db, kit.id, False)
    assert await status_of(db, kit.id) == "active"
