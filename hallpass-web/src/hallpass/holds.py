# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Holds: a parent or the administrator revokes and restores a child's access on services. When two parents' opposing
actions reverse each other inside the window, the child is locked down for review.

Plain functions over a sqlite3 connection. Callers run them inside `Database.run`, so each decision, its decision_log
row, its outbox rows and any lockdown are one transaction: a mail failure later can never undo a decision. Nothing
here logs; addresses and subjects never reach the log.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from hallpass import access, conflict, mail, registry, services, text
from hallpass.access import Hold
from hallpass.registry import Person, _iso

MODES = ("cooperative", "default", "monitored", "strict")


class HoldRefused(Exception):
    """A refused hold change. `reason` is a fixed code; `message` is the user-facing text and names no addresses."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class NoSuchReview(LookupError):
    """A review id that does not exist."""


@dataclass(frozen=True)
class Actor:
    kind: Literal["parent", "admin"]
    person_id: int


@dataclass(frozen=True)
class Outcome:
    changed_services: tuple[int, ...]
    reversal: bool
    lockdown_review_id: int | None


_NO_CHANGE = Outcome((), False, None)


# Lookups -----------------------------------------------------------------------------------------------

def _person(conn: sqlite3.Connection, person_id: int) -> Person:
    person = registry.get_person(conn, person_id)
    if person is None:
        raise registry.NoSuchPerson()
    return person


def _require_own_child(conn: sqlite3.Connection, actor: Actor, child: Person) -> None:
    if actor.person_id not in {parent.id for parent in registry.parents_of(conn, child.id)}:
        raise HoldRefused("not_your_child", text.NOT_YOUR_CHILD)


def _resolve_services(conn: sqlite3.Connection, target: Person, service_ids: Sequence[int] | None) -> list[int]:
    """None means every service the person has, from access.services_for; otherwise each id must exist."""
    if service_ids is None:
        return [service.id for service, _ in access.services_for(conn, target)]
    ids = sorted(set(service_ids))
    for service_id in ids:
        if services.get(conn, service_id) is None:
            raise services.NoSuchService()
    return ids


def _hold_label(conn: sqlite3.Connection, hold: Hold) -> str:
    if hold.kind == "admin":
        return text.ACTOR_ADMIN
    if hold.kind == "parent" and hold.placed_by is not None:
        placer = registry.get_person(conn, hold.placed_by)
        if placer is not None:
            return placer.display_name
    return text.ACTOR_SYSTEM


def hold_labels(conn: sqlite3.Connection, person_id: int, service_id: int) -> list[str]:
    """Who placed each hold on the service: a parent's display name, the administrator, or the system."""
    return [_hold_label(conn, hold) for hold in access.holds_for(conn, person_id, service_id)]


def services_with_holds(conn: sqlite3.Connection, person_id: int) -> dict[int, list[Hold]]:
    rows = conn.execute(
        "SELECT id, person_id, service_id, kind, placed_by, placed_at, reason, review_id FROM hold "
        "WHERE person_id = ? ORDER BY id",
        (person_id,),
    ).fetchall()
    grouped: dict[int, list[Hold]] = {}
    for row in rows:
        hold = Hold(*row)
        grouped.setdefault(hold.service_id, []).append(hold)
    return grouped


def _history(conn: sqlite3.Connection, child_id: int) -> list[conflict.Entry]:
    """The parents' place and lift actions on this child, for conflict.evaluate. Newest id first: evaluate sorts
    stably, so actions logged at the same second keep log order and the newest one is found as the previous action."""
    rows = conn.execute(
        "SELECT at, actor_person_id, action, services, reversal FROM decision_log "
        "WHERE target_person_id = ? AND actor_kind = 'parent' AND action IN ('hold_placed', 'hold_lifted') "
        "ORDER BY id DESC",
        (child_id,),
    ).fetchall()
    return [
        conflict.Entry(at=datetime.fromisoformat(at), actor_id=actor_id,
                       action="place" if action == "hold_placed" else "lift",
                       services=frozenset(json.loads(services)), reversal=bool(reversal))
        for at, actor_id, action, services, reversal in rows
    ]


