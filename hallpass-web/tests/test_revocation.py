# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The revocation store: revoked Authentik sessions and subjects, the check the request paths make, and pruning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hallpass.db import Database
from hallpass.revocation import prune, revoke_session, revoke_subject, session_revoked, stamp

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
MAX_AGE = 43200
SUB = "00000000-0000-4000-8000-0000000000d1"


@pytest.fixture
async def database(tmp_path: Path):
    opened = Database.open(tmp_path / "hallpass.db")
    try:
        yield opened
    finally:
        opened.close()


def test_stamps_are_fixed_width_utc_and_sort_as_their_times_do():
    earlier = stamp(NOW)
    later = stamp(NOW + timedelta(microseconds=1))
    whole_second_before = stamp(NOW - timedelta(seconds=1))

    assert earlier == "2026-11-18T12:00:00.000000Z"
    assert len(earlier) == len(later) == len(whole_second_before)
    assert whole_second_before < earlier < later


def test_a_stamp_is_normalised_to_utc():
    shifted = datetime(2026, 11, 18, 14, 0, tzinfo=UTC).astimezone(UTC)
    assert stamp(shifted) == stamp(NOW + timedelta(hours=2)) == "2026-11-18T14:00:00.000000Z"


async def test_a_revoked_sid_ends_only_that_session(database: Database):
    await database.run(lambda c: revoke_session(c, sid="s-1", now=NOW, max_age_seconds=MAX_AGE))

    assert await database.run(lambda c: session_revoked(c, sid="s-1", sub=SUB, started_at=stamp(NOW))) is True
    assert await database.run(lambda c: session_revoked(c, sid="s-2", sub=SUB, started_at=stamp(NOW))) is False


async def test_a_subject_revocation_ends_sessions_started_before_it(database: Database):
    await database.run(lambda c: revoke_subject(c, sub=SUB, now=NOW, max_age_seconds=MAX_AGE))
    before = stamp(NOW - timedelta(seconds=1))

    assert await database.run(lambda c: session_revoked(c, sid=None, sub=SUB, started_at=before)) is True


async def test_a_subject_revocation_does_not_end_sessions_started_at_or_after_it(database: Database):
    await database.run(lambda c: revoke_subject(c, sub=SUB, now=NOW, max_age_seconds=MAX_AGE))

    at = stamp(NOW)
    after = stamp(NOW + timedelta(seconds=1))

    assert await database.run(lambda c: session_revoked(c, sid=None, sub=SUB, started_at=at)) is False
    assert await database.run(lambda c: session_revoked(c, sid=None, sub=SUB, started_at=after)) is False


async def test_a_session_with_no_start_time_is_ended_by_any_subject_revocation(database: Database):
    await database.run(lambda c: revoke_subject(c, sub=SUB, now=NOW, max_age_seconds=MAX_AGE))

    assert await database.run(lambda c: session_revoked(c, sid=None, sub=SUB, started_at=None)) is True


async def test_nothing_recorded_means_nothing_is_revoked(database: Database):
    assert await database.run(lambda c: session_revoked(c, sid="s-1", sub=SUB, started_at=stamp(NOW))) is False
    assert await database.run(lambda c: session_revoked(c, sid=None, sub=None, started_at=None)) is False


async def test_a_second_subject_revocation_keeps_the_later_time(database: Database):
    later = NOW + timedelta(minutes=5)
    await database.run(lambda c: revoke_subject(c, sub=SUB, now=later, max_age_seconds=MAX_AGE))
    await database.run(lambda c: revoke_subject(c, sub=SUB, now=NOW, max_age_seconds=MAX_AGE))

    stored = await database.run(lambda c: c.execute("SELECT revoked_at FROM revoked_subject").fetchall())

    assert stored == [(stamp(later),)]


async def test_rows_older_than_the_session_max_age_are_pruned_on_the_next_insert(database: Database):
    old = NOW - timedelta(seconds=MAX_AGE + 1)
    await database.run(lambda c: revoke_session(c, sid="old-sid", now=old, max_age_seconds=MAX_AGE))
    await database.run(lambda c: revoke_subject(c, sub="old-sub", now=old, max_age_seconds=MAX_AGE))

    await database.run(lambda c: revoke_session(c, sid="new-sid", now=NOW, max_age_seconds=MAX_AGE))

    sessions, subjects = await database.run(lambda c: (
        c.execute("SELECT sid FROM revoked_session ORDER BY sid").fetchall(),
        c.execute("SELECT sub FROM revoked_subject").fetchall()))
    assert sessions == [("new-sid",)]
    assert subjects == []


async def test_a_row_exactly_at_the_max_age_is_kept(database: Database):
    edge = NOW - timedelta(seconds=MAX_AGE)
    await database.run(lambda c: revoke_session(c, sid="edge-sid", now=edge, max_age_seconds=MAX_AGE))

    await database.run(lambda c: prune(c, now=NOW, max_age_seconds=MAX_AGE))

    assert await database.run(lambda c: c.execute("SELECT sid FROM revoked_session").fetchall()) == [("edge-sid",)]


async def test_a_repeated_sid_revocation_is_recorded_once(database: Database):
    await database.run(lambda c: revoke_session(c, sid="s-1", now=NOW, max_age_seconds=MAX_AGE))
    await database.run(lambda c: revoke_session(c, sid="s-1", now=NOW, max_age_seconds=MAX_AGE))

    count = await database.run(lambda c: c.execute("SELECT count(*) FROM revoked_session").fetchone()[0])
    assert count == 1
