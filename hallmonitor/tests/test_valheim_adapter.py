# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Valheim permitted-list adapter, against the Pterodactyl fake.

A subject is allowed when an entry of permittedlist.txt names its SteamID64, in bare digits, as Steam_ digits or
as V_ digits. The ban list, bannedlist.txt, is read only. Every test runs against FakePteroState on httpx
MockTransport, with a fixed clock and a set uptime. No test reaches a real server.

An autouse guard checks that the ban list is never written: a test that uses a ban list ends with its text unchanged,
apart from any line that the test added by hand as a moderator would.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator
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
PERMITTED = "permittedlist.txt"
BANNED = "bannedlist.txt"
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
ONE_MINUTE = timedelta(minutes=1)
TEN_MINUTES = timedelta(minutes=10)
HOUR_MS = 3_600_000
FIVE_MINUTES_MS = 300_000
PENDING = "pending_restart"
OWN_ID = str(STEAM64_BASE + 1001)
OTHER_ID = str(STEAM64_BASE + 1002)
THIRD_ID = str(STEAM64_BASE + 1003)
BOM = "\ufeff"
PERMITTED_COMMENT = "// players allowed on the server\n"
BANNED_COMMENT = "// bans placed by the server admin\n"
REQUIRED = ("ptero_url", "ptero_key", "ptero_server", "permittedlist_path")
BAN_GUARD: dict[int, tuple[FakePteroState, str]] = {}   # id(state) -> (state, the ban list text it must still hold)


@pytest.fixture(autouse=True)
def the_ban_list_is_never_written() -> Iterator[None]:
    BAN_GUARD.clear()
    yield
    for fake, expected in BAN_GUARD.values():
        assert fake.files[(SERVER, BANNED)] == expected, "the Valheim adapter must never write the ban list"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hallmonitor.db")
    try:
        yield database
    finally:
        database.close()


def ptero_state(permitted: str = PERMITTED_COMMENT, banned: str | None = BANNED_COMMENT, *,
                uptime_ms: int = HOUR_MS, state: str = "running") -> FakePteroState:
    files = {(SERVER, PERMITTED): permitted}
    if banned is not None:
        files[(SERVER, BANNED)] = banned
    fake = FakePteroState(files=files, uptime_ms=uptime_ms, state=state)
    if banned is not None:
        BAN_GUARD[id(fake)] = (fake, banned)
    return fake


def steam_form(steam_id: str) -> str:
    return "Steam_" + steam_id


def v_form(steam_id: str) -> str:
    return "V_" + steam_id


# The three forms Valheim accepts for one id, as (parametrize id, line builder).
FORMS = [("bare", lambda steam_id: steam_id), ("steam", steam_form), ("v", v_form)]


def add_line(fake: FakePteroState, path: str, line: str) -> None:
    """Appends a line the way a hand edit would. A line added to the ban list is the moderator's own, so the guard
    expects it."""
    text = fake.files[(SERVER, path)]
    fake.files[(SERVER, path)] = text + ("" if not text or text.endswith("\n") else "\n") + line + "\n"
    if path == BANNED and id(fake) in BAN_GUARD:
        BAN_GUARD[id(fake)] = (fake, fake.files[(SERVER, path)])


def file_text(fake: FakePteroState, path: str = PERMITTED) -> str:
    return fake.files[(SERVER, path)]


def writes(fake: FakePteroState) -> list[str]:
    return [method for method, _ in fake.requests if method != "GET"]


def resource_reads(fake: FakePteroState) -> int:
    return sum(1 for _, path in fake.requests if path.endswith("/resources"))


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
    "permittedlist_path": PERMITTED,
    "bannedlist_path": BANNED,
}
NO_BAN_LIST = {key: value for key, value in SETTINGS.items() if key != "bannedlist_path"}


