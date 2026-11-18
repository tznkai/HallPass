# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Source panel adapter for The Ship: bans and unbans through the Pterodactyl console, and reads the ban list back
from banned_user.cfg through Pterodactyl. It lifts only the bans it placed.

Every test runs against the fake console and the fake Pterodactyl routes, served through an httpx MockTransport. No
test reaches a real server.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from hallmonitor import own
from hallmonitor.adapters.base import AdapterError, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.registry import build_adapter
from hallmonitor.adapters.source_panel import SourcePanelAdapter
from hallmonitor.adapters.steam import STEAM64_BASE, steam2
from hallmonitor.client import MonitorClient
from hallmonitor.config import Config
from hallmonitor.contract import Identity, Subject
from hallmonitor.db import Database
from hallmonitor.reconcile import cycle
from tests.fake_hallpass import TOKEN, FakeHallpassState
from tests.fake_hallpass import make_transport as make_hallpass_transport
from tests.fake_ptero_files import PTERO_KEY, FakePteroState, make_transport
from tests.fake_source_console import SourceConsole, file_text

URL = "https://hallpass.invalid"
PTERO_URL = "https://ptero.invalid"
SERVER = "srv1abcd"
BANLIST = "fake/cfg/banned_user.cfg"
WRONG_KEY = "not-the-key"
SLUG = "ship"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
OWN_BAN = "ban"
STEAM_KIND = "steam_id"
# SteamID64s are built at runtime from the base. The Source IDs below are what the conversion gives for them.
OWN_ID = str(STEAM64_BASE + 1001)
OTHER_ID = str(STEAM64_BASE + 1002)
THIRD_ID = str(STEAM64_BASE + 1003)
OWN_SID = steam2(OWN_ID, "0")
OTHER_SID = steam2(OTHER_ID, "0")
THIRD_SID = steam2(THIRD_ID, "0")
UNIVERSE_ONE_SID = steam2(OWN_ID, "1")
SETTINGS = {"ptero_url": PTERO_URL, "ptero_key": PTERO_KEY, "ptero_server": SERVER, "banlist_path": BANLIST}


@dataclass
class Clock:
    at: datetime

    def __call__(self) -> datetime:
        return self.at


@dataclass
class Lab:
    db: Database
    db_path: Path
    state: FakePteroState
    console: SourceConsole
    adapter: SourcePanelAdapter
    clock: Clock


def settings_for(**overrides: str) -> dict[str, str]:
    return {**SETTINGS, **overrides}


def ctx(db: Database, http: httpx.AsyncClient, settings: dict[str, str], clock: Clock) -> AdapterContext:
    return AdapterContext(slug=SLUG, settings=settings, config=Config(hallpass_url=URL, hallpass_token=TOKEN),
                          db=db, http=http, clock=clock)


@asynccontextmanager
async def lab_with(tmp_path: Path, *listed: str, keep_on_removeid: bool = False, ignore_banid: bool = False,
                   **overrides: str) -> AsyncIterator[Lab]:
    """A running fake console whose ban file already shows `listed`, and an adapter built from the settings."""
    console = SourceConsole(server=SERVER, banlist_path=BANLIST, listed=list(listed),
                            keep_on_removeid=keep_on_removeid, ignore_banid=ignore_banid)
    state = FakePteroState(files={(SERVER, BANLIST): file_text(listed)}, console=console.run)
    clock = Clock(NOW)
    db_path = tmp_path / "hallmonitor.db"
    database = Database.open(db_path)
    try:
        async with httpx.AsyncClient(transport=make_transport(state)) as http:
            adapter = build_adapter("source_panel", ctx(database, http, settings_for(**overrides), clock))
            assert isinstance(adapter, SourcePanelAdapter)
            yield Lab(db=database, db_path=db_path, state=state, console=console, adapter=adapter, clock=clock)
    finally:
        database.close()


def steam_subject(ref: str, steam_id: str, desired: str) -> Subject:
    return Subject(ref, (Identity(STEAM_KIND, steam_id),), desired, 2)


