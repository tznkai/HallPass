# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The record of suspensions hallmonitor placed itself. Only `(slug, identity, own_ref)` is stored.

The Fluxer adapter places no suspensions and never calls these helpers. They exist for adapters that do.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from hallmonitor.contract import iso_utc


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
