# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Holds against a real database: placing and lifting, the admin and parent rules, lockdown and review, the conflict
modes, manual marks, hold labels and the change emails. Covers hallpass.AC6.1-AC6.8, AC7.1-AC7.9 (store level), AC8.2
(labels) and the restore rule."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hallpass import access, holds, registry, services, text
from hallpass.db import Database

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=24)
ADMIN_EMAIL = "admin@example.com"
ANN_EMAIL = "ann@example.com"
BEN_EMAIL = "ben@example.com"


@dataclass(frozen=True)
class World:
    db: Database
    admin: int
    child: int
    ann: int        # a linked parent (parent A in the plan)
    ben: int        # a linked parent (parent B in the plan)
    cy: int         # a parent with no link to the child
    adult: int
    fluxer: int     # auto-suspendable
    abiotic: int    # not auto-suspendable


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hp.db")
    try:
        yield database
    finally:
        database.close()


async def build(db: Database, *, mode: str = "default", note: str | None = None) -> World:
    def setup(conn) -> World:
        admin = registry.create_person(conn, display_name="Admin", account_type="adult", now=NOW).id
        child = registry.create_person(conn, display_name="Kid", account_type="child", now=NOW).id
        ann = registry.create_person(conn, display_name="Ann", account_type="parent", email=ANN_EMAIL, now=NOW).id
        ben = registry.create_person(conn, display_name="Ben", account_type="parent", email=BEN_EMAIL, now=NOW).id
        cy = registry.create_person(conn, display_name="Cy", account_type="parent", email="cy@example.com", now=NOW).id
        adult = registry.create_person(conn, display_name="Dee", account_type="adult", now=NOW).id
        registry.link_parent(conn, child, ann, NOW)
        registry.link_parent(conn, child, ben, NOW)
        fluxer = services.create_service(conn, slug="fluxer", name="Fluxer", kind="oidc", adapter="fluxer",
                                         auto_suspendable=True, url=None, actor_id=admin, now=NOW).id
        abiotic = services.create_service(conn, slug="abiotic", name="Abiotic", kind="game", adapter="manual",
                                          auto_suspendable=False, url=None, actor_id=admin, now=NOW).id
        for service_id in (fluxer, abiotic):
            services.add_entitlement(conn, service_id, "type", "child", "grant", actor_id=admin, now=NOW)
            services.add_entitlement(conn, service_id, "type", "adult", "grant", actor_id=admin, now=NOW)
        conn.execute("UPDATE person SET conflict_mode = ?, agreement_note = ? WHERE id = ?", (mode, note, child))
        return World(db=db, admin=admin, child=child, ann=ann, ben=ben, cy=cy, adult=adult, fluxer=fluxer,
                     abiotic=abiotic)

    return await db.run(setup)


def parent(person_id: int) -> holds.Actor:
    return holds.Actor(kind="parent", person_id=person_id)


def admin(w: World) -> holds.Actor:
    return holds.Actor(kind="admin", person_id=w.admin)


async def place(w: World, actor: holds.Actor, target: int, service_ids=None, *, at: datetime = NOW,
                reason: str = "") -> holds.Outcome:
    return await w.db.run(lambda c: holds.place(
        c, actor=actor, target_id=target, service_ids=service_ids, reason=reason, now=at, window=WINDOW,
        admin_email=ADMIN_EMAIL))


async def lift(w: World, actor: holds.Actor, target: int, service_ids, *, at: datetime = NOW,
               include_parent: bool = False) -> holds.Outcome:
    return await w.db.run(lambda c: holds.lift(
        c, actor=actor, target_id=target, service_ids=service_ids, now=at, window=WINDOW, admin_email=ADMIN_EMAIL,
        include_parent=include_parent))


async def hold_kinds(w: World, person: int, service: int) -> list[str]:
    return await w.db.run(lambda c: [h.kind for h in access.holds_for(c, person, service)])


async def hold_rows(w: World, person: int, service: int) -> list[access.Hold]:
    return await w.db.run(lambda c: access.holds_for(c, person, service))


async def outbox(w: World) -> list[tuple[str, str, str]]:
    return await w.db.run(lambda c: [tuple(r) for r in c.execute(
        "SELECT to_addr, subject, body FROM outbox ORDER BY id").fetchall()])