def ctx(db: Database, http: httpx.AsyncClient, at: datetime, live_reload: str = "false",
        settings: dict[str, str] | None = None) -> AdapterContext:
    chosen = dict(SETTINGS if settings is None else settings)
    if settings is None:
        chosen["live_reload"] = live_reload
    return AdapterContext(slug=SLUG, settings=chosen, config=Config(hallpass_url=URL, hallpass_token=TOKEN),
                          db=db, http=http, clock=lambda: at)


async def run(db: Database, hallpass: FakeHallpassState, fake: FakePteroState, at: datetime,
              live_reload: str = "false", settings: dict[str, str] | None = None) -> None:
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, at, live_reload, settings))
        assert isinstance(adapter, ValheimAdapter)
        client = MonitorClient(URL, TOKEN, transport=make_hallpass_transport(hallpass))
        try:
            await cycle(client=client, adapter_for=lambda s: adapter if s.slug == SLUG else None, now=at)
        finally:
            await client.aclose()


async def record_of(db: Database, identity: str) -> tuple[str, datetime] | None:
    return await db.run(lambda c: own.own_record(c, SLUG, identity))


# Reading the permitted list ----------------------------------------------------------------------------------------

async def test_a_bare_id_line_lists_the_subject_as_allowed(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n")
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "allowed", None)]
    assert writes(fake) == []


async def test_a_steam_prefixed_line_lists_the_subject_as_allowed(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + steam_form(OWN_ID) + "\n")
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "allowed", None)]
    assert writes(fake) == []


async def test_a_subject_named_in_no_form_is_suspended_and_nothing_is_written(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + steam_form(OTHER_ID) + "\n" + v_form(OTHER_ID) + "\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "suspended", None)]
    assert writes(fake) == []


async def test_a_v_prefixed_line_lists_the_subject_as_allowed(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + v_form(OWN_ID) + "\n")
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "allowed", None)]
    assert writes(fake) == []


# Allowing: append the V_ form and keep the rest ---------------------------------------------------------------------

async def test_allowing_a_suspended_subject_appends_the_v_form_and_keeps_the_other_lines(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + "\n" + steam_form(OTHER_ID) + "\n")
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=3))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == PERMITTED_COMMENT + "\n" + steam_form(OTHER_ID) + "\n" + v_form(OWN_ID) + "\n"
    assert await record_of(db, OWN_ID) == ("add", T0)
    assert outcomes(hallpass) == [("r1", "allowed", PENDING)]


@pytest.mark.parametrize(("before", "after"), [
    ("", v_form(OWN_ID) + "\n"),
    (PERMITTED_COMMENT, PERMITTED_COMMENT + v_form(OWN_ID) + "\n"),
    (PERMITTED_COMMENT.rstrip("\n"), PERMITTED_COMMENT.rstrip("\n") + "\n" + v_form(OWN_ID)),
])
async def test_the_trailing_newline_is_kept_as_it_was(db: Database, before: str, after: str) -> None:
    fake = ptero_state(before)
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == after


# Suspending: remove every line that names the id, in any of its three forms ---------------------------------------

async def test_suspending_removes_all_three_forms_of_the_id_and_nothing_else(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + "\n" + OTHER_ID + "\n" + "  " + steam_form(OWN_ID) + "  \n"
                       + OWN_ID + "\n" + v_form(OWN_ID) + "\n" + steam_form(THIRD_ID) + "\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == PERMITTED_COMMENT + "\n" + OTHER_ID + "\n" + steam_form(THIRD_ID) + "\n"
    assert await record_of(db, OWN_ID) == ("remove", T0)
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]


async def test_a_suspension_that_leaves_another_platform_entry_is_written(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + "OtherPlatform_placeholder\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == PERMITTED_COMMENT + "OtherPlatform_placeholder\n"
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]


async def test_a_suspension_that_leaves_only_another_ids_v_entry_is_written(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + v_form(OTHER_ID) + "\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == PERMITTED_COMMENT + v_form(OTHER_ID) + "\n"
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]


