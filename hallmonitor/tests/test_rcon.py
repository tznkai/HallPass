# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RCON client against the fake server on loopback. Covers AC11.5 at the protocol level. No other network."""

from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from hallmonitor.rcon import RconClient, RconError
from tests.fake_rcon import LOOPBACK, PACKET_AUTH, PACKET_COMMAND, PACKET_MC_SENTINEL, FakeRconState, rcon_server

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


# A server splits output per 4096 characters, and a character may take 4 UTF-8 bytes, so a packet can reach 16384 bytes.
FOUR_BYTE_CHAR = "\U0001F600"   # 4 bytes in UTF-8
THREE_BYTE_CHAR = "\u4e2d"      # 3 bytes in UTF-8


@pytest.mark.parametrize("dialect", DIALECTS)
async def test_a_single_reply_of_four_byte_text_within_one_packet_is_read_in_full(dialect):
    text = FOUR_BYTE_CHAR * 4000   # 16000 bytes in one packet: over 4096 bytes, under the 16384-byte cap
    state = FakeRconState(dialect=dialect, handler=lambda _state, _cmd: text)
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD, dialect=dialect) as client:
            result = await client.command("anything")
    assert result == text


@pytest.mark.parametrize("dialect", DIALECTS)
async def test_fragmented_multi_byte_text_is_reassembled_in_full(dialect):
    text = THREE_BYTE_CHAR * 6000 + FOUR_BYTE_CHAR * 5000   # 43000 bytes across three character-split packets
    state = FakeRconState(dialect=dialect, fragment=True, handler=lambda _state, _cmd: text)
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD, dialect=dialect) as client:
            result = await client.command("anything")
    assert result == text


async def test_fragments_of_the_largest_size_are_all_within_the_cap():
    text = FOUR_BYTE_CHAR * 8192   # two fragments of 4096 characters, each exactly 16384 bytes
    state = FakeRconState(fragment=True, handler=lambda _state, _cmd: text)
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD) as client:
            result = await client.command("anything")
    assert result == text


async def test_a_single_reply_over_the_inbound_cap_is_a_protocol_error():
    state = FakeRconState(handler=lambda _state, _cmd: FOUR_BYTE_CHAR * 4097)   # 16388 bytes: one over the cap
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD) as client:
            with pytest.raises(RconError) as info:
                await client.command("anything")
    assert info.value.reason == "protocol"


async def test_connection_dropped_mid_response_is_protocol_error():
    state = FakeRconState(drop_after=0)
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD) as client:
            with pytest.raises(RconError) as info:
                await client.command("whitelist list")
    assert info.value.reason == "protocol"


SHORT_TIMEOUT = 0.2  # seconds: long enough for loopback, short enough that a timeout test stays quick


@pytest.mark.parametrize("dialect", DIALECTS)
async def test_a_server_that_never_answers_the_sentinel_times_out(dialect):
    state = FakeRconState(dialect=dialect, swallow_sentinel=True)
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD, dialect=dialect, timeout=SHORT_TIMEOUT) as client:
            with pytest.raises(RconError) as info:
                await client.command("whitelist list")
    assert info.value.reason == "timeout"


async def test_a_server_that_never_answers_the_password_times_out():
    state = FakeRconState(silent_auth=True)
    async with rcon_server(state) as port:
        with pytest.raises(RconError) as info:
            async with RconClient(LOOPBACK, port, PASSWORD, timeout=SHORT_TIMEOUT):
                pass
    assert info.value.reason == "timeout"


@pytest.mark.parametrize("dialect", DIALECTS)
async def test_a_response_closed_after_its_first_fragment_is_a_protocol_error(dialect):
    state = FakeRconState(dialect=dialect, fragment=True, close_after_first_fragment=True,
                          handler=lambda _state, _cmd: "a" * 5000)   # two fragments; the second never arrives
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD, dialect=dialect) as client:
            with pytest.raises(RconError) as info:
                await client.command("anything")
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


async def test_source_dialect_authenticates_past_the_empty_response_packet_that_comes_first() -> None:
    state = FakeRconState(dialect="source", handler=lambda _state, _cmd: "ok")
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD, dialect="source") as client:
            text = await client.command("whitelist list")
    assert text == "ok"


async def test_source_dialect_wrong_password_after_the_empty_packet_is_auth_failure() -> None:
    state = FakeRconState(dialect="source", password="right-pass")
    async with rcon_server(state) as port:
        with pytest.raises(RconError) as info:
            async with RconClient(LOOPBACK, port, "wrong-pass", dialect="source"):
                pass
    assert info.value.reason == "auth"


def test_repr_never_shows_the_password():
    assert PASSWORD not in repr(RconClient(LOOPBACK, 1, PASSWORD))


# Pipelining: a Minecraft server may drop a connection that receives the sentinel in the same read as the command ----

def raw_packet(req_id: int, ptype: int, body: bytes) -> bytes:
    payload = struct.pack("<ii", req_id, ptype) + body + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


async def test_the_strict_fake_closes_on_a_pipelined_command_and_sentinel() -> None:
    """The fake is checked against a raw pipelining client: the command and the sentinel go out together, and the
    fake must close the connection without answering."""
    state = FakeRconState(strict_no_pipeline=True)
    async with rcon_server(state) as port:
        reader, writer = await asyncio.open_connection(LOOPBACK, port)
        try:
            writer.write(raw_packet(1, PACKET_AUTH, PASSWORD.encode()))
            await writer.drain()
            await reader.readexactly(14)  # the empty auth reply: a length field and a 10-byte payload
            writer.write(raw_packet(2, PACKET_COMMAND, b"whitelist list") + raw_packet(3, PACKET_MC_SENTINEL, b""))
            await writer.drain()
            leftover = await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
    assert leftover == b""
    assert state.commands == ["whitelist list"]


@pytest.mark.parametrize(("fragment", "text"), [(False, "There are 1 whitelisted player(s): Steve"),
                                                (True, LONG_TEXT)])
async def test_the_minecraft_client_passes_a_server_that_rejects_pipelining(fragment: bool, text: str) -> None:
    state = FakeRconState(strict_no_pipeline=True, fragment=fragment, handler=lambda _state, _cmd: text)
    async with rcon_server(state) as port:
        async with RconClient(LOOPBACK, port, PASSWORD) as client:
            first = await client.command("whitelist list")
            second = await client.command("whitelist list")
    assert first == text
    assert second == text
    assert state.commands == ["whitelist list", "whitelist list"]