async def review_rows(w: World) -> list[tuple]:
    return await w.db.run(lambda c: [tuple(r) for r in c.execute(
        "SELECT id, child_id, resolved_at, resolved_by, outcome FROM review_item ORDER BY id").fetchall()])


async def lockdown_holds(w: World, person: int) -> list[access.Hold]:
    return await w.db.run(lambda c: [h for sid in (w.fluxer, w.abiotic)
                                     for h in access.holds_for(c, person, sid) if h.kind == "lockdown"])


async def decision_rows(w: World, action: str) -> list[tuple]:
    return await w.db.run(lambda c: [tuple(r) for r in c.execute(
        "SELECT at, actor_kind, actor_person_id, target_person_id, services, reversal FROM decision_log "
        "WHERE action = ? ORDER BY id", (action,)).fetchall()])


async def run_sequence(w: World, mode_steps: list) -> list[holds.Outcome]:
    """Runs (actor, kind, at) steps against the child on the fluxer service: 'place' or 'lift'."""
    outcomes = []
    for actor_id, kind, at in mode_steps:
        if kind == "place":
            outcomes.append(await place(w, parent(actor_id), w.child, [w.fluxer], at=at))
        else:
            outcomes.append(await lift(w, parent(actor_id), w.child, [w.fluxer], at=at))
    return outcomes


# AC6.1 -------------------------------------------------------------------------------------------------

async def test_revoke_everything_places_holds_on_every_service(db: Database) -> None:
    w = await build(db)
    outcome = await place(w, parent(w.ann), w.child, None)
    assert set(outcome.changed_services) == {w.fluxer, w.abiotic}
    assert await hold_kinds(w, w.child, w.fluxer) == ["parent"]
    assert await hold_kinds(w, w.child, w.abiotic) == ["parent"]


async def test_single_service_revoke_places_one_hold(db: Database) -> None:
    w = await build(db)
    await place(w, parent(w.ann), w.child, [w.fluxer])
    assert await hold_kinds(w, w.child, w.fluxer) == ["parent"]
    assert await hold_kinds(w, w.child, w.abiotic) == []


# AC6.2 -------------------------------------------------------------------------------------------------

async def test_admin_places_and_lifts_admin_holds_for_all_and_for_one(db: Database) -> None:
    w = await build(db)
    await place(w, admin(w), w.adult, None)
    assert await hold_kinds(w, w.adult, w.fluxer) == ["admin"]
    assert await hold_kinds(w, w.adult, w.abiotic) == ["admin"]
    await lift(w, admin(w), w.adult, [w.fluxer])
    assert await hold_kinds(w, w.adult, w.fluxer) == []
    assert await hold_kinds(w, w.adult, w.abiotic) == ["admin"]
    await lift(w, admin(w), w.adult, [w.abiotic])
    assert await hold_kinds(w, w.adult, w.abiotic) == []


async def test_admin_hold_on_an_already_held_service_is_not_duplicated(db: Database) -> None:
    w = await build(db)
    await place(w, admin(w), w.adult, [w.fluxer])
    again = await place(w, admin(w), w.adult, [w.fluxer])
    assert again.changed_services == ()
    assert await hold_kinds(w, w.adult, w.fluxer) == ["admin"]


# AC6.3 -------------------------------------------------------------------------------------------------

async def test_parent_lift_leaves_admin_hold_in_force(db: Database) -> None:
    w = await build(db)
    await place(w, parent(w.ann), w.child, [w.fluxer])
    await place(w, admin(w), w.child, [w.fluxer])
    await lift(w, parent(w.ann), w.child, [w.fluxer])
    assert await hold_kinds(w, w.child, w.fluxer) == ["admin"]

    decision = await w.db.run(lambda c: access.evaluate(
        c, registry.get_person(c, w.child), services.get(c, w.fluxer)))
    assert decision.desired == "suspended"
    assert "held" in decision.reasons


# AC6.6 -------------------------------------------------------------------------------------------------

async def test_unrelated_parent_cannot_place_or_lift(db: Database) -> None:
    w = await build(db)
    with pytest.raises(holds.HoldRefused) as placed:
        await place(w, parent(w.cy), w.child, None)
    assert placed.value.reason == "not_your_child"
    assert await hold_kinds(w, w.child, w.fluxer) == []

    await place(w, parent(w.ann), w.child, [w.fluxer])
    with pytest.raises(holds.HoldRefused) as lifted:
        await lift(w, parent(w.cy), w.child, [w.fluxer])
    assert lifted.value.reason == "not_your_child"
    assert await hold_kinds(w, w.child, w.fluxer) == ["parent"]


