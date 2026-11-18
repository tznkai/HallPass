# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Revoked Authentik sessions and subjects: what back-channel logout records, and what every request checks.

Times are stored as fixed-width UTC stamps, so the rows compare as strings. Nothing here reads or writes a session
cookie; the callers pass in the sid, the subject and the start time that the session holds.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta


def stamp(moment: datetime) -> str:
    """A fixed-width UTC timestamp. The moment must carry a timezone."""
    if moment.tzinfo is None:
        raise ValueError("a timestamp needs a timezone")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def prune(conn: sqlite3.Connection, *, now: datetime, max_age_seconds: int) -> None:
    """Drops revocations older than the session max age. A session signed before that has expired by its cookie."""
    cutoff = stamp(now - timedelta(seconds=max_age_seconds))
    conn.execute("DELETE FROM revoked_session WHERE revoked_at < ?", (cutoff,))
    conn.execute("DELETE FROM revoked_subject WHERE revoked_at < ?", (cutoff,))


def revoke_session(conn: sqlite3.Connection, *, sid: str, now: datetime, max_age_seconds: int) -> None:
    prune(conn, now=now, max_age_seconds=max_age_seconds)
    conn.execute("INSERT INTO revoked_session (sid, revoked_at) VALUES (?, ?) ON CONFLICT(sid) DO NOTHING",
                 (sid, stamp(now)))


def revoke_subject(conn: sqlite3.Connection, *, sub: str, issued_at: datetime, now: datetime,
                   max_age_seconds: int) -> None:
    """Records when the person's Authentik sessions ended, which is the logout token's issue time, never later than
    now. A late or replayed delivery cannot move the time forward, because the stored time only ever goes up."""
    prune(conn, now=now, max_age_seconds=max_age_seconds)
    ended_at = min(issued_at, now)
    conn.execute("INSERT INTO revoked_subject (sub, revoked_at) VALUES (?, ?) "
                 "ON CONFLICT(sub) DO UPDATE SET revoked_at = MAX(revoked_at, excluded.revoked_at)",
                 (sub, stamp(ended_at)))


def session_ended(conn: sqlite3.Connection, *, sid: str | None, sub: str | None, started_at: str | None) -> bool:
    """Whether a request must end its session. A session with no start time was made before the start time was kept,
    so it ends for every request: everyone signs in once after the upgrade. Any other session is ended as
    session_revoked says."""
    return started_at is None or session_revoked(conn, sid=sid, sub=sub, started_at=started_at)


def session_revoked(conn: sqlite3.Connection, *, sid: str | None, sub: str | None, started_at: str | None) -> bool:
    """Whether a session is ended: its sid was revoked, or its person was revoked after the session started. A session
    with no start time (one made before the start time was kept) is ended by any subject revocation for its person."""
    if sid is not None and conn.execute("SELECT 1 FROM revoked_session WHERE sid = ?", (sid,)).fetchone() is not None:
        return True
    if sub is None:
        return False
    row = conn.execute("SELECT revoked_at FROM revoked_subject WHERE sub = ?", (sub,)).fetchone()
    if row is None:
        return False
    return started_at is None or row[0] > started_at
