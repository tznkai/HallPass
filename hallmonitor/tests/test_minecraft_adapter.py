# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Minecraft whitelist adapter over RCON (AC11.1 to AC11.5, and AC9.10 for Minecraft).

Every test runs against the fake RCON server on loopback. No test sends a pardon, and no test reaches a real server.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from hallmonitor.adapters.base import AdapterError, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.minecraft import BEDROCK_KIND, JAVA_KIND, MinecraftAdapter, _parse_whitelist
from hallmonitor.adapters.registry import build_adapter
from hallmonitor.client import MonitorClient
from hallmonitor.config import Config
from hallmonitor.contract import Identity, Subject
from hallmonitor.db import Database
from hallmonitor.logs import JsonFormatter
from hallmonitor.reconcile import cycle
from tests.fake_hallpass import TOKEN, FakeHallpassState
from tests.fake_hallpass import make_transport as make_hallpass_transport
from tests.fake_rcon import LOOPBACK, FakeRconState, default_minecraft_handler, rcon_server

URL = "https://hallpass.invalid"
PASSWORD = "test-pass"
WRONG_PASSWORD = "wrong-pass"
KICK = "Access paused. Ask a parent or the administrator."
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
# What a cycle sends when nothing is applied: observe reads the list and the ban list, then the unmanaged count reads
# the list again on its own connection.
LIST_COMMANDS = ["whitelist list", "banlist players", "whitelist list"]
SEEN: list[FakeRconState] = []


@pytest.fixture(autouse=True)
def no_pardon_ever() -> Iterator[None]:
    """No state served in this module may receive a pardon: the adapter only reads the ban list."""
    SEEN.clear()
    yield
    for state in SEEN:
        pardons = [cmd for cmd in state.commands if cmd.lower().startswith("pardon")]
        assert pardons == [], "the Minecraft adapter must never send pardon"


def server_state(**fields: Any) -> FakeRconState:
    state = FakeRconState(**fields)
    SEEN.append(state)
    return state


def java_identity(name: str) -> dict[str, str]:
    return {"kind": "java_name", "value": name}


def bedrock_identity(tag: str) -> dict[str, str]:
    return {"kind": "bedrock_gamertag", "value": tag}


def ban(name: str, by: str = "Admin", reason: str = "griefing") -> tuple[str, str, str]:
    """One vanilla ban entry: `NAME was banned by BANNER: REASON`."""
    return (name, by, reason)


def subject(ref: str, desired: str, *identities: dict[str, str], generation: int = 1) -> dict[str, Any]:
    return {"ref": ref, "identities": list(identities), "desired": desired, "generation": generation}


def mc_service(*subjects: dict[str, Any]) -> FakeHallpassState:
    return FakeHallpassState(services=[{"slug": "mc", "adapter": "minecraft", "subjects": list(subjects)}])


def make_adapter(port: int, password: str = PASSWORD) -> MinecraftAdapter:
    return MinecraftAdapter(host=LOOPBACK, port=port, password=password, floodgate_prefix=".", kick_message=KICK)


async def run(hallpass: FakeHallpassState, port: int, password: str = PASSWORD) -> None:
    adapter = make_adapter(port, password)
    client = MonitorClient(URL, TOKEN, transport=make_hallpass_transport(hallpass))
    try:
        await cycle(client=client, adapter_for=lambda s: adapter if s.slug == "mc" else None, now=NOW)
    finally:
        await client.aclose()


def outcomes(hallpass: FakeHallpassState) -> list[tuple[str, str, str | None]]:
    return [(entry["ref"], entry["observed"], entry["detail"]) for entry in hallpass.reports]