async def test_suspending_the_last_entry_writes_nothing_and_reports_would_open_server(
        db: Database, caplog: pytest.LogCaptureFixture) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n")
    before = file_text(fake)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    with caplog.at_level(logging.INFO):
        await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "would_open_server")]
    assert file_text(fake) == before
    assert writes(fake) == []
    assert await record_of(db, OWN_ID) is None
    assert any(record.getMessage() == "not_applied" for record in caplog.records)
    assert all(OWN_ID not in str(vars(record)) for record in caplog.records)


# The ban list: read only, and a banned id is never added or removed ------------------------------------------------

@pytest.mark.parametrize("desired", ["allowed", "suspended"])
@pytest.mark.parametrize("listed", [True, False], ids=["listed", "unlisted"])
@pytest.mark.parametrize("build", [line for _, line in FORMS], ids=[name for name, _ in FORMS])
async def test_a_ban_list_entry_in_any_form_is_banned_external_and_nothing_is_written(
        db: Database, build: Callable[[str], str], listed: bool, desired: str) -> None:
    permitted = PERMITTED_COMMENT + (v_form(OTHER_ID) + "\n" if listed else "")
    fake = ptero_state(permitted, BANNED_COMMENT + build(OTHER_ID) + "\n")
    before = (file_text(fake), file_text(fake, BANNED))
    hallpass = valheim_service(subject("r1", desired, steam(OTHER_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "banned_external", None)]
    assert (file_text(fake), file_text(fake, BANNED)) == before
    assert writes(fake) == []


async def test_apply_allowed_refuses_a_banned_id_and_adds_nothing(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + steam_form(THIRD_ID) + "\n", BANNED_COMMENT + OWN_ID + "\n")
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, T0))
        assert isinstance(adapter, ValheimAdapter)
        result = await adapter.apply(Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 2), "allowed")
    assert result == Observed("banned_external")
    assert writes(fake) == []


async def test_the_ban_list_is_optional_and_is_not_read_without_its_setting(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT, banned=None)
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0, settings=NO_BAN_LIST)
    assert outcomes(hallpass) == [("r1", "allowed", PENDING)]
    assert file_text(fake) == PERMITTED_COMMENT + v_form(OWN_ID) + "\n"


# Live reload on: nothing is pending, and the uptime is not read ----------------------------------------------------

async def test_with_live_reload_an_allow_is_never_pending_and_the_uptime_is_not_read(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT, uptime_ms=HOUR_MS)
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0, live_reload="true")
    assert outcomes(hallpass) == [("r1", "allowed", None)]
    assert resource_reads(fake) == 0
    assert await record_of(db, OWN_ID) is None


async def test_with_live_reload_a_removal_is_never_pending(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n", uptime_ms=HOUR_MS)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0, live_reload="true")
    assert outcomes(hallpass) == [("r1", "suspended", None)]
    assert resource_reads(fake) == 0
    assert file_text(fake) == PERMITTED_COMMENT + OTHER_ID + "\n"


@pytest.mark.parametrize(("live_reload", "removes_connected"), [("true", True), ("false", False)])
async def test_removes_connected_follows_live_reload(db: Database, live_reload: str, removes_connected: bool) -> None:
    async with httpx.AsyncClient(transport=make_ptero_transport(ptero_state())) as http:
        assert build_adapter("valheim", ctx(db, http, T0, live_reload=live_reload)).capabilities == Capabilities(
            suspend=True, restore=True, read_state=True, removes_connected=removes_connected)


# Live reload off: a change is pending until a restart, then its record is forgotten --------------------------------

async def test_an_allow_is_pending_until_the_server_restarts_and_then_its_record_is_forgotten(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT, uptime_ms=HOUR_MS)
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "allowed", PENDING)]
    assert await record_of(db, OWN_ID) == ("add", T0)

    fake.uptime_ms = FIVE_MINUTES_MS
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass)[-1] == ("r1", "allowed", None)
    assert await record_of(db, OWN_ID) is None


