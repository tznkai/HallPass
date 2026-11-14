# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run Minecraft RCON live check for hallmonitor. Acts only on the --java and --bedrock test accounts it is given.

Runs the whitelist and ban-list commands and prints their raw output, so the formats can be recorded in
docs/service-notes.md. After the operator confirms the Java test account is online, it removes that account from the
whitelist, waits, lists the players and kicks it. It always removes both test accounts from the whitelist at the end.
Player names are printed as <redacted>, so the output can be pasted into the notes as it is.

Environment: MC_RCON_HOST, MC_RCON_PORT, MC_RCON_PASSWORD. The RCON exchange is inline and minecraft dialect only.
Exit codes: 0 every step ran; 1 a step or the connection failed; 2 configuration error; 130 interrupted.
"""

from __future__ import annotations

import argparse
import re
import socket
import struct
import time

from _live import Console, LiveConfigError, required_env

PACKET_RESPONSE = 0
PACKET_COMMAND = 2
PACKET_AUTH = 3
PACKET_SENTINEL = 200  # an invalid type; a Minecraft server answers it with "Unknown request c8"
MAX_COMMAND_BYTES = 1446  # the most a client may send in one command packet
MIN_PACKET_LENGTH = 10  # request id, type and two trailing nulls
# The largest reply: a server splits its output per 4096 characters, and a character is at most 4 UTF-8 bytes.
MAX_PACKET_LENGTH = 4096 * 4 + 10
CONNECT_TIMEOUT = 5.0
REMOVE_WAIT_SECONDS = 5
KICK_MESSAGE = "hallmonitor test"
JAVA_NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
BEDROCK_TAG = re.compile(r"[A-Za-z0-9_]{3,32}")
CONFIRM_WORD = "online"


class LinkError(Exception):
    """The RCON exchange failed. The reason is a fixed word; it never carries the password or a command."""


class RconLink:
    def __init__(self, host: str, port: int, password: str) -> None:
        try:
            self._sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except OSError:
            raise LinkError("unreachable") from None
        self._next_id = 1
        try:
            self._authenticate(password)
        except BaseException:
            self._sock.close()
            raise

    def _authenticate(self, password: str) -> None:
        self._send(1, PACKET_AUTH, password)
        while True:
            req_id, ptype, _ = self._recv()
            if ptype == PACKET_COMMAND:
                if req_id == -1:
                    raise LinkError("auth")
                return

    def command(self, cmd: str) -> str:
        if len(cmd.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise LinkError("overlong")
        req_id = self._next_id
        self._next_id += 2
        self._send(req_id, PACKET_COMMAND, cmd)
        # The sentinel goes out after the first response packet, never in the same read as the command: a vanilla
        # server may drop the connection on that, as the adapter's RCON client also avoids.
        sentinel_sent = False
        parts: list[bytes] = []
        while True:
            got_id, ptype, body = self._recv()
            if got_id == req_id + 1:
                break
            if got_id == req_id and ptype == PACKET_RESPONSE:
                parts.append(body)
                if not sentinel_sent:
                    self._send(req_id + 1, PACKET_SENTINEL, "")
                    sentinel_sent = True
        return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        self._sock.close()

    def _send(self, req_id: int, ptype: int, body: str) -> None:
        payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)

    def _recv(self) -> tuple[int, int, bytes]:
        (length,) = struct.unpack("<i", self._recv_exact(4))
        if not MIN_PACKET_LENGTH <= length <= MAX_PACKET_LENGTH:
            raise LinkError("protocol")
        req_id, ptype = struct.unpack("<ii", self._recv_exact(8))
        rest = self._recv_exact(length - 8)  # body, then two null bytes
        return req_id, ptype, rest[:-2]

    def _recv_exact(self, count: int) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            chunk = self._sock.recv(count - len(buf))
            if not chunk:
                raise LinkError("closed")
            buf += chunk
        return bytes(buf)


def show(console: Console, link: RconLink, label: str, cmd: str) -> str:
    """Runs one command and prints its raw output under a label. The label names the command shape, never a player."""
    text = link.command(cmd)
    console.line(f"STEP {label}:")
    for line in text.splitlines() or ["(empty)"]:
        console.line(f"  {line}")
    return text


def operator_confirms() -> bool:
    print(f"Join the server as the Java test account. Type {CONFIRM_WORD!r} once it is online, or press Enter to skip.")
    try:
        return input("> ").strip() == CONFIRM_WORD
    except EOFError:
        return False


def run_checks(console: Console, link: RconLink, java: str, bedrock: str) -> None:
    show(console, link, "whitelist list (before)", "whitelist list")
    show(console, link, "whitelist add (java)", f"whitelist add {java}")
    show(console, link, "whitelist list after java add", "whitelist list")
    show(console, link, "fwhitelist add (bedrock)", f"fwhitelist add {bedrock}")
    show(console, link, "whitelist list after bedrock add", "whitelist list")
    show(console, link, "banlist players", "banlist players")

    if not operator_confirms():
        console.line("STEP removal: skipped, the Java test account was not confirmed online")
        return
    show(console, link, "whitelist remove (java)", f"whitelist remove {java}")
    time.sleep(REMOVE_WAIT_SECONDS)
    listing = show(console, link, f"list {REMOVE_WAIT_SECONDS}s after removal", "list")
    still_online = re.search(rf"\b{re.escape(java)}\b", listing, re.IGNORECASE) is not None
    console.line(f"  java_test_account_still_online={still_online}")
    show(console, link, "kick (java)", f"kick {java} {KICK_MESSAGE}")


def clean_up(console: Console, link: RconLink, java: str, bedrock: str) -> bool:
    """Removes both test accounts from the whitelist. Returns False when a removal could not run."""
    ok = True
    for label, cmd in (
        ("cleanup whitelist remove (java)", f"whitelist remove {java}"),
        ("cleanup fwhitelist remove (bedrock)", f"fwhitelist remove {bedrock}"),
    ):
        try:
            show(console, link, label, cmd)
        except (LinkError, OSError) as exc:
            ok = False
            console.line(f"STEP {label}: failed: {console.error(exc)}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Hand-run Minecraft RCON checks against two test accounts.")
    parser.add_argument("--java", required=True, help="name of a dedicated Java test account")
    parser.add_argument("--bedrock", required=True, help="gamertag of a dedicated Bedrock test account, no prefix")
    args = parser.parse_args()

    if not JAVA_NAME.fullmatch(args.java):
        print("config error: --java must be 3 to 16 letters, digits or underscores")
        return 2
    if not BEDROCK_TAG.fullmatch(args.bedrock):
        print("config error: --bedrock must be 3 to 32 letters, digits or underscores")
        return 2

    try:
        host = required_env("MC_RCON_HOST")
        port_text = required_env("MC_RCON_PORT")
        password = required_env("MC_RCON_PASSWORD")
    except LiveConfigError as exc:
        print(f"config error: {exc}")
        return 2
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        print("config error: MC_RCON_PORT must be a port number")
        return 2

    console = Console([password, host, args.java, args.bedrock])
    link: RconLink | None = None
    status = 0
    try:
        link = RconLink(host, int(port_text), password)
        run_checks(console, link, args.java, args.bedrock)
    except KeyboardInterrupt:
        console.line("interrupted")
        return 130
    except LinkError as exc:
        console.line(f"rcon error: {exc}")
        status = 1
    except OSError as exc:
        console.line(console.error(exc))
        status = 1
    finally:
        if link is not None:
            if not clean_up(console, link, args.java, args.bedrock):
                console.line("cleanup incomplete: check the whitelist by hand")
                status = 1
            link.close()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