def closed_port() -> int:
    """A loopback TCP port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def ignore_whitelist_remove(state: FakeRconState, cmd: str) -> str:
    """A server that answers `whitelist remove` with success and leaves the list unchanged."""
    if cmd.startswith("whitelist remove"):
        return "Removed the player"
    return default_minecraft_handler(state, cmd)


# AC11.1: an entitled Java player and an entitled Bedrock player are both whitelisted ------------------

async def test_an_entitled_java_and_bedrock_player_are_both_added_to_the_whitelist() -> None:
    state = server_state()
    hallpass = mc_service(subject("r1", "allowed", java_identity("Steve"), generation=3),
                          subject("r2", "allowed", bedrock_identity("Tim"), generation=4))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == ["Steve", ".Tim"]
    assert "fwhitelist add Tim" in state.commands
    assert outcomes(hallpass) == [("r1", "allowed", None), ("r2", "allowed", None)]


# AC11.2: a hold removes the player from the whitelist, kicks them, and reports suspended ---------------

async def test_a_hold_removes_a_java_player_and_kicks_them() -> None:
    state = server_state(whitelist=["Steve"], online={"Steve"})
    hallpass = mc_service(subject("r1", "suspended", java_identity("Steve"), generation=5))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == []
    assert f"kick Steve {KICK}" in state.commands
    assert "Steve" not in state.online
    assert outcomes(hallpass) == [("r1", "suspended", None)]


async def test_a_hold_removes_a_bedrock_player_through_floodgate_and_kicks_the_prefixed_name() -> None:
    state = server_state(whitelist=[".Tim"], online={".Tim"})
    hallpass = mc_service(subject("r2", "suspended", bedrock_identity("Tim"), generation=6))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == []
    assert "fwhitelist remove Tim" in state.commands
    assert f"kick .Tim {KICK}" in state.commands
    assert ".Tim" not in state.online
    assert outcomes(hallpass) == [("r2", "suspended", None)]


# AC11.3: lifting the hold re-adds the player and reports allowed -------------------------------------

async def test_lifting_the_hold_re_adds_the_player() -> None:
    state = server_state(whitelist=["Steve"], online={"Steve"})
    hallpass = mc_service(subject("r1", "suspended", java_identity("Steve"), generation=5))
    async with rcon_server(state) as port:
        await run(hallpass, port)
        assert state.whitelist == []

        hallpass.services[0]["subjects"] = [subject("r1", "allowed", java_identity("Steve"), generation=6)]
        await run(hallpass, port)
    assert state.whitelist == ["Steve"]
    assert outcomes(hallpass) == [("r1", "suspended", None), ("r1", "allowed", None)]


# AC11.4: a player on the ban list is banned_external, and nothing is applied or pardoned ------------------

async def test_a_banned_player_is_reported_banned_external_and_never_re_added() -> None:
    state = server_state(bans=[ban("Steve")])
    hallpass = mc_service(subject("r1", "allowed", java_identity("Steve"), generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.commands == LIST_COMMANDS
    assert state.whitelist == []
    assert outcomes(hallpass) == [("r1", "banned_external", None)]


async def test_a_banned_player_with_a_suspend_desired_is_banned_external_and_left_as_it_is() -> None:
    state = server_state(whitelist=["Steve"], bans=[ban("Steve")])
    hallpass = mc_service(subject("r1", "suspended", java_identity("Steve"), generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.commands == LIST_COMMANDS
    assert state.whitelist == ["Steve"]
    assert outcomes(hallpass) == [("r1", "banned_external", None)]


async def test_a_ban_on_a_bedrock_player_is_matched_by_its_prefixed_name() -> None:
    state = server_state(bans=[ban(".Tim")])
    hallpass = mc_service(subject("r2", "allowed", bedrock_identity("Tim"), generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.commands == LIST_COMMANDS
    assert outcomes(hallpass) == [("r2", "banned_external", None)]


async def test_a_ban_on_a_longer_name_does_not_ban_a_shorter_one() -> None:
    state = server_state(bans=[ban("Steven")])
    hallpass = mc_service(subject("r1", "allowed", java_identity("Steve"), generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == ["Steve"]
    assert outcomes(hallpass) == [("r1", "allowed", None)]


async def test_the_ban_list_header_line_is_not_a_ban_on_a_player_named_as_its_words() -> None:
    state = server_state(whitelist=["ban"], bans=[ban("Other")])
    hallpass = mc_service(subject("r1", "allowed", java_identity("ban"), generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert outcomes(hallpass) == [("r1", "allowed", None)]


# A managed name that appears only as the banner or in the reason is not banned --------------------------

async def test_a_managed_name_that_only_banned_someone_else_is_not_banned_and_is_suspended() -> None:
    # "Steve" is the banner of Griefer's ban, so Steve is not on the ban list and the hold is applied.
    state = server_state(whitelist=["Steve"], online={"Steve"}, bans=[ban("Griefer", by="Steve", reason="x")])
    hallpass = mc_service(subject("r1", "suspended", java_identity("Steve"), generation=5))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == []
    assert "Steve" not in state.online
    assert outcomes(hallpass) == [("r1", "suspended", None)]


async def test_a_managed_name_that_only_appears_in_a_ban_reason_is_not_banned_and_is_suspended() -> None:
    state = server_state(whitelist=["Tim"], online={"Tim"},
                         bans=[ban("Griefer", by="Admin", reason="alt of Tim")])
    hallpass = mc_service(subject("r1", "suspended", java_identity("Tim"), generation=5))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == []
    assert outcomes(hallpass) == [("r1", "suspended", None)]


async def test_a_managed_name_that_only_appears_in_a_ban_reason_is_allowed_when_entitled() -> None:
    state = server_state(bans=[ban("Griefer", by="Admin", reason="alt of Tim")])
    hallpass = mc_service(subject("r1", "allowed", java_identity("Tim"), generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == ["Tim"]
    assert outcomes(hallpass) == [("r1", "allowed", None)]


async def test_a_managed_name_on_its_own_ban_line_is_banned_whatever_the_case() -> None:
    state = server_state(bans=[ban("steve", by="Admin", reason="spam")])
    hallpass = mc_service(subject("r1", "allowed", java_identity("Steve"), generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert outcomes(hallpass) == [("r1", "banned_external", None)]


# AC11.5: RCON refusing the connection or the password is error, never a partial success -------------------

async def test_a_wrong_rcon_password_reports_auth_for_every_minecraft_subject() -> None:
    state = server_state(password=PASSWORD)
    hallpass = mc_service(subject("r1", "allowed", java_identity("Steve")),
                          subject("r2", "allowed", bedrock_identity("Tim")))
    async with rcon_server(state) as port:
        await run(hallpass, port, password=WRONG_PASSWORD)
    assert outcomes(hallpass) == [("r1", "error", "auth"), ("r2", "error", "auth")]
    assert state.commands == []


async def test_a_closed_rcon_port_reports_unreachable() -> None:
    hallpass = mc_service(subject("r1", "allowed", java_identity("Steve")))
    await run(hallpass, closed_port())
    assert outcomes(hallpass) == [("r1", "error", "unreachable")]


async def test_a_connection_lost_on_the_kick_is_an_error_and_never_a_suspension() -> None:
    state = server_state(whitelist=["Steve"], online={"Steve"}, drop_on="kick")
    hallpass = mc_service(subject("r1", "suspended", java_identity("Steve"), generation=5))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    # The whitelist removal went through before the connection was lost, so the change is partial. It is reported
    # as an error with the reason the client raised, and never as suspended.
    assert state.whitelist == []
    assert outcomes(hallpass) == [("r1", "error", "protocol")]
    assert all(observed != "suspended" for _, observed, _ in outcomes(hallpass))


async def test_a_server_that_ignores_the_whitelist_removal_is_not_applied_and_never_a_suspension() -> None:
    state = server_state(whitelist=["Steve"], handler=ignore_whitelist_remove)
    hallpass = mc_service(subject("r1", "suspended", java_identity("Steve"), generation=5))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == ["Steve"]
    assert outcomes(hallpass) == [("r1", "error", "not_applied")]


# A partly listed player is brought fully onto the whitelist --------------------------------------------

async def test_a_player_with_only_one_of_two_names_listed_is_brought_fully_onto_the_whitelist() -> None:
    state = server_state(whitelist=["Steve"])
    hallpass = mc_service(subject("r1", "allowed", java_identity("Steve"), bedrock_identity("Tim"), generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == ["Steve", ".Tim"]
    assert outcomes(hallpass) == [("r1", "allowed", None)]


# The whitelist list format: the plan's default strings, parsed exactly -----------------------------------

@pytest.mark.parametrize(("text", "names"), [
    ("There are 1 whitelisted player(s): Steve", ["Steve"]),
    ("There are 3 whitelisted player(s): a, b, c", ["a", "b", "c"]),
    ("There are 2 whitelisted players: Steve, .Tim", ["Steve", ".Tim"]),
    ("There are no whitelisted players", []),
])
def test_the_whitelist_list_parses_the_plan_format(text: str, names: list[str]) -> None:
    assert _parse_whitelist(text) == names


def test_a_whitelist_list_in_an_unknown_format_is_unexpected() -> None:
    with pytest.raises(AdapterError) as info:
        _parse_whitelist("Whitelist: Steve")
    assert info.value.reason == "unexpected"


async def test_a_whitelist_in_the_plan_format_is_read_and_a_hold_is_applied() -> None:
    state = server_state(whitelist=["Steve", "Alex"], online={"Steve"})
    hallpass = mc_service(subject("r1", "suspended", java_identity("Steve"), generation=5))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == ["Alex"]
    assert outcomes(hallpass) == [("r1", "suspended", None)]


# AC9.10 (Minecraft): an unmanaged whitelist entry is left alone and counted --------------------------------

async def test_an_unmanaged_whitelist_entry_stays_and_is_counted() -> None:
    state = server_state(whitelist=["Stranger"])
    hallpass = mc_service(subject("r1", "allowed", java_identity("Steve"), generation=1))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert state.whitelist == ["Stranger", "Steve"]
    assert hallpass.unmanaged == [{"slug": "mc", "count": 1}]


# Subjects without a usable Minecraft identity -----------------------------------------------------------

async def test_a_subject_with_no_minecraft_identity_is_an_error_and_nothing_is_added() -> None:
    state = server_state()
    hallpass = mc_service(subject("r1", "allowed", {"kind": "sso_subject", "value": "not-minecraft"}))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert outcomes(hallpass) == [("r1", "error", "no_identity")]
    assert state.whitelist == []


# Names that are not a plain player name: selectors (@a sends every online player to the whitelist and kick commands),
# over-long names, empty names, spaces, non-ASCII whitespace and command punctuation. Each is refused before any command.
BAD_NAMES = ["", "Ste ve", "Ste\nve", "@a", "@e[type=player]", "@p", "@r", "A" * 17, "Ste\u00a0ve", "Steve;op",
             "Steve/op", "Steve[x]"]


@pytest.mark.parametrize("desired", ["allowed", "suspended"])
@pytest.mark.parametrize("kind", [JAVA_KIND, BEDROCK_KIND])
@pytest.mark.parametrize("value", BAD_NAMES)
async def test_a_name_that_is_not_a_plain_player_name_is_bad_identity_and_no_change_is_sent(
        kind: str, value: str, desired: str) -> None:
    state = server_state(whitelist=["Steve", ".Tim"], online={"Steve", ".Tim"})
    identity = {"kind": kind, "value": value}
    hallpass = mc_service(subject("r1", desired, identity, generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert outcomes(hallpass) == [("r1", "error", "bad_identity")]
    assert state.commands == LIST_COMMANDS
    assert state.whitelist == ["Steve", ".Tim"]
    assert state.online == {"Steve", ".Tim"}


@pytest.mark.parametrize("kind", [JAVA_KIND, BEDROCK_KIND])
async def test_a_sixteen_character_name_is_accepted(kind: str) -> None:
    name = "A" * 16
    state = server_state()
    hallpass = mc_service(subject("r1", "allowed", {"kind": kind, "value": name}, generation=2))
    async with rcon_server(state) as port:
        await run(hallpass, port)
    assert outcomes(hallpass) == [("r1", "allowed", None)]
    assert state.whitelist == [name if kind == JAVA_KIND else "." + name]


async def test_apply_refuses_a_selector_even_when_called_directly() -> None:
    state = server_state(whitelist=["Steve"], online={"Steve"})
    async with rcon_server(state) as port:
        adapter = make_adapter(port)
        result = await adapter.apply(
            Subject(ref="r1", identities=(Identity(kind=JAVA_KIND, value="@a"),), desired="suspended", generation=2),
            "suspended",
        )
    assert result == Observed("error", "bad_identity")
    assert state.commands == []
    assert state.whitelist == ["Steve"]


@pytest.mark.parametrize("prefix", ["@", "@a", "..", "ab", "a b", "\u00a0"])
async def test_a_floodgate_prefix_outside_the_allowlist_is_not_configured(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, prefix: str) -> None:
    db = Database.open(tmp_path / "hallmonitor.db")
    try:
        async with httpx.AsyncClient() as http:
            ctx = AdapterContext(slug="mc", settings={"host": "mc.invalid", "rcon_password": PASSWORD,
                                                      "floodgate_prefix": prefix},
                                 config=Config(hallpass_url=URL, hallpass_token=TOKEN),
                                 db=db, http=http, clock=lambda: NOW)
            with caplog.at_level(logging.INFO):
                assert build_adapter("minecraft", ctx) is None
    finally:
        db.close()
    assert [r.getMessage() for r in caplog.records].count("adapter_not_configured") == 1


@pytest.mark.parametrize("prefix", ["", ".", "*", "_"])
async def test_an_allowed_floodgate_prefix_is_configured(tmp_path: Path, prefix: str) -> None:
    db = Database.open(tmp_path / "hallmonitor.db")
    try:
        async with httpx.AsyncClient() as http:
            ctx = AdapterContext(slug="mc", settings={"host": "mc.invalid", "rcon_password": PASSWORD,
                                                      "floodgate_prefix": prefix},
                                 config=Config(hallpass_url=URL, hallpass_token=TOKEN),
                                 db=db, http=http, clock=lambda: NOW)
            assert isinstance(build_adapter("minecraft", ctx), MinecraftAdapter)
    finally:
        db.close()


# Privacy: no name, password or command text reaches a log ----------------------------------------------

async def test_no_player_name_password_or_command_text_reaches_a_log(caplog: pytest.LogCaptureFixture) -> None:
    state = server_state(whitelist=["Steve"], online={"Steve"})
    hallpass = mc_service(subject("r1", "suspended", java_identity("Steve"), generation=5))
    async with rcon_server(state) as port:
        with caplog.at_level(logging.DEBUG):
            await run(hallpass, port)
    logged = "".join(JsonFormatter().format(record) for record in caplog.records)
    assert "Steve" not in logged
    assert PASSWORD not in logged
    assert "whitelist remove" not in logged
    assert "kick " not in logged


# Registry and capabilities ------------------------------------------------------------------------------

async def test_the_registry_needs_a_host_and_an_rcon_password(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "hallmonitor.db")
    try:
        async with httpx.AsyncClient() as http:
            def ctx(settings: dict[str, str]) -> AdapterContext:
                return AdapterContext(slug="mc", settings=settings,
                                      config=Config(hallpass_url=URL, hallpass_token=TOKEN),
                                      db=db, http=http, clock=lambda: NOW)

            assert build_adapter("minecraft", ctx({})) is None
            assert build_adapter("minecraft", ctx({"host": "mc.invalid"})) is None
            assert build_adapter("minecraft", ctx({"rcon_password": PASSWORD})) is None
            assert build_adapter("minecraft", ctx({"host": "mc.invalid", "rcon_password": PASSWORD,
                                                   "port": "not-a-port"})) is None
            configured = build_adapter("minecraft", ctx({"host": "mc.invalid", "rcon_password": PASSWORD}))
            assert isinstance(configured, MinecraftAdapter)
    finally:
        db.close()


def test_the_minecraft_capabilities_claim_suspend_restore_read_and_connected_removal() -> None:
    caps = MinecraftAdapter.capabilities
    assert caps.suspend and caps.restore and caps.read_state and caps.removes_connected