async def place_record(lab: Lab, steam_id: str, at: datetime) -> None:
    await lab.db.run(lambda c: own.record_own(c, SLUG, steam_id, OWN_BAN, at))


async def record_of(lab: Lab, steam_id: str) -> tuple[str, datetime] | None:
    return await lab.db.run(lambda c: own.own_record(c, SLUG, steam_id))


def record_on_disk(path: Path, steam_id: str) -> str | None:
    """The own reference as another connection sees it right now, so a test can tell what was committed when."""
    with closing(sqlite3.connect(path)) as conn:
        return own.own_ref(conn, SLUG, steam_id)


def outcomes(hallpass: FakeHallpassState) -> list[tuple[str, str, str | None]]:
    return [(entry["ref"], entry["observed"], entry["detail"]) for entry in hallpass.reports]


def service(*subjects: dict[str, Any]) -> FakeHallpassState:
    return FakeHallpassState(services=[{"slug": SLUG, "adapter": "source_panel", "subjects": list(subjects)}])


def hallpass_subject(ref: str, desired: str, steam_id: str, generation: int = 1) -> dict[str, Any]:
    return {"ref": ref, "identities": [{"kind": STEAM_KIND, "value": steam_id}], "desired": desired,
            "generation": generation}


async def run_cycle(lab: Lab, hallpass: FakeHallpassState) -> None:
    client = MonitorClient(URL, TOKEN, transport=make_hallpass_transport(hallpass))
    try:
        await cycle(client=client, adapter_for=lambda s: lab.adapter if s.slug == SLUG else None,
                    now=lab.clock.at)
    finally:
        await client.aclose()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "plain.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


# Observe: every branch, on both sides of the grace window --------------------------------------------------

async def test_a_listed_id_with_our_record_is_suspended(tmp_path: Path) -> None:
    async with lab_with(tmp_path, OWN_SID) as lab:
        await place_record(lab, OWN_ID, NOW)
        observed = await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
    assert observed == {"r1": Observed("suspended")}


async def test_a_listed_id_with_no_record_is_banned_external(tmp_path: Path) -> None:
    async with lab_with(tmp_path, OTHER_SID) as lab:
        observed = await lab.adapter.observe([steam_subject("r1", OTHER_ID, "allowed")])
    assert observed == {"r1": Observed("banned_external")}


async def test_an_unlisted_id_with_no_record_is_allowed(tmp_path: Path) -> None:
    async with lab_with(tmp_path) as lab:
        observed = await lab.adapter.observe([steam_subject("r1", OWN_ID, "allowed")])
    assert observed == {"r1": Observed("allowed")}


@pytest.mark.parametrize(("elapsed_seconds", "expected"), [(0, "suspended"), (599, "suspended"), (600, "suspended"),
                                                           (602, "allowed")])
async def test_an_unlisted_id_with_our_record_is_suspended_only_inside_the_grace_window(
        tmp_path: Path, elapsed_seconds: int, expected: str) -> None:
    async with lab_with(tmp_path) as lab:
        await place_record(lab, OWN_ID, NOW)
        lab.clock.at = NOW + timedelta(seconds=elapsed_seconds)
        observed = await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
        forgotten = await record_of(lab, OWN_ID) is None
    assert observed == {"r1": Observed(expected)}
    assert forgotten == (expected == "allowed")
    assert lab.state.commands == []


async def test_a_grace_window_set_in_settings_is_the_one_used(tmp_path: Path) -> None:
    async with lab_with(tmp_path, forget_grace_seconds="60") as lab:
        await place_record(lab, OWN_ID, NOW)
        lab.clock.at = NOW + timedelta(seconds=59)
        inside = await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
        lab.clock.at = NOW + timedelta(seconds=62)
        outside = await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
    assert inside == {"r1": Observed("suspended")}
    assert outside == {"r1": Observed("allowed")}


async def test_a_wrong_key_fails_the_whole_observe_with_auth(tmp_path: Path) -> None:
    async with lab_with(tmp_path, OWN_SID, ptero_key=WRONG_KEY) as lab:
        with pytest.raises(AdapterError) as info:
            await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
    assert info.value.reason == "auth"