async def test_parent_cannot_lift_an_admin_hold(db: Database) -> None:
    w = await build(db)
    await place(w, admin(w), w.child, [w.fluxer])
    with pytest.raises(holds.HoldRefused) as refused:
        await lift(w, parent(w.ann), w.child, [w.fluxer])
    assert refused.value.reason == "admin_or_lockdown"
    assert await hold_kinds(w, w.child, w.fluxer) == ["admin"]


# AC6.7 -------------------------------------------------------------------------------------------------

async def test_each_place_and_lift_is_logged_with_actor_target_services_and_time(db: Database) -> None:
    w = await build(db)
    await place(w, parent(w.ann), w.child, [w.fluxer])
    await lift(w, parent(w.ann), w.child, [w.fluxer], at=NOW + timedelta(hours=1))
    placed, lifted = await decision_rows(w, "hold_placed"), await decision_rows(w, "hold_lifted")
    assert len(placed) == 1 and len(lifted) == 1
    at, actor_kind, actor_id, target, services_json, reversal = placed[0]
    assert datetime.fromisoformat(at) == NOW
    assert (actor_kind, actor_id, target) == ("parent", w.ann, w.child)
    assert json.loads(services_json) == [w.fluxer]
    assert reversal == 0
    assert datetime.fromisoformat(lifted[0][0]) == NOW + timedelta(hours=1)


# AC6.8 -------------------------------------------------------------------------------------------------

async def test_parent_place_emails_the_other_parent_and_not_the_actor(db: Database) -> None:
    w = await build(db)
    await place(w, parent(w.ann), w.child, [w.fluxer])
    mail = await outbox(w)
    assert [to for to, _, _ in mail] == [BEN_EMAIL]
    _, subject, body = mail[0]
    assert "Kid" in subject
    assert "Ann" in body
    assert "Kid" in body


async def test_admin_change_emails_every_linked_parent(db: Database) -> None:
    w = await build(db)
    await place(w, admin(w), w.child, [w.fluxer])
    assert sorted(to for to, _, _ in await outbox(w)) == sorted([ANN_EMAIL, BEN_EMAIL])


async def test_outbox_rows_hold_no_mail_when_a_parent_has_no_email(db: Database) -> None:
    w = await build(db)
    await w.db.run(lambda c: registry.update_person(
        c, w.ben, display_name="Ben", account_type="parent", email=None, phone=None, fluxer_email_override=None))
    await place(w, parent(w.ann), w.child, [w.fluxer])
    assert await outbox(w) == []


# AC7.1 cooperative -------------------------------------------------------------------------------------

async def test_cooperative_mode_never_locks_down(db: Database) -> None:
    w = await build(db, mode="cooperative")
    outcomes = await run_sequence(w, [
        (w.ann, "place", NOW), (w.ben, "lift", NOW), (w.ann, "place", NOW), (w.ben, "lift", NOW),
    ])
    assert all(not o.reversal and o.lockdown_review_id is None for o in outcomes)
    assert await review_rows(w) == []


# AC7.2 default, first reversal -------------------------------------------------------------------------

async def test_first_cross_parent_reversal_is_allowed_without_lockdown(db: Database) -> None:
    w = await build(db)
    first, second = await run_sequence(w, [(w.ann, "place", NOW), (w.ben, "lift", NOW)])
    assert first.reversal is False
    assert second.reversal is True
    assert second.lockdown_review_id is None
    assert await review_rows(w) == []


# AC7.3 default, second reversal ------------------------------------------------------------------------

async def test_second_reversal_within_window_locks_down_every_service(db: Database) -> None:
    w = await build(db)
    await run_sequence(w, [(w.ann, "place", NOW - timedelta(hours=2)), (w.ben, "lift", NOW - timedelta(hours=1))])
    third = await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW)
    assert third.reversal is True
    assert third.lockdown_review_id is not None

    assert {h.service_id for h in await lockdown_holds(w, w.child)} == {w.fluxer, w.abiotic}
    reviews = await review_rows(w)
    assert len(reviews) == 1
    assert reviews[0][2] is None                              # still open


