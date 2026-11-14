# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Valheim ban-file adapter (AC12.3 mechanism, and AC12.4 restart detection), against the Pterodactyl fake.

Every test runs against FakePteroState on httpx MockTransport, with a fixed clock and a set uptime. No test reaches a
real server. A line that someone else placed is never removed, and every test that places one registers it, so an
autouse guard checks it is still in the file when the test ends.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from hallmonitor import own
from hallmonitor.adapters.base import Capabilities, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.registry import build_adapter
from hallmonitor.adapters.steam import STEAM64_BASE
from hallmonitor.adapters.valheim import ValheimAdapter
from hallmonitor.client import MonitorClient
from hallmonitor.config import Config
from hallmonitor.contract import Identity, Subject
from hallmonitor.db import Database
from hallmonitor.reconcile import cycle
from tests.fake_hallpass import TOKEN, FakeHallpassState
from tests.fake_hallpass import make_transport as make_hallpass_transport
from tests.fake_ptero_files import PTERO_KEY, FakePteroState
from tests.fake_ptero_files import make_transport as make_ptero_transport

URL = "https://fake.test"
SLUG = "val"
SERVER = "valsrv1"
PATH = "bannedlist.txt"
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TEN_MINUTES = timedelta(minutes=10)
HOUR_MS = 3_600_000
FIVE_MINUTES_MS = 300_000
PENDING = "pending_restart"
OWN_ID = str(STEAM64_BASE + 1001)
OTHER_ID = str(STEAM64_BASE + 1002)
THIRD_ID = str(STEAM64_BASE + 1003)
COMMENT = "// bans placed by the server admin\n"
SEEN: list[FakePteroState] = []
MODERATOR_LINES: list[tuple[FakePteroState, str]] = []


@pytest.fixture(autouse=True)
def no_line_placed_by_someone_else_is_removed() -> Iterator[None]:
    """A line that a moderator placed (registered with moderator_line) is still in the file when the test ends."""
    SEEN.clear()
    MODERATOR_LINES.clear()
    yield
    for state, identity in MODERATOR_LINES:
        lines = [line.strip() for line in state.files[(SERVER, PATH)].splitlines()]
        assert identity in lines, "the Valheim adapter must never remove a line it did not place"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hallmonitor.db")
    try:
        yield database
    finally:
        database.close()


def ptero_state(text: str = COMMENT, *, uptime_ms: int = HOUR_MS, state: str = "running") -> FakePteroState:
    fake = FakePteroState(files={(SERVER, PATH): text}, uptime_ms=uptime_ms, state=state)
    SEEN.append(fake)
    return fake


def moderator_line(fake: FakePteroState, identity: str) -> None:
    """Appends an id line the way a moderator's ban would appear in the file, and registers it for the guard."""
    text = fake.files[(SERVER, PATH)]
    fake.files[(SERVER, PATH)] = text + ("" if not text or text.endswith("\n") else "\n") + identity + "\n"
    MODERATOR_LINES.append((fake, identity))


def file_text(fake: FakePteroState) -> str:
    return fake.files[(SERVER, PATH)]


def writes(fake: FakePteroState) -> list[str]:
    return [method for method, _ in fake.requests if method != "GET"]


def steam(value: str) -> dict[str, str]:
    return {"kind": "steam_id", "value": value}


def subject(ref: str, desired: str, *identities: dict[str, str], generation: int = 1) -> dict[str, Any]:
    return {"ref": ref, "identities": list(identities), "desired": desired, "generation": generation}


def valheim_service(*subjects: dict[str, Any]) -> FakeHallpassState:
    return FakeHallpassState(services=[{"slug": SLUG, "adapter": "valheim", "subjects": list(subjects)}])


def outcomes(hallpass: FakeHallpassState) -> list[tuple[str, str, str | None]]:
    return [(entry["ref"], entry["observed"], entry["detail"]) for entry in hallpass.reports]


SETTINGS = {
    "ptero_url": URL,
    "ptero_key": PTERO_KEY,
    "ptero_server": SERVER,
    "banlist_path": PATH,
}


def ctx(db: Database, http: httpx.AsyncClient, at: datetime, live_reload: str = "false",
        settings: dict[str, str] | None = None) -> AdapterContext:
    chosen = dict(SETTINGS if settings is None else settings)
    if settings is None:
        chosen["live_reload"] = live_reload
    return AdapterContext(slug=SLUG, settings=chosen, config=Config(hallpass_url=URL, hallpass_token=TOKEN),
                          db=db, http=http, clock=lambda: at)