@pytest.mark.parametrize(("status", "reason"), [(404, "not_found"), (500, "http")])
async def test_a_failed_file_read_fails_the_whole_observe(tmp_path: Path, status: int, reason: str) -> None:
    async with lab_with(tmp_path, OWN_SID) as lab:
        lab.state.fail_next = {"contents": status}
        with pytest.raises(AdapterError) as info:
            await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
    assert info.value.reason == reason


# Line parsing: CRLF, BOM, spacing, timed lines, junk and other universes ------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        f"\ufeffbanid 0 {OWN_SID}\r\nbanid 0 {OTHER_SID}\r\n",          # a BOM and CRLF endings
        f"banid\t0   {OWN_SID}  \n",                                    # runs of spaces and tabs, and padding
    ],
    ids=["bom-and-crlf", "spaces-and-tabs"],
)
async def test_a_ban_line_in_any_tolerated_form_is_a_listed_ban(tmp_path: Path, text: str) -> None:
    async with lab_with(tmp_path) as lab:
        lab.state.files[(SERVER, BANLIST)] = text
        observed = await lab.adapter.observe([steam_subject("r1", OWN_ID, "allowed")])
    assert observed == {"r1": Observed("banned_external")}


@pytest.mark.parametrize(
    "text",
    [
        f"banid 5 {OWN_SID}\n",                             # a timed ban is not a ban line
        f"banid 0 {OWN_SID} kick\n",                       # trailing text is not a ban line
        f"# banid 0 {OWN_SID}\n",                          # a comment
        "junk\n\nremoveid 0\nbanid 0 STEAM_0:2:500\n",     # junk, a blank line, and an out-of-range y digit
        f"banid 0 {UNIVERSE_ONE_SID}\n",                   # another universe is a different id
    ],
    ids=["timed", "trailing-text", "comment", "junk", "other-universe"],
)
async def test_a_line_that_is_not_a_ban_line_is_ignored(tmp_path: Path, text: str) -> None:
    async with lab_with(tmp_path) as lab:
        lab.state.files[(SERVER, BANLIST)] = text
        observed = await lab.adapter.observe([steam_subject("r1", OWN_ID, "allowed")])
    assert observed == {"r1": Observed("allowed")}


# Suspend: three commands in order, recorded first, and read back ---------------------------------------------

async def test_a_suspension_sends_banid_kickid_and_writeid_in_order_and_writes_no_file_itself(tmp_path: Path) -> None:
    async with lab_with(tmp_path) as lab:
        result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
    assert result == Observed("suspended")
    assert lab.state.commands == [f"banid 0 {OWN_SID}", f"kickid {OWN_SID}", "writeid"]
    assert not any(method == "POST" and path.endswith("/files/write") for method, path in lab.state.requests)


async def test_a_suspension_records_the_ban_before_the_first_command(tmp_path: Path) -> None:
    seen: list[tuple[str, str | None]] = []
    async with lab_with(tmp_path) as lab:
        inner = lab.state.console

        def watch(state: FakePteroState, text: str) -> None:
            seen.append((text, record_on_disk(lab.db_path, OWN_ID)))
            inner(state, text)

        lab.state.console = watch
        await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
    assert seen[0] == (f"banid 0 {OWN_SID}", OWN_BAN)


async def test_a_suspension_reads_back_the_file_and_the_record_is_ours(tmp_path: Path) -> None:
    async with lab_with(tmp_path) as lab:
        await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
        assert lab.state.files[(SERVER, BANLIST)] == f"banid 0 {OWN_SID}\n"
        assert await record_of(lab, OWN_ID) == (OWN_BAN, NOW)


async def test_an_id_the_list_already_shows_is_not_banned_again(tmp_path: Path) -> None:
    async with lab_with(tmp_path, OWN_SID) as lab:
        await place_record(lab, OWN_ID, NOW)
        result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
    assert result == Observed("suspended")
    assert lab.state.commands == []


async def test_an_id_a_moderator_already_listed_is_banned_external_and_never_taken_over(tmp_path: Path) -> None:
    async with lab_with(tmp_path, OWN_SID) as lab:
        result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
        assert await record_of(lab, OWN_ID) is None
    assert result == Observed("banned_external")
    assert lab.state.commands == []