async def test_lockdown_emails_each_parent_and_the_admin(db: Database) -> None:
    w = await build(db)
    await run_sequence(w, [(w.ann, "place", NOW - timedelta(hours=2)), (w.ben, "lift", NOW - timedelta(hours=1))])
    await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW)
    lockdown_subject = text.LOCKDOWN_EMAIL_SUBJECT.format(child="Kid")
    addresses = sorted(to for to, subject, _ in await outbox(w) if subject == lockdown_subject)
    assert addresses == sorted([ANN_EMAIL, BEN_EMAIL, ADMIN_EMAIL])


async def test_lockdown_sequence_at_one_instant_still_locks_down(db: Database) -> None:
    """Decision times have second resolution, so the reversal must be found by log order, not by time."""
    w = await build(db)
    outcomes = await run_sequence(w, [(w.ann, "place", NOW), (w.ben, "lift", NOW), (w.ann, "place", NOW)])
    assert outcomes[1].reversal is True
    assert outcomes[2].reversal is True
    assert outcomes[2].lockdown_review_id is not None


# AC7.4 default, no lockdown cases ----------------------------------------------------------------------

async def test_reversals_further_apart_than_the_window_cause_no_lockdown(db: Database) -> None:
    w = await build(db)
    await run_sequence(w, [(w.ann, "place", NOW - timedelta(hours=26)), (w.ben, "lift", NOW - timedelta(hours=25))])
    third = await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW)
    assert third.reversal is True
    assert third.lockdown_review_id is None
    assert await review_rows(w) == []


async def test_reversal_exactly_at_the_window_edge_is_outside(db: Database) -> None:
    w = await build(db)
    await run_sequence(w, [(w.ann, "place", NOW - timedelta(hours=25)), (w.ben, "lift", NOW - timedelta(hours=24))])
    third = await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW)
    assert third.lockdown_review_id is None


async def test_parent_reversing_their_own_decision_is_not_a_reversal(db: Database) -> None:
    w = await build(db)
    outcomes = await run_sequence(w, [(w.ann, "place", NOW), (w.ann, "lift", NOW), (w.ann, "place", NOW)])
    assert all(not o.reversal for o in outcomes)
    assert await review_rows(w) == []


# AC7.5 monitored ---------------------------------------------------------------------------------------

async def test_monitored_lockdown_quotes_the_agreement_note(db: Database) -> None:
    w = await build(db, mode="monitored", note="Weekdays only")
    await run_sequence(w, [(w.ann, "place", NOW - timedelta(hours=2)), (w.ben, "lift", NOW - timedelta(hours=1))])
    await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW)
    reviews = await w.db.run(lambda c: c.execute("SELECT reason FROM review_item").fetchall())
    assert "Weekdays only" in reviews[0][0]
    lockdown_body = next(body for _, subject, body in await outbox(w)
                         if subject == text.LOCKDOWN_EMAIL_SUBJECT.format(child="Kid"))
    assert "Weekdays only" in lockdown_body


async def test_default_lockdown_reason_does_not_quote_a_note(db: Database) -> None:
    w = await build(db, mode="default", note="Weekdays only")
    await run_sequence(w, [(w.ann, "place", NOW - timedelta(hours=2)), (w.ben, "lift", NOW - timedelta(hours=1))])
    await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW)
    reviews = await w.db.run(lambda c: c.execute("SELECT reason FROM review_item").fetchall())
    assert "Weekdays only" not in reviews[0][0]


# AC7.6 strict: each parent's hold is separate ----------------------------------------------------------

async def test_strict_child_stays_suspended_until_every_parent_lifts(db: Database) -> None:
    w = await build(db, mode="strict")
    await place(w, parent(w.ann), w.child, [w.fluxer])
    await place(w, parent(w.ben), w.child, [w.fluxer])
    assert await hold_kinds(w, w.child, w.fluxer) == ["parent", "parent"]

    await lift(w, parent(w.ann), w.child, [w.fluxer])
    remaining = await hold_rows(w, w.child, w.fluxer)
    assert [h.placed_by for h in remaining] == [w.ben]
    decision = await w.db.run(lambda c: access.evaluate(
        c, registry.get_person(c, w.child), services.get(c, w.fluxer)))
    assert decision.desired == "suspended"

    await lift(w, parent(w.ben), w.child, [w.fluxer])
    assert await hold_kinds(w, w.child, w.fluxer) == []


