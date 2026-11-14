# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versioned terms of service and privacy text, each person's acceptance of them, and the gate rules.

Plain functions over a sqlite3 connection. Callers run them inside `Database.run`. `publish` writes its own
decision-log row; the web layer logs everything else.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from hallpass.registry import _PERSON_SELECT_P, Person, _iso, _person, log_decision

_VERSION_COLUMNS = "id, version, tos_text, privacy_text, blocking, published_at"


@dataclass(frozen=True)
class TermsVersion:
    id: int
    version: int
    tos_text: str
    privacy_text: str
    blocking: bool
    published_at: str


@dataclass(frozen=True)
class TermsState:
    current: TermsVersion | None
    accepted_current: bool
    accepted_any: bool


def _version(row: tuple) -> TermsVersion:
    return TermsVersion(id=row[0], version=row[1], tos_text=row[2], privacy_text=row[3],
                        blocking=bool(row[4]), published_at=row[5])


def publish(conn: sqlite3.Connection, *, tos_text: str, privacy_text: str, blocking: bool, actor_id: int,
            now: datetime) -> TermsVersion:
    """Publishes the next version (highest + 1). Version 1 is always blocking, whatever `blocking` says."""
    if not (tos_text or "").strip() or not (privacy_text or "").strip():
        raise ValueError("terms and privacy text are required")
    number = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM terms_version").fetchone()[0]
    is_blocking = True if number == 1 else bool(blocking)
    cursor = conn.execute(
        "INSERT INTO terms_version (version, tos_text, privacy_text, blocking, published_at, published_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (number, tos_text, privacy_text, int(is_blocking), _iso(now), actor_id),
    )
    log_decision(conn, at=now, actor_kind="admin", actor_person_id=actor_id, target_person_id=None,
                 action="terms_published", detail={"version": number})
    version = get(conn, cursor.lastrowid)
    assert version is not None
    return version


def current(conn: sqlite3.Connection) -> TermsVersion | None:
    row = conn.execute(f"SELECT {_VERSION_COLUMNS} FROM terms_version ORDER BY version DESC LIMIT 1").fetchone()
    return None if row is None else _version(row)


def list_versions(conn: sqlite3.Connection) -> list[TermsVersion]:
    """Every published version, newest first."""
    rows = conn.execute(f"SELECT {_VERSION_COLUMNS} FROM terms_version ORDER BY version DESC").fetchall()
    return [_version(row) for row in rows]


def get(conn: sqlite3.Connection, version_id: int) -> TermsVersion | None:
    row = conn.execute(f"SELECT {_VERSION_COLUMNS} FROM terms_version WHERE id = ?", (version_id,)).fetchone()
    return None if row is None else _version(row)


def accept(conn: sqlite3.Connection, person_id: int, version_id: int, now: datetime) -> bool:
    """Records the acceptance if it is absent. Returns True only for a new acceptance."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO terms_acceptance (person_id, terms_version_id, accepted_at) VALUES (?, ?, ?)",
        (person_id, version_id, _iso(now)),
    )
    return cursor.rowcount == 1


def _accepted(conn: sqlite3.Connection, person_id: int, version_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM terms_acceptance WHERE person_id = ? AND terms_version_id = ?", (person_id, version_id),
    ).fetchone() is not None


def state_for(conn: sqlite3.Connection, person_id: int) -> TermsState:
    latest = current(conn)
    accepted_current = latest is not None and _accepted(conn, person_id, latest.id)
    accepted_any = conn.execute(
        "SELECT 1 FROM terms_acceptance WHERE person_id = ? LIMIT 1", (person_id,)).fetchone() is not None
    return TermsState(current=latest, accepted_current=accepted_current, accepted_any=accepted_any)


def services_blocked(state: TermsState) -> bool:
    """True when no version exists, the person accepted none, or the current version is blocking and unaccepted."""
    if state.current is None or not state.accepted_any:
        return True
    return state.current.blocking and not state.accepted_current


def pages_blocked(state: TermsState) -> bool:
    """Pages follow the same rule as services today. Both names exist so each caller states what it gates."""
    return services_blocked(state)


def acceptance_report(conn: sqlite3.Connection, version_id: int) -> tuple[list[tuple[Person, str]], list[Person]]:
    """(accepted with their times, not accepted). Not accepted counts only unlocked people bound to an IdP account."""
    accepted_rows = conn.execute(
        f"SELECT {_PERSON_SELECT_P}, a.accepted_at FROM terms_acceptance a JOIN person p ON p.id = a.person_id "
        "WHERE a.terms_version_id = ? ORDER BY a.accepted_at, p.id",
        (version_id,),
    ).fetchall()
    accepted = [(_person(row[:-1]), row[-1]) for row in accepted_rows]
    missing_rows = conn.execute(
        f"SELECT {_PERSON_SELECT_P} FROM person p "
        "WHERE p.status <> 'locked' AND p.authentik_uuid IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM terms_acceptance a WHERE a.person_id = p.id AND a.terms_version_id = ?) "
        "ORDER BY p.id",
        (version_id,),
    ).fetchall()
    return accepted, [_person(row) for row in missing_rows]