@pytest.mark.parametrize(("status", "reason"),
                         [(409, "unreachable"), (502, "unreachable"), (401, "auth"), (404, "not_found"),
                          (500, "http")])
async def test_a_refused_banid_forgets_the_record_and_raises_the_reason(tmp_path: Path, status: int,
                                                                       reason: str) -> None:
    async with lab_with(tmp_path) as lab:
        lab.state.fail_next = {"command": status}
        with pytest.raises(AdapterError) as info:
            await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
        assert info.value.reason == reason
        assert await record_of(lab, OWN_ID) is None
        assert lab.console.listed == []
        assert lab.state.commands == []


# Suspend: once banid has been accepted, the ban is ours and its record stays, whatever fails after it ----------------

@pytest.mark.parametrize("refused", ["kickid", "writeid"])
async def test_a_refused_command_after_banid_keeps_the_record_and_raises(tmp_path: Path, refused: str) -> None:
    async with lab_with(tmp_path) as lab:
        lab.state.fail_commands = {refused: 500}
        with pytest.raises(AdapterError) as info:
            await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
        assert info.value.reason == "http"
        assert await record_of(lab, OWN_ID) == (OWN_BAN, NOW)
        assert lab.console.listed == [OWN_SID]   # the console holds the ban hallmonitor placed
        assert lab.state.commands[0] == f"banid 0 {OWN_SID}"
        assert lab.state.commands.count(f"banid 0 {OWN_SID}") == 1


async def test_a_refused_kickid_then_a_following_observe_reads_it_as_suspended(tmp_path: Path) -> None:
    async with lab_with(tmp_path) as lab:
        lab.state.fail_commands = {"kickid": 500}
        with pytest.raises(AdapterError):
            await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
        observed = await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
    assert observed == {"r1": Observed("suspended")}


async def test_a_refused_writeid_then_a_following_observe_reads_it_as_suspended(tmp_path: Path) -> None:
    async with lab_with(tmp_path) as lab:
        lab.state.fail_commands = {"writeid": 500}
        with pytest.raises(AdapterError):
            await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
        observed = await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
    assert observed == {"r1": Observed("suspended")}


@pytest.mark.parametrize(("refused", "sent_before"),
                         [("kickid", [f"banid 0 {OWN_SID}"]), ("writeid", [f"banid 0 {OWN_SID}", f"kickid {OWN_SID}"])])
async def test_a_restore_after_a_refused_command_sends_removeid_and_writeid(tmp_path: Path, refused: str,
                                                                           sent_before: list[str]) -> None:
    async with lab_with(tmp_path) as lab:
        lab.state.fail_commands = {refused: 500}
        with pytest.raises(AdapterError):
            await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
        result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "allowed"), "allowed")
        assert await record_of(lab, OWN_ID) is None
    assert result == Observed("allowed")
    assert lab.state.commands == [*sent_before, f"removeid {OWN_SID}", "writeid"]
    assert lab.state.files[(SERVER, BANLIST)] == ""


# Suspend: the server accepts banid but its list never shows the id, so the record carries the grace window ------------

async def test_a_banid_the_list_never_shows_stays_suspended_inside_the_grace_window_then_is_forgotten(
        tmp_path: Path) -> None:
    async with lab_with(tmp_path, ignore_banid=True) as lab:
        result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended")
        assert result == Observed("suspended")
        assert await record_of(lab, OWN_ID) == (OWN_BAN, NOW)
        assert lab.state.commands == [f"banid 0 {OWN_SID}", f"kickid {OWN_SID}", "writeid"]
        assert lab.state.files[(SERVER, BANLIST)] == ""
        inside = await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
        assert inside == {"r1": Observed("suspended")}
        assert await record_of(lab, OWN_ID) == (OWN_BAN, NOW)
        lab.clock.at = NOW + timedelta(seconds=602)
        outside = await lab.adapter.observe([steam_subject("r1", OWN_ID, "suspended")])
        assert outside == {"r1": Observed("allowed")}
        assert await record_of(lab, OWN_ID) is None


