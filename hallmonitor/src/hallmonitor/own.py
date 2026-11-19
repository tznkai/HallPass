# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The record of suspensions HallMonitor placed itself. Only `(slug, identity, own_ref, placed_at)` is stored.

The Fluxer adapter places no suspensions and never calls these helpers. They exist for adapters that do.

A Palworld record is forgotten only once its ban has been absent from the ban list for longer than the grace window.
The Palworld server may take a moment to write a ban to its list, so a ban placed within that window is still in flight,
not gone. The Source console list is written by `writeid` before the command returns, so Source has no such window.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from hallmonitor.contract import iso_utc

DEFAULT_GRACE_SECONDS = 600
GRACE_DIGITS = re.compile(r"[0-9]{1,9}")


def record_own(conn: sqlite3.Connection, slug: str, identity: str, own_ref: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO own_suspension (slug, identity, own_ref, placed_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(slug, identity) DO UPDATE SET own_ref = excluded.own_ref, placed_at = excluded.placed_at",
        (slug, identity, own_ref, iso_utc(now)),
    )


def own_ref(conn: sqlite3.Connection, slug: str, identity: str) -> str | None:
    row = conn.execute("SELECT own_ref FROM own_suspension WHERE slug = ? AND identity = ?",
                       (slug, identity)).fetchone()
    return None if row is None else row[0]


def forget_own(conn: sqlite3.Connection, slug: str, identity: str) -> None:
    conn.execute("DELETE FROM own_suspension WHERE slug = ? AND identity = ?", (slug, identity))


def own_record(conn: sqlite3.Connection, slug: str, identity: str) -> tuple[str, datetime] | None:
    """The reference and the time it was placed, or None. The stored time is UTC to the second."""
    row = conn.execute("SELECT own_ref, placed_at FROM own_suspension WHERE slug = ? AND identity = ?",
                       (slug, identity)).fetchone()
    if row is None:
        return None
    return row[0], datetime.strptime(row[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def within_grace(placed_at: datetime, now: datetime, grace_seconds: int) -> bool:
    """True while a ban placed at `placed_at` is inside the grace window. `placed_at` is stored to the second, so the
    measured time since placement can run up to one second longer than the true time. The window is widened by that
    second, so it never closes before `grace_seconds` has really passed."""
    return (now - placed_at).total_seconds() < grace_seconds + 1


def parse_grace_seconds(raw: str) -> int | None:
    """The `forget_grace_seconds` setting as a positive whole number of seconds, or None when it is not one."""
    if GRACE_DIGITS.fullmatch(raw) is None:
        return None
    value = int(raw)
    return value if value > 0 else None