def _names(conn: sqlite3.Connection, holds: Sequence[Hold]) -> str:
    return ", ".join(_hold_label(conn, hold) for hold in holds)


def liftable_parent_holds(mode: str, parent_id: int, held: Sequence[Hold]) -> list[Hold]:
    """The parent holds this parent may lift on one service: every parent hold in the shared modes, and only the
    parent's own holds in strict mode. Admin and lockdown holds are never liftable by a parent."""
    parent_holds = [hold for hold in held if hold.kind == "parent"]
    if mode == "strict":
        return [hold for hold in parent_holds if hold.placed_by == parent_id]
    return parent_holds


def marked_service_ids(conn: sqlite3.Connection, person_id: int) -> set[int]:
    """The services the administrator has marked manually suspended for this person."""
    return {row[0] for row in conn.execute(
        "SELECT service_id FROM manual_suspension WHERE person_id = ?", (person_id,)).fetchall()}


# Notifications -----------------------------------------------------------------------------------------

def _queue_change_emails(conn: sqlite3.Connection, *, child: Person, actor_id: int, label: str,
                         now: datetime) -> None:
    """One email to each linked parent with an address, except the actor. Only children have parents to email."""
    if child.account_type != "child":
        return
    subject = text.CHANGE_EMAIL_SUBJECT.format(child=child.display_name)
    body = text.CHANGE_EMAIL_BODY.format(child=child.display_name, actor=label)
    for parent in registry.parents_of(conn, child.id):
        if parent.id != actor_id and parent.email:
            mail.enqueue(conn, to_addr=parent.email, subject=subject, body=body, now=now)


def _clear_orphan_marks(conn: sqlite3.Connection, person_id: int, service_ids: Sequence[int]) -> None:
    """A manual mark goes when the last hold on that person and service goes."""
    conn.executemany(
        "DELETE FROM manual_suspension WHERE person_id = ? AND service_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM hold WHERE person_id = ? AND service_id = ?)",
        [(person_id, service_id, person_id, service_id) for service_id in service_ids],
    )


# Lockdown ----------------------------------------------------------------------------------------------

def _lockdown(conn: sqlite3.Connection, child: Person, now: datetime, admin_email: str | None) -> int:
    """Opens a review, holds every service the child has, logs it, and emails each linked parent and the admin.
    Returns the review id."""
    monitored = child.conflict_mode == "monitored"
    note = child.agreement_note or ""
    if monitored:
        reason = text.LOCKDOWN_REVIEW_REASON_MONITORED.format(agreement_note=note)
        body = text.LOCKDOWN_EMAIL_BODY_MONITORED.format(child=child.display_name, agreement_note=note)
    else:
        reason = text.LOCKDOWN_REVIEW_REASON_DEFAULT
        body = text.LOCKDOWN_EMAIL_BODY_DEFAULT.format(child=child.display_name)
    stamp = _iso(now)
    review_id = conn.execute("INSERT INTO review_item (child_id, reason, opened_at) VALUES (?, ?, ?)",
                             (child.id, reason, stamp)).lastrowid
    locked = [service.id for service, _ in access.services_for(conn, child)]
    for service_id in locked:
        conn.execute(
            "INSERT INTO hold (person_id, service_id, kind, placed_by, placed_at, reason, review_id) "
            "VALUES (?, ?, 'lockdown', NULL, ?, '', ?)",
            (child.id, service_id, stamp, review_id),
        )
    registry.log_decision(conn, at=now, actor_kind="system", actor_person_id=None, target_person_id=child.id,
                          action="lockdown", services=locked, detail={"review_id": review_id})
    subject = text.LOCKDOWN_EMAIL_SUBJECT.format(child=child.display_name)
    for parent in registry.parents_of(conn, child.id):
        if parent.email:
            mail.enqueue(conn, to_addr=parent.email, subject=subject, body=body, now=now)
    if admin_email:
        mail.enqueue(conn, to_addr=admin_email, subject=subject, body=body, now=now)
    return review_id


