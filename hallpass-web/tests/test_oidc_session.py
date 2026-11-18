# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The server-side ID token store: a sign-in keeps its token under a handle, sign-out takes it once, and rows older
than the session max age are pruned on the next insert."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hallpass import oidc_session
from hallpass.db import Database

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
MAX_AGE = 43200


@pytest.fixture
async def database(tmp_path: Path):
    opened = Database.open(tmp_path / "hallpass.db")
    try:
        yield opened
    finally:
        opened.close()


async def store(database: Database, handle: str, token: str, at: datetime) -> None:
    await database.run(lambda c: oidc_session.store(c, handle=handle, id_token=token, now=at, max_age_seconds=MAX_AGE))


async def take(database: Database, handle: str) -> str | None:
    return await database.run(lambda c: oidc_session.take(c, handle))


async def test_a_stored_token_is_taken_once_by_its_handle(database: Database):
    await store(database, "handle-1", "token-1", NOW)

    assert await take(database, "handle-1") == "token-1"
    assert await take(database, "handle-1") is None


async def test_an_unknown_handle_takes_nothing_and_leaves_the_other_rows(database: Database):
    await store(database, "handle-1", "token-1", NOW)

    assert await take(database, "handle-2") is None
    assert await take(database, "handle-1") == "token-1"


async def test_rows_older_than_the_session_max_age_are_pruned_on_the_next_insert(database: Database):
    await store(database, "old", "old-token", NOW - timedelta(seconds=MAX_AGE + 1))
    await store(database, "new", "new-token", NOW)

    assert await take(database, "old") is None
    assert await take(database, "new") == "new-token"


async def test_a_row_exactly_at_the_max_age_is_kept(database: Database):
    await store(database, "edge", "edge-token", NOW - timedelta(seconds=MAX_AGE))
    await store(database, "new", "new-token", NOW)

    assert await take(database, "edge") == "edge-token"
