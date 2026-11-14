# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake Source-RCON server for the Minecraft tests, and for The Ship's later. A real asyncio TCP server on loopback.

It speaks two end-of-response dialects: `minecraft` (a type-200 sentinel, answered `Unknown request c8`) and `source`
(an empty type-0 sentinel, echoed, then a `\\x00\\x00\\x00\\x01` terminator). Commands go to `state.handler` when it is set,
otherwise to the default Minecraft handler.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

LOOPBACK = "127.0.0.1"
PACKET_RESPONSE = 0
PACKET_COMMAND = 2
PACKET_AUTH = 3
PACKET_MC_SENTINEL = 200
MC_SENTINEL_REPLY = "Unknown request c8"
SOURCE_TERMINATOR = b"\x00\x00\x00\x01"
FRAGMENT_BYTES = 4096
UNKNOWN_COMMAND = "Unknown or incomplete command"
MAX_ONLINE = 20

Handler = Callable[["FakeRconState", str], str]


@dataclass
class FakeRconState:
    password: str = "test-pass"
    whitelist: list[str] = field(default_factory=list)  # vanilla list names; Bedrock names carry the prefix
    bans: list[tuple[str, str, str]] = field(default_factory=list)  # (name, banner, reason)
    online: set[str] = field(default_factory=set)
    prefix: str = "."
    dialect: Literal["minecraft", "source"] = "minecraft"
    commands: list[str] = field(default_factory=list)
    drop_after: int | None = None  # close the connection once this many commands have arrived on it
    drop_on: str | None = None  # close without replying when a command starts with this
    fragment: bool = False  # split each response into 4096-byte packets
    handler: Handler | None = None


def _index(names: list[str], name: str) -> int | None:
    lowered = name.lower()
    for i, existing in enumerate(names):
        if existing.lower() == lowered:
            return i
    return None


def _whitelist_list(state: FakeRconState) -> str:
    # The plan's default format, with the plural written as player(s) for every count.
    if not state.whitelist:
        return "There are no whitelisted players"
    return f"There are {len(state.whitelist)} whitelisted player(s): {', '.join(state.whitelist)}"


def _whitelist_add(state: FakeRconState, stored: str, shown: str) -> str:
    if _index(state.whitelist, stored) is not None:
        return "Player is already whitelisted"
    state.whitelist.append(stored)
    return f"Added {shown} to the whitelist"


def _whitelist_remove(state: FakeRconState, stored: str, shown: str) -> str:
    index = _index(state.whitelist, stored)
    if index is None:
        return "Player is not whitelisted"
    del state.whitelist[index]
    return f"Removed {shown} from the whitelist"


def _banlist(state: FakeRconState) -> str:
    if not state.bans:
        return "There are no bans"
    lines = [f"{name} was banned by {banner}: {reason}" for name, banner, reason in state.bans]
    return "\n".join([f"There are {len(state.bans)} ban(s):", *lines])


def _list_online(state: FakeRconState) -> str:
    names = sorted(state.online)
    return f"There are {len(names)} of a max of {MAX_ONLINE} players online: {', '.join(names)}"


def _kick(state: FakeRconState, args: str) -> str:
    name, _, message = args.partition(" ")
    found = next((player for player in state.online if player.lower() == name.lower()), None)
    if found is None:
        return "That player cannot be found"
    state.online.discard(found)
    return f"Kicked {found}: {message}"


def default_minecraft_handler(state: FakeRconState, cmd: str) -> str:
    verb, _, args = cmd.partition(" ")
    if cmd == "whitelist list":
        return _whitelist_list(state)
    if verb == "whitelist":
        sub, _, name = args.partition(" ")
        if sub == "add" and name:
            return _whitelist_add(state, name, name)
        if sub == "remove" and name:
            return _whitelist_remove(state, name, name)
    if verb == "fwhitelist":
        sub, _, tag = args.partition(" ")
        if sub == "add" and tag:
            return _whitelist_add(state, state.prefix + tag, tag)
        if sub == "remove" and tag:
            return _whitelist_remove(state, state.prefix + tag, tag)
    if cmd == "banlist players":
        return _banlist(state)
    if verb == "kick" and args:
        return _kick(state, args)
    if cmd == "list":
        return _list_online(state)
    return UNKNOWN_COMMAND


def _dispatch(state: FakeRconState, cmd: str) -> str:
    if state.handler is not None:
        return state.handler(state, cmd)
    return default_minecraft_handler(state, cmd)


def _packet(req_id: int, ptype: int, body: bytes) -> bytes:
    payload = struct.pack("<ii", req_id, ptype) + body + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _response_packets(state: FakeRconState, req_id: int, text: str) -> bytes:
    data = text.encode("utf-8")
    if not state.fragment:
        return _packet(req_id, PACKET_RESPONSE, data)
    chunks = [data[i:i + FRAGMENT_BYTES] for i in range(0, len(data), FRAGMENT_BYTES)] or [b""]
    return b"".join(_packet(req_id, PACKET_RESPONSE, chunk) for chunk in chunks)


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    (length,) = struct.unpack("<i", await reader.readexactly(4))
    req_id, ptype = struct.unpack("<ii", await reader.readexactly(8))
    rest = await reader.readexactly(length - 8)  # body and two trailing nulls
    return req_id, ptype, rest[:-2]


async def _serve(state: FakeRconState, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    handled = 0
    while True:
        req_id, ptype, body = await _read_packet(reader)
        if ptype == PACKET_AUTH:
            if body.decode("utf-8", "replace") == state.password:
                writer.write(_packet(req_id, PACKET_COMMAND, b""))
            else:
                writer.write(_packet(-1, PACKET_COMMAND, b""))
            await writer.drain()
        elif ptype == PACKET_COMMAND:
            if state.drop_after is not None and handled >= state.drop_after:
                return
            handled += 1
            cmd = body.decode("utf-8", "replace")
            state.commands.append(cmd)
            if state.drop_on is not None and cmd.startswith(state.drop_on):
                return
            writer.write(_response_packets(state, req_id, _dispatch(state, cmd)))
            await writer.drain()
        elif ptype == PACKET_MC_SENTINEL and state.dialect == "minecraft":
            writer.write(_packet(req_id, PACKET_RESPONSE, MC_SENTINEL_REPLY.encode("utf-8")))
            await writer.drain()
        elif ptype == PACKET_RESPONSE and body == b"" and state.dialect == "source":
            writer.write(_packet(req_id, PACKET_RESPONSE, b"") + _packet(req_id, PACKET_RESPONSE, SOURCE_TERMINATOR))
            await writer.drain()


@asynccontextmanager
async def rcon_server(state: FakeRconState) -> AsyncIterator[int]:
    """Serves `state` on a loopback port chosen by the OS, and yields that port."""
    writers: set[asyncio.StreamWriter] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.add(writer)
        try:
            await _serve(state, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writers.discard(writer)
            writer.close()

    server = await asyncio.start_server(handle, LOOPBACK, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        for writer in list(writers):
            writer.close()
        await server.wait_closed()
