# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An asyncio client for the Source RCON protocol, with the Minecraft and Source end-of-response sentinels.

Every failure raises RconError with a fixed reason. A message never carries the password or the command text.
"""

from __future__ import annotations

import asyncio
import re
import struct
from typing import Literal, Self

Dialect = Literal["minecraft", "source"]
Reason = Literal["unreachable", "auth", "protocol", "timeout"]

PACKET_RESPONSE = 0
PACKET_COMMAND = 2
PACKET_AUTH = 3
PACKET_MC_SENTINEL = 200  # an invalid type; a Minecraft server answers it with a reply that names it as unknown
MAX_COMMAND_BYTES = 1446
MAX_RESPONSE_BODY = 4 * 4096  # a server splits output per 4096 characters, and each character is at most 4 UTF-8 bytes
SOURCE_TERMINATOR = b"\x00\x00\x00\x01"
COLOUR_CODE = re.compile("\u00a7.")
HEADER_FIELDS = 8  # request id and packet type, both int32
TRAILER_BYTES = 2  # two null bytes end every packet


class RconError(Exception):
    """A failed RCON exchange. `reason` is one of unreachable, auth, protocol or timeout."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason = reason


class RconClient:
    """One authenticated connection. Use it as an async context manager; each command is one round trip."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        dialect: Dialect = "minecraft",
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._dialect = dialect
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1

    def __repr__(self) -> str:
        return f"RconClient(dialect={self._dialect!r}, timeout={self._timeout!r})"

    async def __aenter__(self) -> Self:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), self._timeout,
            )
        except TimeoutError:
            raise RconError("timeout") from None
        except OSError:
            raise RconError("unreachable") from None
        try:
            await self._authenticate()
        except BaseException:
            await self._close()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._close()

    async def command(self, cmd: str) -> str:
        body = cmd.encode("utf-8")
        if len(body) > MAX_COMMAND_BYTES:
            raise RconError("protocol")
        cmd_id = self._take_id()
        sentinel_id = self._take_id()
        await self._send(cmd_id, PACKET_COMMAND, body)
        # Source keeps its sentinel pipelined: the Source protocol defines the empty-packet sentinel for exactly that.
        # Minecraft is different. A vanilla server may drop the connection when the command and the sentinel arrive in
        # one read, so its sentinel goes out only after the first response packet for this command has arrived.
        sentinel_sent = self._dialect == "source"
        if sentinel_sent:
            await self._send(sentinel_id, PACKET_RESPONSE, b"")
        parts: list[bytes] = []
        while True:
            got_id, ptype, data = await self._read()
            if got_id == sentinel_id:
                # Minecraft: any reply to the sentinel ends the response. Source: the empty echo is skipped.
                if self._dialect == "minecraft" or data == SOURCE_TERMINATOR:
                    break
                continue
            if got_id == cmd_id and ptype == PACKET_RESPONSE:
                parts.append(data)
                if not sentinel_sent:
                    await self._send(sentinel_id, PACKET_MC_SENTINEL, b"")
                    sentinel_sent = True
        text = b"".join(parts).decode("utf-8", errors="replace")
        return COLOUR_CODE.sub("", text)

    async def _authenticate(self) -> None:
        await self._send(self._take_id(), PACKET_AUTH, self._password.encode("utf-8"))
        while True:
            req_id, ptype, _ = await self._read()
            if ptype == PACKET_COMMAND:
                if req_id == -1:
                    raise RconError("auth")
                return

    def _take_id(self) -> int:
        taken = self._next_id
        self._next_id += 1
        return taken

    async def _send(self, req_id: int, ptype: int, body: bytes) -> None:
        if self._writer is None:
            raise RconError("protocol")
        payload = struct.pack("<ii", req_id, ptype) + body + b"\x00\x00"
        self._writer.write(struct.pack("<i", len(payload)) + payload)
        try:
            await asyncio.wait_for(self._writer.drain(), self._timeout)
        except TimeoutError:
            raise RconError("timeout") from None
        except (OSError, RuntimeError):
            raise RconError("protocol") from None

    async def _read(self) -> tuple[int, int, bytes]:
        try:
            return await asyncio.wait_for(self._read_packet(), self._timeout)
        except TimeoutError:
            raise RconError("timeout") from None
        except (asyncio.IncompleteReadError, OSError):
            raise RconError("protocol") from None

    async def _read_packet(self) -> tuple[int, int, bytes]:
        if self._reader is None:
            raise RconError("protocol")
        (length,) = struct.unpack("<i", await self._reader.readexactly(4))
        if length < HEADER_FIELDS + TRAILER_BYTES or length > HEADER_FIELDS + MAX_RESPONSE_BODY + TRAILER_BYTES:
            raise RconError("protocol")
        req_id, ptype = struct.unpack("<ii", await self._reader.readexactly(HEADER_FIELDS))
        rest = await self._reader.readexactly(length - HEADER_FIELDS)
        return req_id, ptype, rest[:-TRAILER_BYTES]

    async def _close(self) -> None:
        if self._writer is None:
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except OSError:
            pass