# Restore: only our own bans are lifted ------------------------------------------------------------------------

async def test_a_restore_with_our_record_removes_the_id_writes_and_forgets(tmp_path: Path) -> None:
    async with lab_with(tmp_path, OWN_SID, OTHER_SID) as lab:
        await place_record(lab, OWN_ID, NOW)
        result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "allowed"), "allowed")
        assert await record_of(lab, OWN_ID) is None
    assert result == Observed("allowed")
    assert lab.state.commands == [f"removeid {OWN_SID}", "writeid"]
    assert lab.state.files[(SERVER, BANLIST)] == f"banid 0 {OTHER_SID}\n"


async def test_a_restore_with_our_record_for_an_id_already_gone_forgets_it(tmp_path: Path) -> None:
    async with lab_with(tmp_path) as lab:
        await place_record(lab, OWN_ID, NOW)
        result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "allowed"), "allowed")
        assert await record_of(lab, OWN_ID) is None
    assert result == Observed("allowed")


async def test_a_restore_the_list_still_shows_is_not_applied_and_keeps_the_record(tmp_path: Path) -> None:
    async with lab_with(tmp_path, OWN_SID, keep_on_removeid=True) as lab:
        await place_record(lab, OWN_ID, NOW)
        result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "allowed"), "allowed")
        assert await record_of(lab, OWN_ID) == (OWN_BAN, NOW)
    assert result == Observed("error", "not_applied")


async def test_a_moderator_ban_is_never_lifted_by_a_restore(tmp_path: Path) -> None:
    async with lab_with(tmp_path, OTHER_SID) as lab:
        result = await lab.adapter.apply(steam_subject("r1", OTHER_ID, "allowed"), "allowed")
    assert result == Observed("banned_external")
    assert lab.state.commands == []
    assert lab.state.files[(SERVER, BANLIST)] == f"banid 0 {OTHER_SID}\n"


async def test_a_restore_after_the_shared_ban_is_lifted_reads_the_file_and_sends_nothing(tmp_path: Path) -> None:
    """Two subjects share one id. The first restore lifts our ban; the second finds no record, reads the file, and
    reports allowed with no removeid or writeid."""
    async with lab_with(tmp_path) as lab:
        assert await lab.adapter.apply(steam_subject("r1", OWN_ID, "suspended"), "suspended") == Observed("suspended")
        assert await lab.adapter.apply(steam_subject("r1", OWN_ID, "allowed"), "allowed") == Observed("allowed")
        sent = len(lab.state.commands)
        assert await lab.adapter.apply(steam_subject("r2", OWN_ID, "allowed"), "allowed") == Observed("allowed")
    assert not any(cmd.startswith(("banid", "removeid", "writeid")) for cmd in lab.state.commands[sent:])
    assert OWN_SID not in lab.state.files[(SERVER, BANLIST)]


async def test_a_restore_with_no_own_record_and_no_listed_ban_is_allowed_and_sends_nothing(tmp_path: Path) -> None:
    async with lab_with(tmp_path, OTHER_SID) as lab:
        result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "allowed"), "allowed")
    assert result == Observed("allowed")
    assert lab.state.commands == []
    assert lab.state.files[(SERVER, BANLIST)] == f"banid 0 {OTHER_SID}\n"


# Identity: a malformed or missing steam_id is refused before any command or file line --------------------------

@pytest.mark.parametrize("value", ["12a", "1 2", "1\nremoveid 1", "", str(STEAM64_BASE - 2),
                                   "0" + OWN_ID, str(STEAM64_BASE + (1 << 32))])
async def test_a_malformed_steam_id_is_bad_identity_before_any_command(tmp_path: Path, value: str) -> None:
    async with lab_with(tmp_path) as lab:
        for desired in ("suspended", "allowed"):
            result = await lab.adapter.apply(steam_subject("r1", value, desired), desired)
            assert result == Observed("error", "bad_identity")
        observed = await lab.adapter.observe([steam_subject("r1", value, "suspended")])
    assert observed == {"r1": Observed("error", "bad_identity")}
    assert lab.state.commands == []