async def test_a_removal_is_pending_until_the_server_restarts_and_then_its_record_is_forgotten(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n", uptime_ms=HOUR_MS)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]

    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass)[-1] == ("r1", "suspended", PENDING)

    fake.uptime_ms = FIVE_MINUTES_MS
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass)[-1] == ("r1", "suspended", None)
    assert await record_of(db, OWN_ID) is None


@pytest.mark.parametrize(("uptime_seconds", "pending"), [
    (599, True),   # one second short of the time since the change: the margin keeps it pending
    (600, True),   # exactly the time since the change: the server may have started at the change
    (598, False),  # two seconds short: the server started after the change, so it has restarted
])
async def test_the_restart_test_keeps_the_error_on_the_pending_side_by_one_second(
        db: Database, uptime_seconds: int, pending: bool) -> None:
    """placed_at is stored to the second, so the time since the change can run a second over. A one-second margin
    keeps a change pending when the uptime is within a second of that time."""
    fake = ptero_state(PERMITTED_COMMENT, uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "allowed", steam(OWN_ID))), fake, T0)
    fake.uptime_ms = uptime_seconds * 1000
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass) == [("r1", "allowed", PENDING if pending else None)]


async def test_a_stopped_server_keeps_an_allow_pending(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT, uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "allowed", steam(OWN_ID))), fake, T0)
    fake.state = "offline"
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0 + TEN_MINUTES)
    assert outcomes(hallpass) == [("r1", "allowed", PENDING)]


# Records that no longer match the file, and lines added by hand ---------------------------------------------------

async def test_a_removal_record_for_an_id_that_is_listed_again_is_forgotten_without_a_write(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n", uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    add_line(fake, PERMITTED, OWN_ID)
    writes_before = len(writes(fake))
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0 + ONE_MINUTE)
    assert outcomes(hallpass) == [("r1", "allowed", None)]
    assert await record_of(db, OWN_ID) is None
    assert len(writes(fake)) == writes_before


async def test_a_line_added_by_hand_for_a_suspended_subject_is_removed_again_on_the_next_apply(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    add_line(fake, PERMITTED, steam_form(OWN_ID))
    await run(db, hallpass, fake, T0 + ONE_MINUTE)
    assert outcomes(hallpass)[-1] == ("r1", "suspended", PENDING)
    assert file_text(fake) == PERMITTED_COMMENT + OTHER_ID + "\n"
    assert await record_of(db, OWN_ID) == ("remove", T0 + ONE_MINUTE)


# Unmanaged entries ------------------------------------------------------------------------------------------------

async def test_unmanaged_counts_each_entry_that_names_no_known_subject(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + "\n" + OWN_ID + "\n" + steam_form(OWN_ID) + "\n" + v_form(OWN_ID) + "\n"
                       + steam_form(OTHER_ID) + "\n" + v_form(OTHER_ID) + "\n" + THIRD_ID + "\n"
                       + "OtherPlatform_placeholder\n")
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, T0))
        known = [Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 1)]
        assert await adapter.unmanaged_count(known) == 4


# Identity and errors ----------------------------------------------------------------------------------------------

async def test_a_subject_without_a_steam_id_is_no_identity_and_nothing_is_written(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT)
    hallpass = valheim_service(subject("r1", "suspended"))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "no_identity")]
    assert writes(fake) == []


@pytest.mark.parametrize("value", ["12a", "", "+" + OWN_ID, str(STEAM64_BASE - 2), OWN_ID + "\n", "9" * 25,
                                   "0" + OWN_ID, str(STEAM64_BASE + (1 << 32)), steam_form(OWN_ID)])
async def test_a_malformed_steam_id_is_bad_identity_and_nothing_is_written(db: Database, value: str) -> None:
    fake = ptero_state(PERMITTED_COMMENT)
    hallpass = valheim_service(subject("r1", "allowed", steam(value)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "bad_identity")]
    assert file_text(fake) == PERMITTED_COMMENT
    assert writes(fake) == []


