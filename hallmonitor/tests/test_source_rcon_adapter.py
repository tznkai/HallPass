# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Source RCON adapter for The Ship (AC12.3, the Source mechanism): bans and unbans through the console in the
`source` dialect, and lifts only the bans it placed.

Every test runs against the fake Source server on loopback. No test reaches a real server.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from hallmonitor import own
from hallmonitor.adapters.base import AdapterError, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.registry import build_adapter
from hallmonitor.adapters.source_rcon import SourceRconAdapter
from hallmonitor.adapters.steam import STEAM64_BASE
from hallmonitor.client import MonitorClient
from hallmonitor.config import Config
from hallmonitor.contract import Identity, Subject
from hallmonitor.db import Database
from hallmonitor.reconcile import cycle
from tests.fake_hallpass import TOKEN, FakeHallpassState
from tests.fake_hallpass import make_transport as make_hallpass_transport
from tests.fake_rcon import LOOPBACK, FakeRconState, rcon_server, source_handler

URL = "https://hallpass.invalid"
PASSWORD = "test-pass"
WRONG_PASSWORD = "wrong-pass"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
SLUG = "ship"
# SteamID64s are built at runtime from the base. The Source IDs below are what the conversion gives for them.
OWN_ID = str(STEAM64_BASE + 1001)
OTHER_ID = str(STEAM64_BASE + 1002)
OWN_SID = "STEAM_0:1:500"
OTHER_SID = "STEAM_0:0:501"
SEEN: list[FakeRconState] = []


@pytest.fixture(autouse=True)
def no_removeid_for_an_id_we_did_not_ban() -> Iterator[None]:
    """No removeid may reach a fake server for an id that a banid from this adapter placed and has not yet lifted."""
    SEEN.clear()
    yield
    for state in SEEN:
        placed: set[str] = set()
        for cmd in state.commands:
            verb, _, args = cmd.partition(" ")
            if verb == "banid":
                placed.add(args.split()[1])
            elif verb == "removeid":
                assert args in placed, "the Source adapter must never remove an id it did not ban"
                placed.discard(args)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hallmonitor.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def server_state(**fields: Any) -> FakeRconState:
    state = FakeRconState(**{"dialect": "source", "handler": source_handler, **fields})
    SEEN.append(state)
    return state


def steam(value: str) -> dict[str, str]:
    return {"kind": "steam_id", "value": value}


def subject(ref: str, desired: str, *identities: dict[str, str], generation: int = 1) -> dict[str, Any]:
    return {"ref": ref, "identities": list(identities), "desired": desired, "generation": generation}


def source_service(*subjects: dict[str, Any]) -> FakeHallpassState:
    return FakeHallpassState(services=[{"slug": SLUG, "adapter": "source_rcon", "subjects": list(subjects)}])


def outcomes(hallpass: FakeHallpassState) -> list[tuple[str, str, str | None]]:
    return [(entry["ref"], entry["observed"], entry["detail"]) for entry in hallpass.reports]


def settings_for(port: int, **overrides: str) -> dict[str, str]:
    return {"host": LOOPBACK, "port": str(port), "rcon_password": PASSWORD, **overrides}


def ctx(db: Database, http: httpx.AsyncClient, settings: dict[str, str], at: datetime = NOW) -> AdapterContext:
    return AdapterContext(slug=SLUG, settings=settings, config=Config(hallpass_url=URL, hallpass_token=TOKEN),
                          db=db, http=http, clock=lambda: at)


def adapter_for(db: Database, http: httpx.AsyncClient, port: int, at: datetime = NOW,
                **overrides: str) -> SourceRconAdapter:
    adapter = build_adapter("source_rcon", ctx(db, http, settings_for(port, **overrides), at))
    assert isinstance(adapter, SourceRconAdapter)
    return adapter


async def run(db: Database, http: httpx.AsyncClient, hallpass: FakeHallpassState, port: int,
              at: datetime = NOW, **overrides: str) -> None:
    adapter = adapter_for(db, http, port, at, **overrides)
    client = MonitorClient(URL, TOKEN, transport=make_hallpass_transport(hallpass))
    try:
        await cycle(client=client, adapter_for=lambda s: adapter if s.slug == SLUG else None, now=at)
    finally:
        await client.aclose()


async def record_of(db: Database, identity: str) -> tuple[str, datetime] | None:
    return await db.run(lambda c: own.own_record(c, SLUG, identity))