async def run(db: Database, hallpass: FakeHallpassState, fake: FakePteroState, at: datetime,
              live_reload: str = "false") -> None:
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, at, live_reload))
        assert isinstance(adapter, ValheimAdapter)
        client = MonitorClient(URL, TOKEN, transport=make_hallpass_transport(hallpass))
        try:
            await cycle(client=client, adapter_for=lambda s: adapter if s.slug == SLUG else None, now=at)
        finally:
            await client.aclose()


async def record_of(db: Database, identity: str) -> tuple[str, datetime] | None:
    return await db.run(lambda c: own.own_record(c, SLUG, identity))


# AC12.3 (mechanism): suspend adds the id line and keeps the rest; restore removes only that line ------------

async def test_a_suspension_appends_the_id_and_keeps_comments_and_blank_lines(db: Database) -> None:
    fake = ptero_state(COMMENT + "\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == COMMENT + "\n" + OWN_ID + "\n"
    assert await record_of(db, OWN_ID) == ("ban", T0)
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]


@pytest.mark.parametrize(("before", "after"), [
    ("", OWN_ID + "\n"),
    (COMMENT, COMMENT + OWN_ID + "\n"),
    (COMMENT.rstrip("\n"), COMMENT.rstrip("\n") + "\n" + OWN_ID),
])
async def test_the_trailing_newline_is_kept_as_it_was(db: Database, before: str, after: str) -> None:
    fake = ptero_state(before)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == after


async def test_a_restore_removes_only_that_line_and_keeps_every_other_line(db: Database) -> None:
    fake = ptero_state(COMMENT + "\n")
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    moderator_line(fake, OTHER_ID)
    await run(db, valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2)), fake, T0)
    assert file_text(fake) == COMMENT + "\n" + OTHER_ID + "\n"
    assert await record_of(db, OWN_ID) == ("restore", T0)


async def test_a_restore_removes_one_of_two_lines_for_the_id_and_leaves_a_moderators_line_standing(db: Database) -> None:
    """Our ban and a moderator's ban for the same id are two identical lines. The restore removes one of them, and the
    line that remains is not ours, so the next cycle reports it as banned_external and removes nothing more."""
    fake = ptero_state(COMMENT)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    moderator_line(fake, OWN_ID)
    await run(db, valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2)), fake, T0)
    assert file_text(fake) == COMMENT + OWN_ID + "\n"
    assert await record_of(db, OWN_ID) == ("restore", T0)

    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=3))
    writes_before = len(writes(fake))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "banned_external", None)]
    assert len(writes(fake)) == writes_before
    assert file_text(fake) == COMMENT + OWN_ID + "\n"


async def test_a_restore_keeps_a_file_that_has_no_trailing_newline_without_one(db: Database) -> None:
    fake = ptero_state(COMMENT + OWN_ID)
    await record_own(db, OWN_ID, "ban", T0)
    await run(db, valheim_service(subject("r1", "allowed", steam(OWN_ID))), fake, T0)
    assert file_text(fake) == COMMENT.rstrip("\n")


async def record_own(db: Database, identity: str, ref: str, at: datetime) -> None:
    await db.run(lambda c: own.record_own(c, SLUG, identity, ref, at))


# AC12.3: a line someone else placed is banned_external and is never removed -------------------------------

async def test_a_line_placed_by_someone_else_is_banned_external_and_never_removed(db: Database) -> None:
    fake = ptero_state(COMMENT)
    moderator_line(fake, OTHER_ID)
    before = file_text(fake)
    hallpass = valheim_service(subject("r1", "allowed", steam(OTHER_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "banned_external", None)]
    assert file_text(fake) == before
    assert writes(fake) == []


async def test_a_moderator_line_is_left_alone_when_the_desired_state_is_suspended(db: Database) -> None:
    fake = ptero_state(COMMENT)
    moderator_line(fake, OTHER_ID)
    before = file_text(fake)
    hallpass = valheim_service(subject("r1", "suspended", steam(OTHER_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "banned_external", None)]
    assert file_text(fake) == before
    assert writes(fake) == []


async def test_a_moderator_re_add_during_a_pending_restore_is_banned_external(db: Database) -> None:
    fake = ptero_state(COMMENT)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    await run(db, valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2)), fake, T0)
    moderator_line(fake, OWN_ID)
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=3))
    writes_before = len(writes(fake))
    await run(db, hallpass, fake, T0 + timedelta(minutes=1))
    assert outcomes(hallpass)[-1] == ("r1", "banned_external", None)
    assert len(writes(fake)) == writes_before
    assert OWN_ID in [line.strip() for line in file_text(fake).splitlines()]