async def test_a_wrong_pterodactyl_key_is_an_auth_error_for_every_subject(db: Database) -> None:
    fake = FakePteroState(files={(SERVER, PERMITTED): PERMITTED_COMMENT}, key="not-the-key")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)),
                               subject("r2", "suspended", steam(OTHER_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "auth"), ("r2", "error", "auth")]
    assert file_text(fake) == PERMITTED_COMMENT


async def test_a_failed_write_is_that_subject_only_and_the_others_still_apply(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n")
    fake.fail_next = {"write": 500}
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)),
                               subject("r2", "allowed", steam(THIRD_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "http"), ("r2", "allowed", PENDING)]
    assert file_text(fake) == PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n" + v_form(THIRD_ID) + "\n"
    assert await record_of(db, OWN_ID) is None   # no earlier record, so the failed write leaves none
    assert await record_of(db, THIRD_ID) == ("add", T0)


# Registry settings -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("missing", REQUIRED)
async def test_the_registry_needs_every_required_valheim_setting(db: Database, caplog: pytest.LogCaptureFixture,
                                                                 missing: str) -> None:
    settings = {key: value for key, value in SETTINGS.items() if key != missing}
    async with httpx.AsyncClient() as http:
        assert isinstance(build_adapter("valheim", ctx(db, http, T0, settings=dict(SETTINGS))), ValheimAdapter)
        with caplog.at_level(logging.INFO):
            assert build_adapter("valheim", ctx(db, http, T0, settings=settings)) is None
    assert [r.getMessage() for r in caplog.records].count("adapter_not_configured") == 1


async def test_the_old_banlist_path_setting_is_not_enough_for_the_permitted_list(db: Database) -> None:
    settings = {key: value for key, value in SETTINGS.items() if key != "permittedlist_path"}
    settings["banlist_path"] = BANNED
    async with httpx.AsyncClient() as http:
        assert build_adapter("valheim", ctx(db, http, T0, settings=settings)) is None


@pytest.mark.parametrize("value", ["yes", "1", "TRUE"])
async def test_live_reload_must_be_true_or_false(db: Database, value: str) -> None:
    async with httpx.AsyncClient() as http:
        assert isinstance(build_adapter("valheim", ctx(db, http, T0, live_reload="true")), ValheimAdapter)
        assert build_adapter("valheim", ctx(db, http, T0, live_reload=value)) is None


async def test_live_reload_defaults_to_false_and_the_adapter_is_built(db: Database) -> None:
    async with httpx.AsyncClient() as http:
        adapter = build_adapter("valheim", ctx(db, http, T0, settings=dict(SETTINGS)))
    assert isinstance(adapter, ValheimAdapter)
    assert adapter.capabilities.removes_connected is False


async def test_apply_suspended_on_an_unlisted_id_writes_nothing(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + steam_form(OTHER_ID) + "\n")
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, T0))
        assert isinstance(adapter, ValheimAdapter)
        result = await adapter.apply(Subject("r1", (Identity("steam_id", OWN_ID),), "suspended", 2), "suspended")
    assert result == Observed("suspended")
    assert writes(fake) == []


# The guard: the forms of one id, blanks and indented comments, an entry-less file ------------------------------------

@pytest.mark.parametrize("lines", [
    OWN_ID + "\n" + steam_form(OWN_ID) + "\n",
    steam_form(OWN_ID) + "\n" + OWN_ID + "\n",
    v_form(OWN_ID) + "\n" + steam_form(OWN_ID) + "\n" + OWN_ID + "\n",
    steam_form(OWN_ID) + "\n" + v_form(OWN_ID) + "\n",
    v_form(OWN_ID) + "\n",
], ids=["bare_then_steam", "steam_then_bare", "v_steam_bare", "steam_then_v", "v_only"])
async def test_suspending_when_the_only_entries_are_forms_of_the_id_writes_nothing(db: Database, lines: str) -> None:
    fake = ptero_state(PERMITTED_COMMENT + lines)
    before = file_text(fake)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "would_open_server")]
    assert file_text(fake) == before
    assert writes(fake) == []
    assert await record_of(db, OWN_ID) is None