# Placing -----------------------------------------------------------------------------------------------

def _place_parent_hold(conn: sqlite3.Connection, *, child_id: int, service_id: int, parent_id: int, reason: str,
                       now: datetime, strict: bool) -> None:
    stamp = _iso(now)
    if strict:
        # One hold per parent: placing again refreshes that parent's own hold.
        conn.execute(
            "INSERT INTO hold (person_id, service_id, kind, placed_by, placed_at, reason) "
            "VALUES (?, ?, 'parent', ?, ?, ?) "
            "ON CONFLICT (person_id, service_id, placed_by) WHERE kind = 'parent' "
            "DO UPDATE SET placed_at = excluded.placed_at, reason = excluded.reason",
            (child_id, service_id, parent_id, stamp, reason),
        )
        return
    # Shared modes keep one parent hold per child and service. Any other parent's hold is replaced by this one.
    conn.execute("DELETE FROM hold WHERE person_id = ? AND service_id = ? AND kind = 'parent' AND placed_by IS NOT ?",
                 (child_id, service_id, parent_id))
    updated = conn.execute(
        "UPDATE hold SET placed_by = ?, placed_at = ?, reason = ? "
        "WHERE person_id = ? AND service_id = ? AND kind = 'parent'",
        (parent_id, stamp, reason, child_id, service_id),
    ).rowcount
    if not updated:
        conn.execute(
            "INSERT INTO hold (person_id, service_id, kind, placed_by, placed_at, reason) "
            "VALUES (?, ?, 'parent', ?, ?, ?)",
            (child_id, service_id, parent_id, stamp, reason),
        )


