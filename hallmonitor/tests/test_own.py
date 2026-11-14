# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The own-suspension store: hallmonitor records only the suspensions it placed itself."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hallmonitor import own
from hallmonitor.db import Database

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hallmonitor.db")
    try:
        yield database
    finally:
        database.close()


async def test_record_then_read_back(db: Database) -> None:
    await db.run(lambda c: own.record_own(c, "minecraft", "Steve", "ref-1", NOW))
    assert await db.run(lambda c: own.own_ref(c, "minecraft", "Steve")) == "ref-1"


async def test_unknown_pair_reads_none(db: Database) -> None:
    assert await db.run(lambda c: own.own_ref(c, "minecraft", "Nobody")) is None


async def test_record_again_replaces_the_reference(db: Database) -> None:
    await db.run(lambda c: own.record_own(c, "minecraft", "Steve", "ref-1", NOW))
    await db.run(lambda c: own.record_own(c, "minecraft", "Steve", "ref-2", NOW))
    assert await db.run(lambda c: own.own_ref(c, "minecraft", "Steve")) == "ref-2"
    count = await db.run(lambda c: c.execute("SELECT COUNT(*) FROM own_suspension").fetchone()[0])
    assert count == 1


async def test_forget_removes_only_that_pair(db: Database) -> None:
    await db.run(lambda c: own.record_own(c, "minecraft", "Steve", "ref-1", NOW))
    await db.run(lambda c: own.record_own(c, "valheim", "Steve", "ref-2", NOW))
    await db.run(lambda c: own.forget_own(c, "minecraft", "Steve"))
    assert await db.run(lambda c: own.own_ref(c, "minecraft", "Steve")) is None
    assert await db.run(lambda c: own.own_ref(c, "valheim", "Steve")) == "ref-2"


async def test_own_ref_defaults_to_empty_text(db: Database) -> None:
    await db.run(lambda c: own.record_own(c, "minecraft", "Steve", "", NOW))
    assert await db.run(lambda c: own.own_ref(c, "minecraft", "Steve")) == ""