async def test_strict_mode_never_locks_down(db: Database) -> None:
    w = await build(db, mode="strict")
    outcomes = [
        await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW),
        await place(w, parent(w.ben), w.child, [w.fluxer], at=NOW),
        await lift(w, parent(w.ann), w.child, [w.fluxer], at=NOW),
        await lift(w, parent(w.ben), w.child, [w.fluxer], at=NOW),
        await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW),
    ]
    assert all(not o.reversal and o.lockdown_review_id is None for o in outcomes)
    assert await review_rows(w) == []


# AC7.7 strict: refused lift -----------------------------------------------------------------------------

async def test_strict_refuses_a_parent_lifting_another_parents_hold(db: Database) -> None:
    w = await build(db, mode="strict")
    await place(w, parent(w.ben), w.child, [w.fluxer])
    with pytest.raises(holds.HoldRefused) as refused:
        await lift(w, parent(w.ann), w.child, [w.fluxer])
    assert refused.value.reason == "strict_not_yours"
    assert "Ben" in refused.value.message
    assert await hold_kinds(w, w.child, w.fluxer) == ["parent"]


# AC7.8 lockdown and review -----------------------------------------------------------------------------

async def lock_child(w: World) -> int:
    await run_sequence(w, [(w.ann, "place", NOW - timedelta(hours=2)), (w.ben, "lift", NOW - timedelta(hours=1))])
    outcome = await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW)
    assert outcome.lockdown_review_id is not None
    return outcome.lockdown_review_id


async def test_parent_lift_during_a_lockdown_leaves_the_lockdown(db: Database) -> None:
    w = await build(db)
    await lock_child(w)
    # Ann's own parent hold is lifted; the lockdown stays.
    await lift(w, parent(w.ann), w.child, [w.fluxer], at=NOW + timedelta(days=2))
    assert await hold_kinds(w, w.child, w.fluxer) == ["lockdown"]


async def test_parent_cannot_lift_a_service_held_only_by_a_lockdown(db: Database) -> None:
    w = await build(db)
    await lock_child(w)
    await lift(w, parent(w.ann), w.child, [w.fluxer], at=NOW + timedelta(days=2))
    with pytest.raises(holds.HoldRefused) as refused:
        await lift(w, parent(w.ann), w.child, [w.fluxer], at=NOW + timedelta(days=2))
    assert refused.value.reason == "admin_or_lockdown"
    assert await hold_kinds(w, w.child, w.fluxer) == ["lockdown"]


async def test_admin_lift_of_parent_holds_leaves_the_lockdown(db: Database) -> None:
    w = await build(db)
    await lock_child(w)
    await lift(w, admin(w), w.child, [w.fluxer, w.abiotic], include_parent=True)
    assert await hold_kinds(w, w.child, w.fluxer) == ["lockdown"]
    assert await hold_kinds(w, w.child, w.abiotic) == ["lockdown"]


async def test_resolving_the_review_with_lift_removes_the_lockdown(db: Database) -> None:
    w = await build(db)
    review_id = await lock_child(w)
    await w.db.run(lambda c: holds.resolve_review(
        c, admin_id=w.admin, review_id=review_id, outcome="Agreed to keep to weekdays", lift_lockdown=True, now=NOW))
    assert await lockdown_holds(w, w.child) == []
    reviews = await review_rows(w)
    assert reviews[0][2] is not None
    assert reviews[0][3] == w.admin
    assert reviews[0][4] == "Agreed to keep to weekdays"


async def test_resolving_without_lift_records_the_outcome_only(db: Database) -> None:
    w = await build(db)
    review_id = await lock_child(w)
    await w.db.run(lambda c: holds.resolve_review(
        c, admin_id=w.admin, review_id=review_id, outcome="Reviewed, keep the lockdown", lift_lockdown=False, now=NOW))
    assert {h.service_id for h in await lockdown_holds(w, w.child)} == {w.fluxer, w.abiotic}
    assert (await review_rows(w))[0][4] == "Reviewed, keep the lockdown"


async def test_resolved_review_cannot_be_resolved_again(db: Database) -> None:
    w = await build(db)
    review_id = await lock_child(w)
    await w.db.run(lambda c: holds.resolve_review(
        c, admin_id=w.admin, review_id=review_id, outcome="First", lift_lockdown=False, now=NOW))
    with pytest.raises(ValueError):
        await w.db.run(lambda c: holds.resolve_review(
            c, admin_id=w.admin, review_id=review_id, outcome="Second", lift_lockdown=False, now=NOW))