async def test_a_subject_without_a_steam_id_is_no_identity_before_any_command(tmp_path: Path) -> None:
    async with lab_with(tmp_path) as lab:
        result = await lab.adapter.apply(Subject("r1", (), "suspended", 2), "suspended")
    assert result == Observed("error", "no_identity")
    assert lab.state.commands == []


# Unmanaged: listed ids that match no subject are counted, and listed subjects are not ----------------------

async def test_unmanaged_counts_listed_bans_that_match_no_subject(tmp_path: Path) -> None:
    text = f"\ufeffbanid 0 {OWN_SID}\r\nbanid 0 {OTHER_SID}\r\nbanid 5 {THIRD_SID}\r\njunk\r\n"
    async with lab_with(tmp_path) as lab:
        lab.state.files[(SERVER, BANLIST)] = text
        assert await lab.adapter.unmanaged_count([steam_subject("r1", OWN_ID, "allowed")]) == 1
        assert await lab.adapter.unmanaged_count([]) == 2


# Settings: each missing or invalid setting means not configured ---------------------------------------------

@pytest.mark.parametrize("missing", ["ptero_url", "ptero_key", "ptero_server", "banlist_path"])
async def test_a_missing_required_setting_is_not_configured(db: Database, http: httpx.AsyncClient,
                                                            missing: str) -> None:
    settings = {key: value for key, value in SETTINGS.items() if key != missing}
    clock = Clock(NOW)
    assert build_adapter("source_panel", ctx(db, http, settings, clock)) is None


@pytest.mark.parametrize("universe", ["6", "10", "x", "-1"])
async def test_a_steamid_universe_that_is_not_one_digit_from_0_to_5_is_not_configured(
        db: Database, http: httpx.AsyncClient, universe: str) -> None:
    settings = settings_for(steamid_universe=universe)
    assert build_adapter("source_panel", ctx(db, http, settings, Clock(NOW))) is None


@pytest.mark.parametrize("grace", ["0", "-5", "x", "1234567890", "60s"])
async def test_a_grace_setting_that_is_not_a_positive_whole_number_is_not_configured(
        db: Database, http: httpx.AsyncClient, grace: str) -> None:
    settings = settings_for(forget_grace_seconds=grace)
    assert build_adapter("source_panel", ctx(db, http, settings, Clock(NOW))) is None


async def test_the_default_settings_build_the_adapter(db: Database, http: httpx.AsyncClient) -> None:
    adapter = build_adapter("source_panel", ctx(db, http, settings_for(), Clock(NOW)))
    assert isinstance(adapter, SourcePanelAdapter)
    assert adapter.capabilities.suspend and adapter.capabilities.restore
    assert adapter.capabilities.read_state and adapter.capabilities.removes_connected


# Logs: an event names what happened, never an id, a path, a host or a command -----------------------------------

async def test_logs_never_carry_an_id_a_path_a_key_or_a_command(tmp_path: Path,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="hallmonitor"):   # the HTTP client's own request log is out of scope
        async with lab_with(tmp_path, OWN_SID, keep_on_removeid=True) as lab:
            await place_record(lab, OWN_ID, NOW)
            result = await lab.adapter.apply(steam_subject("r1", OWN_ID, "allowed"), "allowed")
    assert result == Observed("error", "not_applied")
    for secret in (OWN_ID, OWN_SID, PTERO_KEY, BANLIST, PTERO_URL, "removeid", "banid", "writeid"):
        assert secret not in caplog.text


# Through the reconcile loop -----------------------------------------------------------------------------------

async def test_a_cycle_suspends_through_the_console_and_reports_suspended(tmp_path: Path) -> None:
    hallpass = service(hallpass_subject("r1", "suspended", OWN_ID, generation=3))
    async with lab_with(tmp_path) as lab:
        await run_cycle(lab, hallpass)
        assert lab.state.commands == [f"banid 0 {OWN_SID}", f"kickid {OWN_SID}", "writeid"]
    assert outcomes(hallpass) == [("r1", "suspended", None)]