def _place_admin_hold(conn: sqlite3.Connection, *, child_id: int, service_id: int, admin_id: int, reason: str,
                      now: datetime) -> bool:
    """Returns True when a new admin hold was written; an existing one is left as it is."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO hold (person_id, service_id, kind, placed_by, placed_at, reason) "
        "VALUES (?, ?, 'admin', ?, ?, ?)",
        (child_id, service_id, admin_id, _iso(now), reason),
    )
    return cursor.rowcount == 1


def place(conn: sqlite3.Connection, *, actor: Actor, target_id: int, service_ids: Sequence[int] | None,
          reason: str = "", now: datetime, window: timedelta, admin_email: str | None) -> Outcome:
    """Places a hold on each service (None means all of the target's services). A parent may do this only for a child
    linked to them. A parent's action is checked for a reversal, and a second reversal in the window locks the child."""
    target = _person(conn, target_id)
    ids = _resolve_services(conn, target, service_ids)
    if actor.kind == "parent":
        _require_own_child(conn, actor, target)
        strict = target.conflict_mode == "strict"
        for service_id in ids:
            _place_parent_hold(conn, child_id=target.id, service_id=service_id, parent_id=actor.person_id,
                               reason=reason, now=now, strict=strict)
        changed = ids
        verdict = conflict.evaluate(target.conflict_mode, _history(conn, target.id),
                                    conflict.Proposed(actor.person_id, "place", frozenset(changed)), now, window)
        label = _person(conn, actor.person_id).display_name
    else:
        changed = [service_id for service_id in ids
                   if _place_admin_hold(conn, child_id=target.id, service_id=service_id, admin_id=actor.person_id,
                                        reason=reason, now=now)]
        verdict = conflict.Verdict(reversal=False, lockdown=False)
        label = text.ACTOR_ADMIN
    if not changed:
        return _NO_CHANGE
    registry.log_decision(conn, at=now, actor_kind=actor.kind, actor_person_id=actor.person_id,
                          target_person_id=target.id, action="hold_placed", services=changed,
                          reversal=verdict.reversal)
    _queue_change_emails(conn, child=target, actor_id=actor.person_id, label=label, now=now)
    review_id = _lockdown(conn, target, now, admin_email) if verdict.lockdown else None
    return Outcome(tuple(changed), verdict.reversal, review_id)


# Lifting -----------------------------------------------------------------------------------------------

def _parent_lift_plan(conn: sqlite3.Connection, target: Person, parent_id: int,
                      service_ids: Sequence[int]) -> list[tuple[int, list[int]]]:
    """For each service, the parent hold ids this parent may lift. Refuses before anything is changed."""
    strict = target.conflict_mode == "strict"
    plan: list[tuple[int, list[int]]] = []
    for service_id in service_ids:
        service = services.get(conn, service_id)
        if service is None:
            raise services.NoSuchService()
        if not service.auto_suspendable:
            raise HoldRefused("restore_not_automatic", text.RESTORE_NOT_AUTOMATIC)
        held = access.holds_for(conn, target.id, service_id)
        if strict:
            parent_holds = [hold for hold in held if hold.kind == "parent"]
            own = [hold for hold in parent_holds if hold.placed_by == parent_id]
            others = [hold for hold in parent_holds if hold.placed_by != parent_id]
            if others and not own:
                raise HoldRefused("strict_not_yours", text.STRICT_NOT_YOURS.format(names=_names(conn, others)))
        liftable = liftable_parent_holds(target.conflict_mode, parent_id, held)
        if not liftable:
            if any(hold.kind in ("admin", "lockdown") for hold in held):
                raise HoldRefused("admin_or_lockdown", text.LOCKDOWN_ADMIN_ONLY)
            continue
        plan.append((service_id, [hold.id for hold in liftable]))
    return plan


def lift(conn: sqlite3.Connection, *, actor: Actor, target_id: int, service_ids: Sequence[int], now: datetime,
         window: timedelta, admin_email: str | None, include_parent: bool = False) -> Outcome:
    """Lifts holds on the listed services. A parent lifts only parent holds, only on their linked child, and only on
    services the administrator has marked auto-suspendable. The administrator lifts admin holds, and parent holds when
    include_parent is set. Lockdown holds are never lifted here; they go when their review is resolved."""
    target = _person(conn, target_id)
    if actor.kind == "parent":
        _require_own_child(conn, actor, target)
        plan = _parent_lift_plan(conn, target, actor.person_id, service_ids)
        for _, hold_ids in plan:
            for hold_id in hold_ids:
                conn.execute("DELETE FROM hold WHERE id = ?", (hold_id,))
        changed = [service_id for service_id, _ in plan]
        verdict = conflict.evaluate(target.conflict_mode, _history(conn, target.id),
                                    conflict.Proposed(actor.person_id, "lift", frozenset(changed)), now, window)
        label = _person(conn, actor.person_id).display_name
    else:
        sql = ("DELETE FROM hold WHERE person_id = ? AND service_id = ? AND kind IN ('admin', 'parent')"
               if include_parent else
               "DELETE FROM hold WHERE person_id = ? AND service_id = ? AND kind = 'admin'")
        changed = [service_id for service_id in dict.fromkeys(service_ids)
                   if conn.execute(sql, (target.id, service_id)).rowcount > 0]
        verdict = conflict.Verdict(reversal=False, lockdown=False)
        label = text.ACTOR_ADMIN
    _clear_orphan_marks(conn, target.id, service_ids)
    if not changed:
        return _NO_CHANGE
    registry.log_decision(conn, at=now, actor_kind=actor.kind, actor_person_id=actor.person_id,
                          target_person_id=target.id, action="hold_lifted", services=changed,
                          reversal=verdict.reversal)
    _queue_change_emails(conn, child=target, actor_id=actor.person_id, label=label, now=now)
    review_id = _lockdown(conn, target, now, admin_email) if verdict.lockdown else None
    return Outcome(tuple(changed), verdict.reversal, review_id)


# Review ------------------------------------------------------------------------------------------------

def resolve_review(conn: sqlite3.Connection, *, admin_id: int, review_id: int, outcome: str, lift_lockdown: bool,
                   now: datetime) -> None:
    """Closes a review with the administrator's outcome. With lift_lockdown, the lockdown holds it opened go too.
    Only the web layer's admin gate calls this; admin_id records who resolved it."""
    row = conn.execute("SELECT child_id, resolved_at FROM review_item WHERE id = ?", (review_id,)).fetchone()
    if row is None:
        raise NoSuchReview()
    child_id, resolved_at = row
    if resolved_at is not None:
        raise ValueError("review already resolved")
    clean = (outcome or "").strip()
    if not clean:
        raise ValueError("outcome is required")
    conn.execute("UPDATE review_item SET resolved_at = ?, resolved_by = ?, outcome = ? WHERE id = ?",
                 (_iso(now), admin_id, clean, review_id))
    lifted: list[int] = []
    if lift_lockdown:
        lifted = [service_id for (service_id,) in conn.execute(
            "SELECT DISTINCT service_id FROM hold WHERE review_id = ? AND kind = 'lockdown' ORDER BY service_id",
            (review_id,)).fetchall()]
        conn.execute("DELETE FROM hold WHERE review_id = ? AND kind = 'lockdown'", (review_id,))
        _clear_orphan_marks(conn, child_id, lifted)
    registry.log_decision(conn, at=now, actor_kind="admin", actor_person_id=admin_id, target_person_id=child_id,
                          action="review_resolved", services=lifted, detail={"outcome": clean})
    if lifted:
        _queue_change_emails(conn, child=_person(conn, child_id), actor_id=admin_id, label=text.ACTOR_ADMIN, now=now)


def open_review_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM review_item WHERE resolved_at IS NULL").fetchone()[0]


def review_child_id(conn: sqlite3.Connection, review_id: int) -> int | None:
    row = conn.execute("SELECT child_id FROM review_item WHERE id = ?", (review_id,)).fetchone()
    return None if row is None else row[0]


def _review_summary(conn: sqlite3.Connection, row: tuple) -> dict:
    review_id, child_id, opened_at, resolved_at, outcome = row
    return {"id": review_id, "child_name": _person(conn, child_id).display_name, "opened_at": opened_at,
            "resolved_at": resolved_at, "outcome": outcome}


def list_reviews(conn: sqlite3.Connection, *, resolved_limit: int = 20) -> tuple[list[dict], list[dict]]:
    """The open review items, oldest first, and the most recently resolved ones."""
    columns = "SELECT id, child_id, opened_at, resolved_at, outcome FROM review_item"
    open_rows = conn.execute(f"{columns} WHERE resolved_at IS NULL ORDER BY id").fetchall()
    resolved_rows = conn.execute(f"{columns} WHERE resolved_at IS NOT NULL ORDER BY resolved_at DESC, id DESC LIMIT ?",
                                 (resolved_limit,)).fetchall()
    return ([_review_summary(conn, row) for row in open_rows],
            [_review_summary(conn, row) for row in resolved_rows])


def _actor_label_for_admin(conn: sqlite3.Connection, actor_kind: str, actor_person_id: int | None) -> str:
    if actor_kind == "admin":
        return text.ACTOR_ADMIN
    if actor_kind == "system" or actor_person_id is None:
        return text.ACTOR_SYSTEM
    person = registry.get_person(conn, actor_person_id)
    return person.display_name if person is not None else text.ACTOR_SYSTEM


def recent_decisions(conn: sqlite3.Connection, person_id: int, *, limit: int = 20) -> list[dict]:
    """The newest decision-log rows about this person, for the administrator's review page."""
    rows = conn.execute(
        "SELECT at, actor_kind, actor_person_id, action, reversal FROM decision_log WHERE target_person_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (person_id, limit),
    ).fetchall()
    return [{"at": at, "actor": _actor_label_for_admin(conn, actor_kind, actor_id), "action": action,
             "reversal": bool(reversal)}
            for at, actor_kind, actor_id, action, reversal in rows]


def review_detail(conn: sqlite3.Connection, review_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, child_id, reason, opened_at, resolved_at, resolved_by, outcome FROM review_item WHERE id = ?",
        (review_id,),
    ).fetchone()
    if row is None:
        return None
    review_id, child_id, reason, opened_at, resolved_at, resolved_by, outcome = row
    resolver = registry.get_person(conn, resolved_by) if resolved_by is not None else None
    return {
        "id": review_id,
        "child": _person(conn, child_id),
        "reason": reason,
        "opened_at": opened_at,
        "resolved_at": resolved_at,
        "outcome": outcome,
        "resolver": None if resolver is None else resolver.display_name,
        "decisions": recent_decisions(conn, child_id),
    }


# Conflict modes ----------------------------------------------------------------------------------------

def _parent_ids(conn: sqlite3.Connection, child_id: int) -> frozenset[int]:
    return frozenset(parent.id for parent in registry.parents_of(conn, child_id))


def set_mode(conn: sqlite3.Connection, *, child_id: int, mode: str, agreement_note: str | None,
             apply_to_siblings: bool, admin_id: int, now: datetime) -> list[int]:
    """Sets a child's conflict mode, and the agreement note on that child. With apply_to_siblings, the same mode goes
    to every other child with exactly the same set of parents (never for a child with no parents). Returns the ids."""
    if mode not in MODES:
        raise ValueError("unknown conflict mode")
    child = _person(conn, child_id)
    if child.account_type != "child":
        raise ValueError("only a child has a conflict mode")
    targets = [child.id]
    parents = _parent_ids(conn, child.id)
    if apply_to_siblings and parents:
        for (other_id,) in conn.execute(
                "SELECT id FROM person WHERE account_type = 'child' AND id <> ? ORDER BY id", (child.id,)).fetchall():
            if _parent_ids(conn, other_id) == parents:
                targets.append(other_id)
    note = (agreement_note or "").strip() or None
    for target_id in targets:
        if target_id == child.id:
            conn.execute("UPDATE person SET conflict_mode = ?, agreement_note = ? WHERE id = ?", (mode, note, target_id))
        else:
            conn.execute("UPDATE person SET conflict_mode = ? WHERE id = ?", (mode, target_id))
        registry.log_decision(conn, at=now, actor_kind="admin", actor_person_id=admin_id,
                              target_person_id=target_id, action="mode_set", detail={"mode": mode})
    return targets


# Manual marks ------------------------------------------------------------------------------------------

def mark_manual(conn: sqlite3.Connection, *, admin_id: int, person_id: int, service_id: int, now: datetime) -> None:
    """Marks a held service that cannot be automatically suspended as suspended by hand."""
    _person(conn, person_id)
    service = services.get(conn, service_id)
    if service is None:
        raise services.NoSuchService()
    if service.auto_suspendable:
        raise HoldRefused("auto_suspendable", text.MARK_NOT_MANUAL)
    if not access.holds_for(conn, person_id, service_id):
        raise HoldRefused("no_hold", text.MARK_NO_HOLD)
    conn.execute("INSERT OR IGNORE INTO manual_suspension (person_id, service_id, marked_at, marked_by) "
                 "VALUES (?, ?, ?, ?)", (person_id, service_id, _iso(now), admin_id))
    registry.log_decision(conn, at=now, actor_kind="admin", actor_person_id=admin_id, target_person_id=person_id,
                          action="manual_marked", services=[service_id])


def unmark_manual(conn: sqlite3.Connection, *, admin_id: int, person_id: int, service_id: int, now: datetime) -> None:
    removed = conn.execute("DELETE FROM manual_suspension WHERE person_id = ? AND service_id = ?",
                           (person_id, service_id)).rowcount
    if removed:
        registry.log_decision(conn, at=now, actor_kind="admin", actor_person_id=admin_id,
                              target_person_id=person_id, action="manual_unmarked", services=[service_id])
