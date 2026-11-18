# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Person-status transitions out of `invited`, and parent grants.

This module and `registry.set_locked` are the only places that set a person's status. Plain functions over a
sqlite3 connection, run inside `Database.run`, so each call is one transaction. Nothing here calls log_event;
each change writes its decision_log row.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from hallpass import registry, terms
from hallpass.registry import Person, _iso

# The status an adult or a parent moves to on their first acceptance of any terms version, while still invited.
# A child never accepts: a linked parent's acceptance covers them, and their status moves at sign-in instead.
_FIRST_ACCEPTANCE_STATUS = {"adult": "active", "parent": "active"}


class GrantRefused(Exception):
    """A grant that a rule refuses. `reason` is one of the fixed codes below; the web layer maps each to text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AcceptanceLocked(Exception):
    """A locked person tried to accept terms. Nothing is recorded; the web layer answers with the locked text."""


class ChildCannotAccept(Exception):
    """A child tried to accept terms. Nothing is recorded; a linked parent accepts for the child."""


def _set_status(conn: sqlite3.Connection, person_id: int, status: str) -> None:
    conn.execute("UPDATE person SET status = ? WHERE id = ?", (status, person_id))


def accept_terms(conn: sqlite3.Connection, person_id: int, version_id: int, now: datetime) -> Person:
    """Records the acceptance. On the person's first-ever acceptance while invited, moves them out of invited.
    A locked person is refused before anything is written, so a lock can never leave an acceptance behind. A child
    is refused the same way: their terms are covered by a parent, not by their own acceptance."""
    person = registry.get_person(conn, person_id)
    if person is None:
        raise registry.NoSuchPerson()
    if person.status == "locked":
        raise AcceptanceLocked()
    if person.account_type == "child":
        raise ChildCannotAccept()
    version = terms.get(conn, version_id)
    if version is None:
        raise ValueError("unknown terms version")
    first_ever = not terms.state_for(conn, person_id).accepted_any
    if terms.accept(conn, person_id, version_id, now):
        registry.log_decision(conn, at=now, actor_kind="person", actor_person_id=person_id,
                              target_person_id=person_id, action="terms_accepted",
                              detail={"version": version.version})
        if first_ever and person.status == "invited":
            _set_status(conn, person_id, _FIRST_ACCEPTANCE_STATUS[person.account_type])
    result = registry.get_person(conn, person_id)
    assert result is not None
    return result


def _is_linked(conn: sqlite3.Connection, child_id: int, parent_id: int) -> bool:
    return conn.execute("SELECT 1 FROM parent_link WHERE child_id = ? AND parent_id = ?",
                        (child_id, parent_id)).fetchone() is not None


def _has_grant(conn: sqlite3.Connection, child_id: int) -> bool:
    return conn.execute("SELECT 1 FROM parent_grant WHERE child_id = ? LIMIT 1", (child_id,)).fetchone() is not None


def grant(conn: sqlite3.Connection, *, child_id: int, parent_id: int, now: datetime) -> Person:
    """A linked parent approves a child. Checks run in order; the first failure raises GrantRefused.
    The link is checked first, so a missing child and a child the caller is not linked to are refused alike.

    An awaiting child becomes active at once. An invited child (not yet enrolled) keeps its status: the grant is
    recorded, and the child becomes active at their first sign-in. A second grant on an invited child is refused."""
    if not _is_linked(conn, child_id, parent_id):
        raise GrantRefused("not_linked_parent")
    child = registry.get_person(conn, child_id)
    parent = registry.get_person(conn, parent_id)
    if child is None or parent is None:
        raise registry.NoSuchPerson()
    state = terms.state_for(conn, parent_id)
    if state.current is None or not state.accepted_current:
        raise GrantRefused("parent_terms")
    if parent.status != "active":
        raise GrantRefused("parent_inactive")
    if child.status == "awaiting_grant":
        _set_status(conn, child_id, "active")
    elif child.status == "invited":
        if _has_grant(conn, child_id):
            raise GrantRefused("already_granted")
    else:
        raise GrantRefused("child_not_awaiting")
    conn.execute(
        "INSERT INTO parent_grant (child_id, parent_id, terms_version_id, granted_at) VALUES (?, ?, ?, ?)",
        (child_id, parent_id, state.current.id, _iso(now)),
    )
    registry.log_decision(conn, at=now, actor_kind="parent", actor_person_id=parent_id,
                          target_person_id=child_id, action="granted", detail={"version": state.current.version})
    result = registry.get_person(conn, child_id)
    assert result is not None
    return result


def settle_child_at_sign_in(conn: sqlite3.Connection, person_id: int) -> Person:
    """Run on each successful sign-in. A child still invited (now enrolled) moves to active when a parent has granted,
    otherwise to awaiting_grant. Any other status is left alone, so a lock is never overwritten. The status is read
    inside this transaction, so a lock written just before it is seen."""
    person = registry.get_person(conn, person_id)
    if person is None:
        raise registry.NoSuchPerson()
    if person.account_type != "child" or person.status != "invited":
        return person
    _set_status(conn, person_id, "active" if _has_grant(conn, person_id) else "awaiting_grant")
    result = registry.get_person(conn, person_id)
    assert result is not None
    return result


def grants_for(conn: sqlite3.Connection, child_id: int) -> list[dict]:
    """Each grant made for the child, oldest first: parent id, the parent's display name, the time and the version."""
    rows = conn.execute(
        "SELECT g.parent_id, p.display_name, g.granted_at, t.version FROM parent_grant g "
        "JOIN person p ON p.id = g.parent_id JOIN terms_version t ON t.id = g.terms_version_id "
        "WHERE g.child_id = ? ORDER BY g.id",
        (child_id,),
    ).fetchall()
    return [{"parent_id": row[0], "display_name": row[1], "granted_at": row[2], "version": row[3]} for row in rows]