async def test_resolving_an_unknown_review_is_refused(db: Database) -> None:
    w = await build(db)
    with pytest.raises(holds.NoSuchReview):
        await w.db.run(lambda c: holds.resolve_review(
            c, admin_id=w.admin, review_id=999, outcome="x", lift_lockdown=False, now=NOW))


# AC7.9 conflict modes -----------------------------------------------------------------------------------

async def test_set_mode_applies_to_siblings_with_the_same_parents_only(db: Database) -> None:
    w = await build(db)

    def add_children(conn) -> tuple[int, int]:
        same = registry.create_person(conn, display_name="Eli", account_type="child", now=NOW).id
        registry.link_parent(conn, same, w.ann, NOW)
        registry.link_parent(conn, same, w.ben, NOW)
        partial = registry.create_person(conn, display_name="Fay", account_type="child", now=NOW).id
        registry.link_parent(conn, partial, w.ann, NOW)
        return same, partial

    same, partial = await w.db.run(add_children)
    changed = await w.db.run(lambda c: holds.set_mode(
        c, child_id=w.child, mode="strict", agreement_note="Weekdays only", apply_to_siblings=True,
        admin_id=w.admin, now=NOW))
    assert sorted(changed) == sorted([w.child, same])

    modes = await w.db.run(lambda c: {pid: (registry.get_person(c, pid).conflict_mode,
                                            registry.get_person(c, pid).agreement_note)
                                      for pid in (w.child, same, partial)})
    assert modes[w.child] == ("strict", "Weekdays only")
    assert modes[same] == ("strict", None)
    assert modes[partial] == ("default", None)


async def test_set_mode_without_siblings_changes_only_the_child(db: Database) -> None:
    w = await build(db)
    changed = await w.db.run(lambda c: holds.set_mode(
        c, child_id=w.child, mode="cooperative", agreement_note="", apply_to_siblings=False,
        admin_id=w.admin, now=NOW))
    assert changed == [w.child]
    assert await w.db.run(lambda c: registry.get_person(c, w.child).conflict_mode) == "cooperative"


async def test_set_mode_refuses_an_unknown_mode(db: Database) -> None:
    w = await build(db)
    with pytest.raises(ValueError):
        await w.db.run(lambda c: holds.set_mode(
            c, child_id=w.child, mode="lenient", agreement_note="", apply_to_siblings=False,
            admin_id=w.admin, now=NOW))


# Restore rule and manual marks --------------------------------------------------------------------------

async def test_parent_cannot_restore_a_service_that_is_not_auto_suspendable(db: Database) -> None:
    w = await build(db)
    await place(w, parent(w.ann), w.child, [w.abiotic])
    with pytest.raises(holds.HoldRefused) as refused:
        await lift(w, parent(w.ann), w.child, [w.abiotic])
    assert refused.value.reason == "restore_not_automatic"
    assert await hold_kinds(w, w.child, w.abiotic) == ["parent"]


async def test_mark_manual_needs_a_hold_on_a_non_auto_service(db: Database) -> None:
    w = await build(db)
    with pytest.raises(holds.HoldRefused) as no_hold:
        await w.db.run(lambda c: holds.mark_manual(c, admin_id=w.admin, person_id=w.child,
                                                   service_id=w.abiotic, now=NOW))
    assert no_hold.value.reason == "no_hold"

    await place(w, admin(w), w.child, [w.fluxer])
    with pytest.raises(holds.HoldRefused) as automatic:
        await w.db.run(lambda c: holds.mark_manual(c, admin_id=w.admin, person_id=w.child,
                                                   service_id=w.fluxer, now=NOW))
    assert automatic.value.reason == "auto_suspendable"


async def test_manual_mark_is_cleared_when_the_last_hold_goes(db: Database) -> None:
    w = await build(db)
    await place(w, admin(w), w.child, [w.abiotic])
    await w.db.run(lambda c: holds.mark_manual(c, admin_id=w.admin, person_id=w.child,
                                               service_id=w.abiotic, now=NOW))
    marked = await w.db.run(lambda c: c.execute("SELECT COUNT(*) FROM manual_suspension").fetchone()[0])
    assert marked == 1
    await lift(w, admin(w), w.child, [w.abiotic])
    marked = await w.db.run(lambda c: c.execute("SELECT COUNT(*) FROM manual_suspension").fetchone()[0])
    assert marked == 0


