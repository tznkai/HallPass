# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The raw ID tokens that sign-out needs, kept on the server under a handle.

A sign-in stores its ID token here and puts only the handle in the session cookie. Sign-out takes the token by its
handle and deletes the row. Nothing in the cookie can be read as the person's name, username or groups.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from hallpass import revocation


def prune(conn: sqlite3.Connection, *, now: datetime, max_age_seconds: int) -> None:
    """Drops tokens older than the session max age. A session that held one has expired by its cookie by then."""
    cutoff = revocation.stamp(now - timedelta(seconds=max_age_seconds))
    conn.execute("DELETE FROM oidc_session WHERE created_at < ?", (cutoff,))


def store(conn: sqlite3.Connection, *, handle: str, id_token: str, now: datetime, max_age_seconds: int) -> None:
    prune(conn, now=now, max_age_seconds=max_age_seconds)
    conn.execute("INSERT INTO oidc_session (handle, id_token, created_at) VALUES (?, ?, ?)",
                 (handle, id_token, revocation.stamp(now)))


def take(conn: sqlite3.Connection, handle: str) -> str | None:
    """The token stored under the handle, and its row deleted, so a token is taken at most once. None when there is
    no such row."""
    row = conn.execute("SELECT id_token FROM oidc_session WHERE handle = ?", (handle,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM oidc_session WHERE handle = ?", (handle,))
    return row[0]