async def test_a_ban_whose_line_a_moderator_removed_is_forgotten_and_a_later_re_add_is_banned_external(
        db: Database) -> None:
    """The forget step: our record outlives a line that was removed outside the adapter. The record is forgotten with
    no write, and a moderator who then re-adds the id is banned_external, whose line is never removed."""
    fake = ptero_state(COMMENT)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    fake.files[(SERVER, PATH)] = COMMENT                       # a moderator's unban, made through the game
    writes_before = len(writes(fake))
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0)
    assert await record_of(db, OWN_ID) is None
    assert outcomes(hallpass) == [("r1", "allowed", None)]
    assert len(writes(fake)) == writes_before

    moderator_line(fake, OWN_ID)                               # the moderator re-adds the id
    hallpass.services[0]["subjects"] = [subject("r1", "allowed", steam(OWN_ID), generation=3)]
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass)[-1] == ("r1", "banned_external", None)
    assert len(writes(fake)) == writes_before
    assert OWN_ID in [line.strip() for line in file_text(fake).splitlines()]


# AC12.4: restart detection, with live_reload off -----------------------------------------------------------

async def test_a_pending_suspension_reports_pending_restart_until_the_server_restarts(db: Database) -> None:
    fake = ptero_state(COMMENT, uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]


async def test_a_restart_after_the_suspension_clears_pending_restart(db: Database) -> None:
    fake = ptero_state(COMMENT, uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    fake.uptime_ms = FIVE_MINUTES_MS
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass) == [("r1", "suspended", None)]


@pytest.mark.parametrize(("uptime_seconds", "pending"), [
    (599, True),   # one second short of the time since the change: the margin keeps it pending
    (600, True),   # exactly the time since the change: the server may have started at the change
    (598, False),  # two seconds short: the server started after the change, so it has restarted
])
async def test_the_restart_test_keeps_the_error_on_the_pending_side_by_one_second(
        db: Database, uptime_seconds: int, pending: bool) -> None:
    """placed_at is stored to the second, so the time since the change can run a second over. A one-second margin keeps a
    change pending when the uptime is within a second of that time."""
    fake = ptero_state(COMMENT, uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    fake.uptime_ms = uptime_seconds * 1000
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass) == [("r1", "suspended", PENDING if pending else None)]


async def test_a_stopped_server_keeps_a_suspension_pending(db: Database) -> None:
    fake = ptero_state(COMMENT, uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    fake.state = "offline"
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]


async def test_a_pending_restore_reports_allowed_with_pending_restart_then_is_forgotten_after_a_restart(
        db: Database) -> None:
    fake = ptero_state(COMMENT, uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    await run(db, valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2)), fake, T0)
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=3))
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass) == [("r1", "allowed", PENDING)]
    assert await record_of(db, OWN_ID) == ("restore", T0)

    fake.uptime_ms = FIVE_MINUTES_MS
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass)[-1] == ("r1", "allowed", None)
    assert await record_of(db, OWN_ID) is None


# AC12.4: with live_reload on, no change is ever pending -------------------------------------------------------

async def test_with_live_reload_no_detail_is_reported_at_any_point(db: Database) -> None:
    fake = ptero_state(COMMENT, uptime_ms=HOUR_MS)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0, live_reload="true")
    await run(db, hallpass, fake, T0 + TEN_MINUTES, live_reload="true")
    fake.uptime_ms = FIVE_MINUTES_MS
    await run(db, hallpass, fake, T0 + TEN_MINUTES, live_reload="true")
    assert outcomes(hallpass) == [("r1", "suspended", None)] * 3


async def test_with_live_reload_a_restore_is_never_pending(db: Database) -> None:
    fake = ptero_state(COMMENT, uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0, live_reload="true")
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0, live_reload="true")
    assert outcomes(hallpass) == [("r1", "allowed", None)]
    await run(db, hallpass, fake, T0, live_reload="true")
    assert outcomes(hallpass)[-1] == ("r1", "allowed", None)
    assert await record_of(db, OWN_ID) is None


# Identity and errors ----------------------------------------------------------------------------------------

async def test_a_subject_without_a_steam_id_is_no_identity_and_nothing_is_written(db: Database) -> None:
    fake = ptero_state(COMMENT)
    hallpass = valheim_service(subject("r1", "suspended"))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "no_identity")]
    assert writes(fake) == []