@pytest.mark.parametrize("blanks", [
    "\n",
    "   \n\t\n",
    "  // indented comment\n",
    "\n  // one\n\t// two\n   \n",
], ids=["blank", "whitespace", "indented_comment", "mixed"])
async def test_suspending_is_refused_when_only_blanks_and_comments_would_remain(db: Database, blanks: str) -> None:
    fake = ptero_state(PERMITTED_COMMENT + blanks + OWN_ID + "\n")
    before = file_text(fake)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "would_open_server")]
    assert file_text(fake) == before
    assert writes(fake) == []


@pytest.mark.parametrize("text", ["", "\n  \n", PERMITTED_COMMENT + "  // indented\n"],
                         ids=["empty", "blank", "comments"])
async def test_suspending_on_an_entry_less_file_writes_nothing_and_reports_suspended(
        db: Database, text: str) -> None:
    fake = ptero_state(text)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "suspended", None)]
    assert file_text(fake) == text
    assert writes(fake) == []
    assert await record_of(db, OWN_ID) is None


# Ids that are only similar: a prefix or a superstring names another id, and a lower-case prefix is not the form ------

async def test_removal_keeps_an_id_that_is_a_prefix_or_a_superstring_of_the_subject_id(db: Database) -> None:
    superstring, prefix = steam_form(OWN_ID + "0"), OWN_ID[:-1]
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + superstring + "\n" + prefix + "\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == PERMITTED_COMMENT + superstring + "\n" + prefix + "\n"
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]


@pytest.mark.parametrize("line", [OWN_ID + "0", OWN_ID[:-1], steam_form(OWN_ID + "0"), steam_form(OWN_ID[:-1]),
                                  v_form(OWN_ID + "0"), v_form(OWN_ID[:-1])],
                         ids=["bare_superstring", "bare_prefix", "steam_superstring", "steam_prefix",
                              "v_superstring", "v_prefix"])
async def test_a_prefix_or_superstring_line_does_not_list_the_subject(db: Database, line: str) -> None:
    fake = ptero_state(PERMITTED_COMMENT + line + "\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "suspended", None)]
    assert writes(fake) == []


@pytest.mark.parametrize("prefix", ["steam_", "v_", "STEAM_"], ids=["steam", "v", "upper_steam"])
async def test_a_lower_case_or_other_prefix_does_not_name_the_id(db: Database, prefix: str) -> None:
    fake = ptero_state(PERMITTED_COMMENT + prefix + OWN_ID + "\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "suspended", None)]
    assert writes(fake) == []


async def test_allowing_keeps_a_lower_case_steam_line_and_appends_the_v_form(db: Database) -> None:
    lower = "steam_" + OWN_ID
    fake = ptero_state(PERMITTED_COMMENT + lower + "\n")
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == PERMITTED_COMMENT + lower + "\n" + v_form(OWN_ID) + "\n"
    assert outcomes(hallpass) == [("r1", "allowed", PENDING)]


@pytest.mark.parametrize("prefix", ["steam_", "v_"], ids=["steam", "v"])
async def test_a_lower_case_or_other_prefix_counts_as_unmanaged(db: Database, prefix: str) -> None:
    fake = ptero_state(PERMITTED_COMMENT + prefix + OWN_ID + "\n")
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, T0))
        known = [Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 1)]
        assert await adapter.unmanaged_count(known) == 1


# Live reload on: a record left by a change made with live reload off is forgotten ----------------------------------

async def test_with_live_reload_a_stale_record_is_forgotten_without_an_uptime_read(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n", uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    assert await record_of(db, OWN_ID) == ("remove", T0)
    fake.requests.clear()
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID), generation=2))
    await run(db, hallpass, fake, T0 + ONE_MINUTE, live_reload="true")
    assert outcomes(hallpass) == [("r1", "suspended", None)]
    assert await record_of(db, OWN_ID) is None
    assert resource_reads(fake) == 0