async def test_unmark_removes_the_mark_and_keeps_the_hold(db: Database) -> None:
    w = await build(db)
    await place(w, admin(w), w.child, [w.abiotic])
    await w.db.run(lambda c: holds.mark_manual(c, admin_id=w.admin, person_id=w.child,
                                               service_id=w.abiotic, now=NOW))
    await w.db.run(lambda c: holds.unmark_manual(c, admin_id=w.admin, person_id=w.child,
                                                 service_id=w.abiotic, now=NOW))
    assert await hold_kinds(w, w.child, w.abiotic) == ["admin"]
    assert await w.db.run(lambda c: c.execute("SELECT COUNT(*) FROM manual_suspension").fetchone()[0]) == 0


# AC8.2 hold labels --------------------------------------------------------------------------------------

async def test_hold_labels_name_the_parent_the_administrator_or_the_system(db: Database) -> None:
    w = await build(db)
    await lock_child(w)
    await place(w, admin(w), w.child, [w.fluxer], at=NOW + timedelta(hours=1))
    labels = await w.db.run(lambda c: holds.hold_labels(c, w.child, w.fluxer))
    assert sorted(labels) == sorted(["Ann", text.ACTOR_ADMIN, text.ACTOR_SYSTEM])


async def test_services_with_holds_groups_each_service_holds(db: Database) -> None:
    w = await build(db)
    await place(w, parent(w.ann), w.child, [w.fluxer])
    await place(w, admin(w), w.child, [w.fluxer, w.abiotic])
    grouped = await w.db.run(lambda c: holds.services_with_holds(c, w.child))
    assert sorted(grouped) == sorted([w.fluxer, w.abiotic])
    assert [h.kind for h in grouped[w.fluxer]] == ["parent", "admin"]
    assert [h.kind for h in grouped[w.abiotic]] == ["admin"]


# Lifting a lockdown after a resolution that kept it --------------------------------------------------

async def test_lifting_a_lockdown_left_by_a_resolution_removes_it_logs_it_and_emails_the_parents(db: Database) -> None:
    w = await build(db)
    review_id = await lock_child(w)
    await w.db.run(lambda c: holds.resolve_review(
        c, admin_id=w.admin, review_id=review_id, outcome="Talked it through", lift_lockdown=False, now=NOW))
    assert {h.service_id for h in await lockdown_holds(w, w.child)} == {w.fluxer, w.abiotic}
    before = await outbox(w)

    await w.db.run(lambda c: holds.lift_lockdown(c, admin_id=w.admin, review_id=review_id,
                                                 now=NOW + timedelta(hours=1)))

    assert await lockdown_holds(w, w.child) == []
    logged = await decision_rows(w, "lockdown_lifted")
    assert len(logged) == 1
    _, actor_kind, actor_id, target, services_json, _ = logged[0]
    assert (actor_kind, actor_id, target) == ("admin", w.admin, w.child)
    assert json.loads(services_json) == sorted([w.fluxer, w.abiotic])
    new_mail = (await outbox(w))[len(before):]
    change_subject = text.CHANGE_EMAIL_SUBJECT.format(child="Kid")
    assert sorted(to for to, subject, _ in new_mail if subject == change_subject) == sorted([ANN_EMAIL, BEN_EMAIL])


async def test_lifting_a_lockdown_is_refused_while_the_review_is_open(db: Database) -> None:
    w = await build(db)
    review_id = await lock_child(w)
    with pytest.raises(ValueError):
        await w.db.run(lambda c: holds.lift_lockdown(c, admin_id=w.admin, review_id=review_id, now=NOW))
    assert {h.service_id for h in await lockdown_holds(w, w.child)} == {w.fluxer, w.abiotic}
    assert await decision_rows(w, "lockdown_lifted") == []


async def test_lifting_a_lockdown_for_an_unknown_review_is_refused(db: Database) -> None:
    w = await build(db)
    with pytest.raises(holds.NoSuchReview):
        await w.db.run(lambda c: holds.lift_lockdown(c, admin_id=w.admin, review_id=999, now=NOW))


async def test_lifting_a_lockdown_twice_changes_nothing_the_second_time(db: Database) -> None:
    w = await build(db)
    review_id = await lock_child(w)
    await w.db.run(lambda c: holds.resolve_review(
        c, admin_id=w.admin, review_id=review_id, outcome="Done", lift_lockdown=False, now=NOW))
    await w.db.run(lambda c: holds.lift_lockdown(c, admin_id=w.admin, review_id=review_id, now=NOW))
    await w.db.run(lambda c: holds.lift_lockdown(c, admin_id=w.admin, review_id=review_id, now=NOW))
    assert len(await decision_rows(w, "lockdown_lifted")) == 1


