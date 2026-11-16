# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run Pterodactyl file and resource check for hallmonitor. Works on one file of one server, given by --path.

Reads the file and prints its line count and the first line's shape, never its content. Writes the same bytes back
unchanged and prints the status. Reads the server's resources and prints its state and whether an uptime is present.
Ids, the key and the instance address are printed as <redacted>, and every digit is masked in a shape.

Environment: PTERO_URL, PTERO_KEY (a subuser key with only file.read and file.read-content, plus file.create for the
write step: the write route checks file.create, not file.update), PTERO_SERVER (the server's short id).
Exit codes: 0 every step ran; 1 a step failed; 2 configuration error; 130 interrupted.
"""

from __future__ import annotations

import argparse
import re

import httpx
from _live import Console, LiveConfigError, instance_secrets, required_env

TIMEOUT_SECONDS = 30.0
SHAPE_LIMIT = 80
DIGIT = re.compile(r"[0-9]")
LETTER = re.compile(r"[A-Za-z]")


def shape(text: str) -> str:
    """The shape of a line: each digit becomes 9 and each letter a. Punctuation and spaces are kept, so the shape of a
    ban line shows its layout without its values."""
    masked = LETTER.sub("a", DIGIT.sub("9", text))
    return masked if len(masked) <= SHAPE_LIMIT else masked[:SHAPE_LIMIT] + "..."


def make_client(base_url: str, key: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
        transport=transport,
    )


def _files_path(server: str, action: str) -> str:
    return f"/api/client/servers/{server}/files/{action}"


def read_file(client: httpx.Client, server: str, path: str, console: Console) -> bytes | None:
    """Reads the file and prints its shape. Returns the bytes exactly as the server sent them, or None when the read
    failed. The shape is decoded only for display, so a file that is not UTF-8 is still written back unchanged."""
    try:
        response = client.get(_files_path(server, "contents"), params={"file": path})
    except httpx.HTTPError as exc:
        console.step("read", f"failed: {console.error(exc)}")
        return None
    if response.status_code != 200:
        console.step("read", f"status={response.status_code}")
        return None
    lines = response.content.decode("utf-8", errors="replace").splitlines()
    first = shape(lines[0]) if lines else "(empty)"
    console.step("read", f"status=200 lines={len(lines)} first_line={first}")
    return response.content


def write_file(client: httpx.Client, server: str, path: str, content: bytes, console: Console) -> bool:
    """Writes the bytes back as a raw body and prints the status. True on a 204."""
    try:
        response = client.post(_files_path(server, "write"), params={"file": path},
                               content=content, headers={"Content-Type": "text/plain"})
    except httpx.HTTPError as exc:
        console.step("write", f"failed: {console.error(exc)}")
        return False
    console.step("write", f"status={response.status_code}")
    return response.status_code == 204


def check_resources(client: httpx.Client, server: str, console: Console) -> None:
    """Prints the server's state and whether its resources carry an uptime. Nothing else from the body is printed."""
    try:
        response = client.get(f"/api/client/servers/{server}/resources")
    except httpx.HTTPError as exc:
        console.step("resources", f"failed: {console.error(exc)}")
        return
    if response.status_code != 200:
        console.step("resources", f"status={response.status_code}")
        return
    try:
        attributes = response.json()["attributes"]
        state = attributes["current_state"]
        has_uptime = "uptime" in attributes["resources"]
    except (ValueError, KeyError, TypeError):
        console.step("resources", "unexpected body")
        return
    console.step("resources", f"current_state={state} uptime_present={has_uptime}")


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hand-run Pterodactyl file and resource checks on one file.")
    parser.add_argument("--path", required=True, help="the file's path inside the server, relative to its root")
    args = parser.parse_args(argv)

    try:
        base_url = required_env("PTERO_URL")
        key = required_env("PTERO_KEY")
        server = required_env("PTERO_SERVER")
    except LiveConfigError as exc:
        print(f"config error: {exc}")
        return 2

    console = Console([key, server, *instance_secrets(base_url)])
    client = make_client(base_url, key, transport=transport)
    try:
        content = read_file(client, server, args.path, console)
        if content is None:
            return 1
        ok = write_file(client, server, args.path, content, console)
        check_resources(client, server, console)
        return 0 if ok else 1
    except KeyboardInterrupt:
        console.line("interrupted")
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
