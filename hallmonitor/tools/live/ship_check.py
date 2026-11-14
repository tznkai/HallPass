# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run Source RCON ban check for The Ship. Acts only on the --steam test account it is given.

Runs listid, banid, listid, writeid, removeid, writeid and listid through the shared RCON client in the source dialect,
and prints each raw reply. The other IDs in a reply are masked, keeping only the universe and parity digits. The test
account is removed again if the ban was attempted and the sequence did not reach its removal.

Environment: SHIP_RCON_HOST, SHIP_RCON_PASSWORD, and optionally SHIP_RCON_PORT (default 27015).
Exit codes: 0 every step ran; 1 a step or the connection failed; 2 configuration error; 130 interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re

from _live import Console, LiveConfigError, required_env

from hallmonitor.adapters.steam import steam2
from hallmonitor.rcon import RconClient, RconError

DEFAULT_PORT = 27015
DIGITS = re.compile(r"[0-9]{1,20}")
UNIVERSES = ("0", "1")
STEAM_ID_IN_REPLY = re.compile(r"STEAM_([0-5]):([01]):[0-9]+")


def digits_arg(value: str) -> str:
    if DIGITS.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("--steam must be a SteamID64 of decimal digits")
    return value


def mask_ids(text: str) -> str:
    """Each Source ID in a reply with its account number masked. The universe and parity digits stay visible."""
    return STEAM_ID_IN_REPLY.sub(r"STEAM_\1:\2:<n>", text)


async def _show(console: Console, client: RconClient, label: str, cmd: str) -> None:
    text = await client.command(cmd)
    console.line(f"STEP {label}:")
    for line in mask_ids(text).splitlines() or ["(empty)"]:
        console.line(f"  {line}")


async def _cleanup(console: Console, client: RconClient, sid: str) -> bool:
    """Removes the test ID after a failed sequence. False when the removal could not run."""
    try:
        await _show(console, client, "cleanup removeid", f"removeid {sid}")
        await _show(console, client, "cleanup writeid", "writeid")
    except (RconError, OSError) as exc:
        console.line(f"STEP cleanup: failed: {console.error(exc)}")
        return False
    return True


async def run_checks(console: Console, client: RconClient, steam64: str, universe: str) -> bool:
    """The Source sequence. Returns False when a cleanup removal could not run. A failing step raises, after cleanup."""
    sid = steam2(steam64, universe)
    attempted = False
    removed = False
    cleanup_ok = True
    try:
        await _show(console, client, "listid (before)", "listid")
        attempted = True
        await _show(console, client, "banid (permanent)", f"banid 0 {sid}")
        await _show(console, client, "listid after banid", "listid")
        await _show(console, client, "writeid after banid", "writeid")
        await _show(console, client, "removeid", f"removeid {sid}")
        removed = True
        await _show(console, client, "writeid after removeid", "writeid")
        await _show(console, client, "listid after removeid", "listid")
    finally:
        if attempted and not removed:
            cleanup_ok = await _cleanup(console, client, sid)
    return cleanup_ok


def _port() -> int | None:
    """SHIP_RCON_PORT when set, else the default. None when it is set but is not a port number."""
    raw = os.environ.get("SHIP_RCON_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        return None
    return int(raw)


async def _run(console: Console, host: str, port: int, password: str, steam: str, universe: str) -> int:
    try:
        async with RconClient(host, port, password, dialect="source") as client:
            ok = await run_checks(console, client, steam, universe)
    except RconError as exc:
        console.line(f"rcon error: {exc.reason}")
        return 1
    except OSError as exc:
        console.line(console.error(exc))
        return 1
    if not ok:
        console.line("cleanup incomplete: check the ban list by hand")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hand-run Source RCON ban checks on one test account.")
    parser.add_argument("--steam", required=True, type=digits_arg,
                        help="the SteamID64 of a dedicated test account, digits only")
    parser.add_argument("--universe", default="0", choices=UNIVERSES,
                        help="the universe digit the server prints, 0 by default")
    args = parser.parse_args(argv)

    try:
        host = required_env("SHIP_RCON_HOST")
        password = required_env("SHIP_RCON_PASSWORD")
    except LiveConfigError as exc:
        print(f"config error: {exc}")
        return 2
    port = _port()
    if port is None:
        print("config error: SHIP_RCON_PORT must be a port number")
        return 2
    try:
        sid = steam2(args.steam, args.universe)
    except ValueError as exc:
        print(f"config error: {exc}")
        return 2

    console = Console([password, host, args.steam, sid])
    try:
        return asyncio.run(_run(console, host, port, password, args.steam, args.universe))
    except KeyboardInterrupt:
        console.line("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
