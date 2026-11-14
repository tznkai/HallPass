# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Minecraft live check's RCON link, run against the loopback fake on a background thread. No real server."""

from __future__ import annotations

import asyncio
import socket
import struct
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "live"))
import minecraft_check  # noqa: E402
from tests.fake_rcon import LOOPBACK, FakeRconState, rcon_server  # noqa: E402

PASSWORD = "test-pass"
STARTUP_SECONDS = 5
SOCKET_SECONDS = 2


class ServerThread:
    """The async fake served on its own event loop, so the synchronous live link can talk to it."""

    def __init__(self, state: FakeRconState) -> None:
        self._state = state
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._port = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def serve() -> None:
            async with rcon_server(self._state) as port:
                self._port = port
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.01)

        asyncio.run(serve())

    def __enter__(self) -> int:
        self._thread.start()
        assert self._ready.wait(STARTUP_SECONDS), "the fake server did not start"
        return self._port

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(STARTUP_SECONDS)


@pytest.fixture
def socket_pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    """A connected pair of sockets: the live link takes one end, the test drives the other."""
    left, right = socket.socketpair()
    left.settimeout(SOCKET_SECONDS)   # a read that is waiting for bytes that never come fails, rather than hangs
    right.settimeout(SOCKET_SECONDS)
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def bare_link(sock: socket.socket) -> minecraft_check.RconLink:
    """A live link over an existing socket, without the connect and the login, for the framing checks."""
    link = object.__new__(minecraft_check.RconLink)
    link._sock = sock
    link._next_id = 1
    return link


# The live link sends the sentinel after the first response, as the adapter does ----------------------

def test_the_live_link_passes_a_server_that_rejects_pipelining() -> None:
    state = FakeRconState(strict_no_pipeline=True, whitelist=["Steve"])
    with ServerThread(state) as port:
        link = minecraft_check.RconLink(LOOPBACK, port, PASSWORD)
        try:
            first = link.command("whitelist list")
            second = link.command("whitelist list")
        finally:
            link.close()
    assert "Steve" in first
    assert second == first
    assert state.commands == ["whitelist list", "whitelist list"]


# Packet lengths and command size are bounded before anything is read or sent ------------------------------

@pytest.mark.parametrize("length", [9, -1, 16395, 2**31 - 1])
def test_a_packet_length_outside_the_bounds_is_a_protocol_error(socket_pair, length: int) -> None:
    left, right = socket_pair
    right.sendall(struct.pack("<i", length))
    with pytest.raises(minecraft_check.LinkError) as info:
        bare_link(left)._recv()
    assert str(info.value) == "protocol"


def test_a_packet_at_the_largest_allowed_length_is_read(socket_pair) -> None:
    left, right = socket_pair
    body = b"x" * (4096 * 4)
    payload = struct.pack("<ii", 7, 0) + body + b"\x00\x00"
    right.sendall(struct.pack("<i", len(payload)) + payload)
    assert bare_link(left)._recv() == (7, 0, body)


def test_a_command_over_the_limit_is_refused_before_anything_is_sent(socket_pair) -> None:
    left, right = socket_pair
    right.setblocking(False)
    with pytest.raises(minecraft_check.LinkError) as info:
        bare_link(left).command("a" * (minecraft_check.MAX_COMMAND_BYTES + 1))
    assert str(info.value) == "overlong"
    with pytest.raises(BlockingIOError):
        right.recv(1)


def test_a_multi_byte_command_is_measured_in_bytes(socket_pair) -> None:
    left, right = socket_pair
    right.setblocking(False)
    # 400 characters of a 4-byte code point is 1600 bytes, over the 1446-byte limit.
    with pytest.raises(minecraft_check.LinkError) as info:
        bare_link(left).command("\U0001F600" * 400)
    assert str(info.value) == "overlong"