# A further reversal while a review is open -----------------------------------------------------------

async def test_a_further_reversal_while_a_review_is_open_adds_no_second_review(db: Database) -> None:
    w = await build(db)
    await lock_child(w)
    third = await lift(w, parent(w.ben), w.child, [w.fluxer], at=NOW + timedelta(hours=1))

    assert third.reversal is True
    assert third.lockdown_review_id is None
    assert len(await review_rows(w)) == 1
    assert len(await lockdown_holds(w, w.child)) == 2          # one per service, from the first review only
    logged = await decision_rows(w, "hold_lifted")
    assert logged[-1][5] == 1                                   # the action is still logged as a reversal


# Parents placing holds: the child link first, then the services ---------------------------------------

async def add_service(w: World, slug: str, *, entitled: bool, retired: bool = False) -> int:
    def change(conn) -> int:
        service = services.create_service(conn, slug=slug, name=slug.title(), kind="forward_auth", adapter="manual",
                                          auto_suspendable=True, url=None, actor_id=w.admin, now=NOW)
        if entitled:
            services.add_entitlement(conn, service.id, "type", "child", "grant", actor_id=w.admin, now=NOW)
        if retired:
            services.retire_service(conn, service.id, actor_id=w.admin, now=NOW)
        return service.id

    return await w.db.run(change)


async def test_an_unrelated_parent_is_refused_as_not_your_child_even_for_a_missing_service(db: Database) -> None:
    w = await build(db)
    with pytest.raises(holds.HoldRefused) as refused:
        await place(w, parent(w.cy), w.child, [999999])
    assert refused.value.reason == "not_your_child"


async def test_a_linked_parent_is_refused_for_a_service_that_does_not_exist(db: Database) -> None:
    w = await build(db)
    with pytest.raises(holds.HoldRefused) as refused:
        await place(w, parent(w.ann), w.child, [999999])
    assert refused.value.reason == "not_child_service"


async def test_a_linked_parent_cannot_place_on_a_service_the_child_is_not_entitled_to(db: Database) -> None:
    w = await build(db)
    walled = await add_service(w, "walled", entitled=False)
    with pytest.raises(holds.HoldRefused) as refused:
        await place(w, parent(w.ann), w.child, [w.fluxer, walled])
    assert refused.value.reason == "not_child_service"
    assert await hold_kinds(w, w.child, w.fluxer) == []        # refused before anything is written
    assert await hold_kinds(w, w.child, walled) == []


async def test_a_linked_parent_cannot_place_on_a_retired_service(db: Database) -> None:
    w = await build(db)
    retired = await add_service(w, "retired", entitled=True, retired=True)
    with pytest.raises(holds.HoldRefused) as refused:
        await place(w, parent(w.ben), w.child, [retired])
    assert refused.value.reason == "not_child_service"
    assert await hold_kinds(w, w.child, retired) == []


# Fresh start after a resolved review ------------------------------------------------------------------

async def test_after_a_resolution_only_reversals_logged_since_then_count(db: Database) -> None:
    w = await build(db)
    review_id = await lock_child(w)
    await w.db.run(lambda c: holds.resolve_review(
        c, admin_id=w.admin, review_id=review_id, outcome="Talked it through", lift_lockdown=True,
        now=NOW + timedelta(minutes=30)))

    # Ben lifts first. Nothing before the resolution counts, so there is nothing to reverse against.
    first = await lift(w, parent(w.ben), w.child, [w.fluxer], at=NOW + timedelta(hours=1))
    assert first.reversal is False and first.lockdown_review_id is None
    # Ann places again: a reversal of Ben's lift, but the only one since the resolution, so no lockdown.
    second = await place(w, parent(w.ann), w.child, [w.fluxer], at=NOW + timedelta(hours=2))
    assert second.reversal is True and second.lockdown_review_id is None
    assert len(await review_rows(w)) == 1
    # Ben lifts within the window: a second reversal since the resolution, so the child is locked down again.
    third = await lift(w, parent(w.ben), w.child, [w.fluxer], at=NOW + timedelta(hours=3))
    assert third.reversal is True
    assert third.lockdown_review_id is not None
    assert len(await review_rows(w)) == 2