@pytest.mark.parametrize("value", ["12a", "", "+" + OWN_ID, str(STEAM64_BASE - 2), OWN_ID + "\n", "9" * 25,
                                   "0" + OWN_ID, str(STEAM64_BASE + (1 << 32))])
async def test_a_malformed_steam_id_is_bad_identity_and_nothing_is_written(db: Database, value: str) -> None:
    fake = ptero_state(COMMENT)
    hallpass = valheim_service(subject("r1", "suspended", steam(value)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "bad_identity")]
    assert file_text(fake) == COMMENT
    assert writes(fake) == []


async def test_a_wrong_pterodactyl_key_is_an_auth_error_for_every_subject(db: Database) -> None:
    fake = FakePteroState(files={(SERVER, PATH): COMMENT}, key="not-the-key")
    SEEN.append(fake)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)),
                               subject("r2", "suspended", steam(OTHER_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "auth"), ("r2", "error", "auth")]
    assert file_text(fake) == COMMENT


async def test_a_failed_write_is_that_subject_only_and_the_others_still_apply(db: Database) -> None:
    fake = ptero_state(COMMENT)
    fake.fail_next = {"write": 500}
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)),
                               subject("r2", "suspended", steam(OTHER_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "http"), ("r2", "suspended", PENDING)]
    assert file_text(fake) == COMMENT + OTHER_ID + "\n"
    assert await record_of(db, OWN_ID) is None
    assert await record_of(db, OTHER_ID) == ("ban", T0)


async def test_a_restore_that_changes_nothing_writes_nothing(db: Database) -> None:
    fake = ptero_state(COMMENT)
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, T0))
        assert isinstance(adapter, ValheimAdapter)
        result = await adapter.apply(Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 2), "allowed")
    assert result == Observed("allowed")
    assert writes(fake) == []
    assert file_text(fake) == COMMENT


# Capabilities, unmanaged lines, and the registry ------------------------------------------------------------

async def test_the_valheim_capabilities_claim_suspend_restore_and_read_without_connected_removal(db: Database) -> None:
    async with httpx.AsyncClient(transport=make_ptero_transport(ptero_state())) as http:
        assert build_adapter("valheim", ctx(db, http, T0)).capabilities == Capabilities(
            suspend=True, restore=True, read_state=True, removes_connected=False)


async def test_unmanaged_counts_the_id_lines_that_match_no_subject(db: Database) -> None:
    fake = ptero_state(COMMENT + OWN_ID + "\n")
    moderator_line(fake, OTHER_ID)
    moderator_line(fake, THIRD_ID)
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, T0))
        known = [Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 1)]
        assert await adapter.unmanaged_count(known) == 2


@pytest.mark.parametrize("missing", sorted(SETTINGS))
async def test_the_registry_needs_every_valheim_setting(db: Database, caplog: pytest.LogCaptureFixture,
                                                         missing: str) -> None:
    settings = {key: value for key, value in SETTINGS.items() if key != missing}
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.INFO):
            assert build_adapter("valheim", ctx(db, http, T0, settings=settings)) is None
    assert [r.getMessage() for r in caplog.records].count("adapter_not_configured") == 1


@pytest.mark.parametrize("value", ["yes", "1", "TRUE"])
async def test_live_reload_must_be_true_or_false(db: Database, value: str) -> None:
    async with httpx.AsyncClient() as http:
        assert build_adapter("valheim", ctx(db, http, T0, live_reload=value)) is None


async def test_live_reload_defaults_to_false_and_the_adapter_is_built(db: Database) -> None:
    settings = dict(SETTINGS)
    async with httpx.AsyncClient() as http:
        assert isinstance(build_adapter("valheim", ctx(db, http, T0, settings=settings)), ValheimAdapter)


async def test_apply_refuses_to_restore_a_line_nobody_here_placed(db: Database) -> None:
    """Reconcile never applies to a banned_external subject, so the apply's own refusal is checked directly."""
    fake = ptero_state(COMMENT)
    moderator_line(fake, OTHER_ID)
    before = file_text(fake)
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, T0))
        assert isinstance(adapter, ValheimAdapter)
        result = await adapter.apply(Subject("r1", (Identity("steam_id", OTHER_ID),), "allowed", 2), "allowed")
    assert result == Observed("banned_external")
    assert file_text(fake) == before
    assert writes(fake) == []