# Byte order marks: classified without the mark, kept in the file -----------------------------------------------------

async def test_a_bom_before_a_comment_does_not_make_it_an_entry(db: Database) -> None:
    fake = ptero_state(BOM + "// players allowed on the server\n" + OWN_ID + "\n")
    before = file_text(fake)
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "error", "would_open_server")]
    assert file_text(fake) == before
    assert writes(fake) == []


async def test_a_bom_before_the_first_line_id_is_read_as_listed(db: Database) -> None:
    fake = ptero_state(BOM + OWN_ID + "\n" + OTHER_ID + "\n")
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert outcomes(hallpass) == [("r1", "allowed", None)]
    assert writes(fake) == []


@pytest.mark.parametrize("build", [line for _, line in FORMS], ids=[name for name, _ in FORMS])
async def test_suspending_a_bom_first_line_removes_it_and_keeps_the_bom_at_the_start(
        db: Database, build: Callable[[str], str]) -> None:
    fake = ptero_state(BOM + build(OWN_ID) + "\n" + OTHER_ID + "\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == BOM + OTHER_ID + "\n"
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]


# Line endings: a CRLF file stays CRLF --------------------------------------------------------------------------------

CRLF_TEXT = "// c\r\n" + steam_form(OTHER_ID)


@pytest.mark.parametrize(("before", "after"), [
    (CRLF_TEXT + "\r\n", CRLF_TEXT + "\r\n" + v_form(OWN_ID) + "\r\n"),
    (CRLF_TEXT, CRLF_TEXT + "\r\n" + v_form(OWN_ID)),
], ids=["ends_with_newline", "no_final_newline"])
async def test_allowing_in_a_crlf_file_ends_the_new_line_the_way_the_file_ends_its_lines(
        db: Database, before: str, after: str) -> None:
    fake = ptero_state(before)
    hallpass = valheim_service(subject("r1", "allowed", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == after


async def test_suspending_in_a_crlf_file_keeps_the_other_lines_and_their_endings(db: Database) -> None:
    fake = ptero_state("// c\r\n" + OWN_ID + "\r\n" + steam_form(OTHER_ID) + "\r\n")
    hallpass = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, fake, T0)
    assert file_text(fake) == "// c\r\n" + steam_form(OTHER_ID) + "\r\n"
    assert outcomes(hallpass) == [("r1", "suspended", PENDING)]


# The write order and the uptime read after a change ------------------------------------------------------------------

async def test_a_done_change_does_not_read_the_uptime_so_an_uptime_failure_cannot_fail_it(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n", uptime_ms=HOUR_MS)
    fake.fail_next = {"resources": 500}
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, T0))
        assert isinstance(adapter, ValheimAdapter)
        result = await adapter.apply(Subject("r1", (Identity("steam_id", OWN_ID),), "suspended", 2), "suspended")
    assert result == Observed("suspended", PENDING)
    assert file_text(fake) == PERMITTED_COMMENT + OTHER_ID + "\n"
    assert resource_reads(fake) == 0
    assert await record_of(db, OWN_ID) == ("remove", T0)


async def test_a_failed_record_writes_nothing(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n")
    before = file_text(fake)

    def broken(*_args: object) -> None:
        raise sqlite3.OperationalError("record failed")

    monkeypatch.setattr(own, "record_own", broken)
    async with httpx.AsyncClient(transport=make_ptero_transport(fake)) as http:
        adapter = build_adapter("valheim", ctx(db, http, T0))
        assert isinstance(adapter, ValheimAdapter)
        with pytest.raises(sqlite3.OperationalError):
            await adapter.apply(Subject("r1", (Identity("steam_id", OWN_ID),), "suspended", 2), "suspended")
    assert file_text(fake) == before
    assert writes(fake) == []


async def test_a_failed_write_with_no_earlier_record_leaves_no_record(db: Database) -> None:
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n", uptime_ms=HOUR_MS)
    before = file_text(fake)
    fake.fail_next = {"write": 500}
    first = valheim_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, first, fake, T0)
    assert outcomes(first) == [("r1", "error", "http")]
    assert file_text(fake) == before
    assert await record_of(db, OWN_ID) is None

    second = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2))
    await run(db, second, fake, T0 + ONE_MINUTE)
    assert outcomes(second) == [("r1", "allowed", None)]
    assert await record_of(db, OWN_ID) is None


