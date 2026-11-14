# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run Palworld REST check for hallmonitor. Acts only on the --steam test account it is given.

Lists the players and prints only the key names of the reply. Bans the test account with the REST API, then waits
while the operator checks the ban-list file with ptero_files_check.py. The ban is lifted in every case once it was
sent, including when the wait is interrupted. When the unban is refused or cannot be sent, the run says the test ban may
still be in place and asks for it to be lifted by hand. The account's id is never printed.

Environment: PAL_REST_URL, PAL_ADMIN_PASSWORD. The admin password is never printed.
Exit codes: 0 every step ran; 1 a step failed; 2 configuration error; 130 interrupted.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable
from typing import Any

import httpx
from _live import Console, LiveConfigError, instance_secrets, required_env

ADMIN_USER = "admin"
BAN_MESSAGE = "hallmonitor test"
UNBAN_BY_HAND = "test ban may still be in place: unban by hand"
TIMEOUT_SECONDS = 30.0
DIGITS = re.compile(r"[0-9]{1,20}")


def digits_arg(value: str) -> str:
    if DIGITS.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("--steam must be a SteamID64 of decimal digits")
    return value


def key_paths(value: Any, prefix: str = "") -> list[str]:
    """The key names of a JSON value, as dotted paths with [] for list items. Values are never included."""
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            paths.update(key_paths(child, path))
    elif isinstance(value, list):
        for item in value:
            paths.update(key_paths(item, f"{prefix}[]"))
    return sorted(paths)


def _userid(steam: str) -> str:
    return f"steam_{steam}"


def _show(console: Console, label: str, response: httpx.Response) -> None:
    console.step(label, f"status={response.status_code}")


def _unban(console: Console, client: httpx.Client, userid: str) -> bool:
    """Lifts the test ban. A refused or unreachable unban is reported with the manual step, and never with the id. True
    when the server answered with a 2xx status."""
    try:
        response = client.post("/v1/api/unban", json={"userid": userid})
    except httpx.HTTPError as exc:
        console.step("unban", f"failed: {console.error(exc)}")
        console.line(UNBAN_BY_HAND)
        return False
    _show(console, "unban", response)
    if not 200 <= response.status_code < 300:
        console.line(UNBAN_BY_HAND)
        return False
    return True


def run_checks(console: Console, client: httpx.Client, steam: str, pause: Callable[[], None]) -> bool:
    """The list, the ban, the operator's check, then the unban. The unban runs in a finally once the ban was sent. Returns
    True when the test ban was lifted."""
    userid = _userid(steam)
    players = client.get("/v1/api/players")
    _show(console, "players", players)
    try:
        console.step("players keys", ", ".join(key_paths(players.json())) or "(none)")
    except ValueError:
        console.step("players keys", "reply was not JSON")

    banned = False
    lifted = True
    try:
        banned = True
        _show(console, "ban", client.post("/v1/api/ban", json={"userid": userid, "message": BAN_MESSAGE}))
        console.line("Check the ban list now: re-read the Palworld ban-list file with tools/live/ptero_files_check.py.")
        console.line("Press Enter here once you have checked, to lift the test ban.")
        pause()
    finally:
        if banned:
            lifted = _unban(console, client, userid)
    return lifted


def _wait_for_enter() -> None:
    try:
        input("> ")
    except EOFError:
        return


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None,
         pause: Callable[[], None] = _wait_for_enter) -> int:
    parser = argparse.ArgumentParser(description="Hand-run Palworld REST checks on one test account.")
    parser.add_argument("--steam", required=True, type=digits_arg,
                        help="the SteamID64 of a dedicated test account, digits only")
    args = parser.parse_args(argv)

    try:
        base_url = required_env("PAL_REST_URL")
        password = required_env("PAL_ADMIN_PASSWORD")
    except LiveConfigError as exc:
        print(f"config error: {exc}")
        return 2

    console = Console([password, args.steam, _userid(args.steam), *instance_secrets(base_url)])
    client = httpx.Client(base_url=base_url.rstrip("/"), auth=httpx.BasicAuth(ADMIN_USER, password),
                          timeout=TIMEOUT_SECONDS, transport=transport)
    try:
        return 0 if run_checks(console, client, args.steam, pause) else 1
    except httpx.HTTPError as exc:
        console.line(console.error(exc))
        return 1
    except KeyboardInterrupt:
        console.line("interrupted")
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