def closed_port() -> int:
    """A loopback TCP port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def ignore_banid(state: FakeRconState, cmd: str) -> str:
    """A server that answers `banid` with success and never adds the id to its list."""
    if cmd.startswith("banid "):
        return "Banned"
    return source_handler(state, cmd)


# AC12.3: a suspension bans, kicks by id, persists the list, and records the ban as ours ---------------------

async def test_a_suspension_bans_kicks_by_id_persists_the_list_and_records_the_ban(db: Database,
                                                                                   http: httpx.AsyncClient) -> None:
    state = server_state()
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port)
    assert state.source_bans == [OWN_SID]
    assert [cmd for cmd in state.commands if cmd != "listid"] == [f"banid 0 {OWN_SID}", f"kickid {OWN_SID}", "writeid"]
    assert await record_of(db, OWN_ID) == ("ban", NOW)
    assert outcomes(hallpass) == [("r1", "suspended", None)]


async def test_a_suspension_never_sends_banid_with_a_trailing_kick(db: Database, http: httpx.AsyncClient) -> None:
    """The live check showed `banid 0 <steam2> kick` does not kick a player who is connected, so no command has it."""
    state = server_state()
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port)
    assert not any(cmd.endswith(" kick") for cmd in state.commands)


async def test_the_source_id_is_built_from_the_configured_universe(db: Database, http: httpx.AsyncClient) -> None:
    state = server_state()
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port, steamid_universe="1")
    assert state.source_bans == ["STEAM_1:1:500"]
    assert outcomes(hallpass) == [("r1", "suspended", None)]


# AC12.3: a lift removes only that id, persists the list, and forgets the record -------------------------

async def test_lifting_our_own_ban_removes_only_that_id_and_forgets_the_record(db: Database,
                                                                               http: httpx.AsyncClient) -> None:
    state = server_state(source_bans=[OTHER_SID])
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port)
        hallpass.services[0]["subjects"] = [subject("r1", "allowed", steam(OWN_ID), generation=4)]
        await run(db, http, hallpass, port)
    assert state.source_bans == [OTHER_SID]
    assert state.commands[state.commands.index(f"removeid {OWN_SID}") + 1] == "writeid"
    assert await record_of(db, OWN_ID) is None
    assert outcomes(hallpass) == [("r1", "suspended", None), ("r1", "allowed", None)]


async def test_a_steady_suspension_sends_no_second_ban(db: Database, http: httpx.AsyncClient) -> None:
    state = server_state()
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port)
        await run(db, http, hallpass, port)
    assert [cmd for cmd in state.commands if cmd.startswith("banid")] == [f"banid 0 {OWN_SID}"]
    assert outcomes(hallpass) == [("r1", "suspended", None), ("r1", "suspended", None)]


# AC12.3: a ban someone else placed is banned_external and is never lifted -----------------------------

async def test_a_moderator_ban_is_never_lifted_when_the_desired_state_is_allowed(db: Database,
                                                                                 http: httpx.AsyncClient) -> None:
    state = server_state(source_bans=[OTHER_SID])
    hallpass = source_service(subject("r1", "allowed", steam(OTHER_ID), generation=2))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port)
    assert state.source_bans == [OTHER_SID]
    assert not any(cmd.startswith(("removeid", "banid")) for cmd in state.commands)
    assert outcomes(hallpass) == [("r1", "banned_external", None)]


async def test_a_moderator_ban_is_left_alone_when_the_desired_state_is_suspended(db: Database,
                                                                                 http: httpx.AsyncClient) -> None:
    state = server_state(source_bans=[OTHER_SID])
    hallpass = source_service(subject("r1", "suspended", steam(OTHER_ID), generation=2))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port)
    assert not any(cmd.startswith("banid") for cmd in state.commands)
    assert outcomes(hallpass) == [("r1", "banned_external", None)]
    assert await record_of(db, OTHER_ID) is None


async def test_a_ban_placed_by_a_moderator_after_observe_is_never_taken_over(db: Database,
                                                                             http: httpx.AsyncClient) -> None:
    """The apply re-reads the list, so a ban that appears between observe and apply is never treated as ours."""
    state = server_state()
    subj = Subject("r1", (Identity("steam_id", OWN_ID),), "suspended", 2)
    async with rcon_server(state) as port:
        adapter = adapter_for(db, http, port)
        await adapter.observe([subj])
        state.source_bans.append(OWN_SID)
        result = await adapter.apply(subj, "suspended")
    assert result == Observed("banned_external")
    assert not any(cmd.startswith("banid") for cmd in state.commands)
    assert await record_of(db, OWN_ID) is None


async def test_an_own_record_whose_id_a_moderator_unlisted_is_forgotten_at_once_and_a_later_re_add_is_banned_external(
        db: Database, http: httpx.AsyncClient) -> None:
    """The forget step: our record outlives the id's removal from the list, made outside the adapter. The record is
    forgotten at the next observe with no banid, removeid or writeid, and a later re-add is banned_external and kept."""
    state = server_state()
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port)
        state.source_bans.remove(OWN_SID)                      # a moderator's unban, made through the console
        hallpass.services[0]["subjects"] = [subject("r1", "allowed", steam(OWN_ID), generation=4)]
        before = len(state.commands)
        await run(db, http, hallpass, port)
        assert await record_of(db, OWN_ID) is None
        assert outcomes(hallpass)[-1] == ("r1", "allowed", None)
        assert not any(cmd.startswith(("banid", "removeid", "writeid")) for cmd in state.commands[before:])

        state.source_bans.append(OWN_SID)                     # the moderator re-adds the id
        hallpass.services[0]["subjects"] = [subject("r1", "allowed", steam(OWN_ID), generation=5)]
        before = len(state.commands)
        await run(db, http, hallpass, port, at=NOW + timedelta(seconds=1))
    assert outcomes(hallpass)[-1] == ("r1", "banned_external", None)
    assert state.source_bans == [OWN_SID]
    assert not any(cmd.startswith(("banid", "removeid", "writeid")) for cmd in state.commands[before:])


async def test_a_moderator_ban_on_our_subject_is_not_lifted_by_a_restore(db: Database, http: httpx.AsyncClient) -> None:
    state = server_state(source_bans=[OWN_SID])
    subj = Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 2)
    async with rcon_server(state) as port:
        result = await adapter_for(db, http, port).apply(subj, "allowed")
    assert result == Observed("banned_external")
    assert state.source_bans == [OWN_SID]
    assert not any(cmd.startswith("removeid") for cmd in state.commands)


# Restore with no own record: the console list is read again, and a lifted ban is reported allowed ---------

async def test_a_restore_after_the_shared_ban_is_lifted_reads_the_list_and_sends_nothing(db: Database,
                                                                                         http: httpx.AsyncClient) -> None:
    """Two subjects share one id. The first restore lifts our ban; the second finds no record, reads `listid`, and
    reports allowed with no removeid or writeid."""
    state = server_state()
    first = Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 2)
    second = Subject("r2", (Identity("steam_id", OWN_ID),), "allowed", 2)
    async with rcon_server(state) as port:
        adapter = adapter_for(db, http, port)
        assert await adapter.apply(first, "suspended") == Observed("suspended")
        assert await adapter.apply(first, "allowed") == Observed("allowed")
        sent = len(state.commands)
        assert await adapter.apply(second, "allowed") == Observed("allowed")
    assert state.source_bans == []
    assert state.commands[sent:] == ["listid"]


async def test_a_restore_with_no_own_record_and_no_listed_ban_is_allowed_and_changes_nothing(db: Database,
                                                                                            http: httpx.AsyncClient) -> None:
    state = server_state(source_bans=[OTHER_SID])
    subj = Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 2)
    async with rcon_server(state) as port:
        result = await adapter_for(db, http, port).apply(subj, "allowed")
    assert result == Observed("allowed")
    assert state.source_bans == [OTHER_SID]
    assert not any(cmd.startswith(("banid", "removeid", "writeid")) for cmd in state.commands)


# AC12.3: a server that does not confirm the ban is not_applied, and is never recorded as a suspension ----

async def test_a_server_that_does_not_confirm_the_ban_is_not_applied_and_not_recorded(db: Database,
                                                                                      http: httpx.AsyncClient) -> None:
    state = server_state(handler=ignore_banid)
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port)
    assert state.source_bans == []
    assert f"banid 0 {OWN_SID}" in state.commands
    assert await record_of(db, OWN_ID) is None
    assert outcomes(hallpass) == [("r1", "error", "not_applied")]


# Identity: a missing or malformed steam_id is refused before any console command is sent -----------------

@pytest.mark.parametrize("value", ["12a", "1 2", "1\nremoveid 1", "", str(STEAM64_BASE - 2),
                                   "0" + OWN_ID, str(STEAM64_BASE + (1 << 32))])
async def test_a_malformed_steam_id_is_refused_before_any_command(db: Database, http: httpx.AsyncClient,
                                                                  value: str) -> None:
    state = server_state()
    async with rcon_server(state) as port:
        adapter = adapter_for(db, http, port)
        for desired in ("suspended", "allowed"):
            result = await adapter.apply(Subject("r1", (Identity("steam_id", value),), desired, 2), desired)
            assert result == Observed("error", "bad_identity")
    assert state.commands == []


async def test_a_subject_without_a_steam_id_is_no_identity_before_any_command(db: Database,
                                                                              http: httpx.AsyncClient) -> None:
    state = server_state()
    async with rcon_server(state) as port:
        result = await adapter_for(db, http, port).apply(Subject("r1", (), "suspended", 2), "suspended")
    assert result == Observed("error", "no_identity")
    assert state.commands == []


# Errors: auth and unreachable are service-wide, and the reason is reported for each subject --------------

async def test_a_wrong_rcon_password_reports_auth_for_every_subject(db: Database, http: httpx.AsyncClient) -> None:
    state = server_state(password=PASSWORD)
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID)), subject("r2", "allowed", steam(OTHER_ID)))
    async with rcon_server(state) as port:
        await run(db, http, hallpass, port, rcon_password=WRONG_PASSWORD)
    assert outcomes(hallpass) == [("r1", "error", "auth"), ("r2", "error", "auth")]
    assert state.commands == []


async def test_a_closed_rcon_port_reports_unreachable(db: Database, http: httpx.AsyncClient) -> None:
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, http, hallpass, closed_port())
    assert outcomes(hallpass) == [("r1", "error", "unreachable")]


# Logs: an event names only what happened, never a steam id, a password or command text ------------------

async def test_logs_never_carry_a_steam_id_a_password_or_a_command(db: Database, http: httpx.AsyncClient,
                                                                    caplog: pytest.LogCaptureFixture) -> None:
    state = server_state(handler=ignore_banid)
    hallpass = source_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    with caplog.at_level(logging.DEBUG):
        async with rcon_server(state) as port:
            await run(db, http, hallpass, port)
    assert outcomes(hallpass) == [("r1", "error", "not_applied")]
    for secret in (OWN_ID, OWN_SID, PASSWORD, "banid"):
        assert secret not in caplog.text


# Plugin creation: a missing setting or a bad universe digit is not configured --------------------------

@pytest.mark.parametrize("universe", ["6", "10", "", "x"])
async def test_a_steamid_universe_that_is_not_one_digit_from_0_to_5_is_not_configured(
        db: Database, http: httpx.AsyncClient, universe: str) -> None:
    settings = settings_for(27015, steamid_universe=universe)
    assert build_adapter("source_rcon", ctx(db, http, settings)) is None


async def test_a_missing_host_is_not_configured(db: Database, http: httpx.AsyncClient) -> None:
    settings = {"rcon_password": PASSWORD}
    assert build_adapter("source_rcon", ctx(db, http, settings)) is None


# Unmanaged: a listed id that matches no subject is counted, and a listed subject is not -----------------

async def test_unmanaged_counts_listed_ids_that_match_no_subject(db: Database, http: httpx.AsyncClient) -> None:
    state = server_state(source_bans=[OTHER_SID])
    own_subject = Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 1)
    other_subject = Subject("r2", (Identity("steam_id", OTHER_ID),), "allowed", 1)
    async with rcon_server(state) as port:
        adapter = adapter_for(db, http, port)
        assert await adapter.unmanaged_count([own_subject]) == 1
        assert await adapter.unmanaged_count([other_subject]) == 0


async def test_a_restore_with_no_own_record_and_a_wrong_password_raises_auth_and_sends_nothing(
        db: Database, http: httpx.AsyncClient) -> None:
    state = server_state(password=PASSWORD)
    subj = Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 2)
    async with rcon_server(state) as port:
        with pytest.raises(AdapterError) as info:
            await adapter_for(db, http, port, rcon_password=WRONG_PASSWORD).apply(subj, "allowed")
    assert info.value.reason == "auth"
    assert state.commands == []


async def test_a_restore_with_no_own_record_and_an_unreachable_server_raises_unreachable(
        db: Database, http: httpx.AsyncClient) -> None:
    subj = Subject("r1", (Identity("steam_id", OWN_ID),), "allowed", 2)
    with pytest.raises(AdapterError) as info:
        await adapter_for(db, http, closed_port()).apply(subj, "allowed")
    assert info.value.reason == "unreachable"