async def test_a_failed_write_keeps_the_earlier_pending_record(db: Database) -> None:
    """The earlier removal is still pending, since the server has not restarted. A failed allow must not erase it."""
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n", uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)
    assert await record_of(db, OWN_ID) == ("remove", T0)

    fake.fail_next = {"write": 500}
    second = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2))
    await run(db, second, fake, T0 + ONE_MINUTE)
    assert outcomes(second) == [("r1", "error", "http")]
    assert await record_of(db, OWN_ID) == ("remove", T0)

    third = valheim_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    await run(db, third, fake, T0 + 2 * ONE_MINUTE)
    assert outcomes(third) == [("r1", "suspended", PENDING)]
    assert await record_of(db, OWN_ID) == ("remove", T0)


async def test_a_failed_restore_keeps_the_write_reason_so_the_pass_still_stops(
        db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A write refused for auth, and then a database failure while putting the earlier record back: the subject still
    reports the write's own reason, so the loop stops the service's remaining applies."""
    fake = ptero_state(PERMITTED_COMMENT + OWN_ID + "\n" + OTHER_ID + "\n", uptime_ms=HOUR_MS)
    await run(db, valheim_service(subject("r1", "suspended", steam(OWN_ID))), fake, T0)

    async def broken_restore(self, steam_id, earlier) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ValheimAdapter, "_restore", broken_restore)
    fake.fail_next = {"write": 401}
    second = valheim_service(subject("r1", "allowed", steam(OWN_ID), generation=2),
                             subject("r2", "allowed", steam(THIRD_ID)))
    before = file_text(fake)
    with caplog.at_level(logging.WARNING):
        await run(db, second, fake, T0 + ONE_MINUTE)
    assert dict((ref, (state, detail)) for ref, state, detail in outcomes(second))["r1"] == ("error", "auth")
    assert file_text(fake) == before, "the service-wide stop keeps r2's add from being written"
    assert [r.getMessage() for r in caplog.records].count("record_restore_failed") == 1


# The two configured lists must name two files ----------------------------------------------------------------------

@pytest.mark.parametrize("banned", [
    PERMITTED,
    "/" + PERMITTED,
    " " + PERMITTED + " ",
    "//" + PERMITTED,
    "./" + PERMITTED,
    ".//" + PERMITTED,
    "x/../" + PERMITTED,
    "x/./../" + PERMITTED,
    "../" + PERMITTED,
    "/../" + PERMITTED,
    "x/../../" + PERMITTED,
], ids=["same", "leading_slash", "whitespace", "double_slash", "dot_segment", "dot_slash_double", "parent_segment",
        "mixed_segments", "above_root", "rooted_above_root", "two_above_root"])
async def test_a_ban_list_path_naming_the_permitted_list_is_not_configured(db: Database, banned: str) -> None:
    settings = dict(SETTINGS, bannedlist_path=banned)
    async with httpx.AsyncClient() as http:
        assert build_adapter("valheim", ctx(db, http, T0, settings=settings)) is None


async def test_a_ban_list_in_a_different_directory_is_a_different_file(db: Database) -> None:
    settings = dict(SETTINGS, bannedlist_path="a//" + PERMITTED)   # names a/permittedlist.txt, not permittedlist.txt
    async with httpx.AsyncClient() as http:
        assert isinstance(build_adapter("valheim", ctx(db, http, T0, settings=settings)), ValheimAdapter)
