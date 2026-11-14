# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The own-suspension store: hallmonitor records only the suspensions it placed itself."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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


async def test_own_record_reads_the_reference_and_the_time_it_was_placed(db: Database) -> None:
    later = datetime(2026, 11, 13, 12, 30, tzinfo=UTC)
    await db.run(lambda c: own.record_own(c, "valheim", "1001", "ban", NOW))
    assert await db.run(lambda c: own.own_record(c, "valheim", "1001")) == ("ban", NOW)
    await db.run(lambda c: own.record_own(c, "valheim", "1001", "restore", later))
    assert await db.run(lambda c: own.own_record(c, "valheim", "1001")) == ("restore", later)


async def test_own_record_of_an_unknown_pair_is_none(db: Database) -> None:
    assert await db.run(lambda c: own.own_record(c, "valheim", "1001")) is None


# The grace window: the setting plus one second, since placed_at is stored to the second ----------------------

@pytest.mark.parametrize(("elapsed", "inside"), [(0, True), (599, True), (600, True), (601, False), (602, False)])
def test_the_grace_window_is_the_setting_plus_the_one_second_margin(elapsed: int, inside: bool) -> None:
    assert own.within_grace(NOW, NOW + timedelta(seconds=elapsed), 600) is inside


def test_a_placed_at_that_is_later_than_now_is_inside_the_window() -> None:
    assert own.within_grace(NOW, NOW - timedelta(seconds=5), 600)


@pytest.mark.parametrize("raw", ["1", "120", "600", "999999999"])
def test_a_positive_whole_number_is_a_grace_setting(raw: str) -> None:
    assert own.parse_grace_seconds(raw) == int(raw)


@pytest.mark.parametrize("raw", ["", "0", "00", "-1", "+5", "1.5", "1e3", " 5", "5 ", "1234567890", "\u0661"])
def test_anything_else_is_not_a_grace_setting(raw: str) -> None:
    assert own.parse_grace_seconds(raw) is None


def test_the_default_grace_is_ten_minutes() -> None:
    assert own.DEFAULT_GRACE_SECONDS == 600
