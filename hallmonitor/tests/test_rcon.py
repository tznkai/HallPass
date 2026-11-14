# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RCON client against the fake server on loopback. Covers AC11.5 at the protocol level. No other network."""

from __future__ import annotations

import socket

import pytest

from hallmonitor.rcon import RconClient, RconError
from tests.fake_rcon import LOOPBACK, FakeRconState, rcon_server

PASSWORD = "test-pass"
DIALECTS = ["minecraft", "source"]
LONG_TEXT = "ab" * 3000  # 6000 characters: two 4096-byte packets' worth of body in each dialect
COMMAND_LIMIT = 1446


def closed_port() -> int:
    """A loopback TCP port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


async def test_connecting_to_a_closed_port_is_unreachable():
    with pytest.raises(RconError) as info:
        async with RconClient(LOOPBACK, closed_port(), PASSWORD):
            pass
    assert info.value.reason == "unreachable"


async def test_wrong_password_is_auth_failure():
    state = FakeRconState(password="right-pass")
    async with rcon_server(state) as port:
        with pytest.raises(RconError) as info:
            async with RconClient(LOOPBACK, port, "wrong-pass"):
                pass
    assert info.value.reason == "auth"


@pytest.mark.parametrize("dialect", DIALECTS)
async def test_command_round_trip_returns_the_handler_output(dialect):
    state = FakeRconState(dialect=dialect, whitelist=["Steve"])
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD, dialect=dialect) as client:
            text = await client.command("whitelist list")
    assert text == "There are 1 whitelisted player(s): Steve"
    assert state.commands == ["whitelist list"]


@pytest.mark.parametrize("dialect", DIALECTS)
async def test_long_fragmented_response_is_reassembled_in_full(dialect):
    state = FakeRconState(dialect=dialect, fragment=True, handler=lambda _state, _cmd: LONG_TEXT)
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD, dialect=dialect) as client:
            text = await client.command("anything")
    assert text == LONG_TEXT


@pytest.mark.parametrize("dialect", DIALECTS)
async def test_minecraft_colour_codes_are_stripped(dialect):
    state = FakeRconState(dialect=dialect, handler=lambda _state, _cmd: "\u00a7aHello \u00a7lthere")
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD, dialect=dialect) as client:
            text = await client.command("anything")
    assert text == "Hello there"


async def test_connection_dropped_mid_response_is_protocol_error():
    state = FakeRconState(drop_after=0)
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD) as client:
            with pytest.raises(RconError) as info:
                await client.command("whitelist list")
    assert info.value.reason == "protocol"


async def test_command_over_the_limit_is_refused_before_sending():
    state = FakeRconState()
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD) as client:
            with pytest.raises(RconError) as info:
                await client.command("a" * (COMMAND_LIMIT + 1))
    assert info.value.reason == "protocol"
    assert state.commands == []


async def test_command_at_the_limit_is_sent():
    state = FakeRconState(handler=lambda _state, _cmd: "ok")
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD) as client:
            text = await client.command("a" * COMMAND_LIMIT)
    assert text == "ok"
    assert len(state.commands[0]) == COMMAND_LIMIT


def test_repr_never_shows_the_password():
    assert PASSWORD not in repr(RconClient(LOOPBACK, 1, PASSWORD))
