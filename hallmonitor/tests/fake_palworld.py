# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake Palworld REST API, served together with the Pterodactyl file routes through one httpx MockTransport.

The REST routes are `POST /v1/api/ban` and `POST /v1/api/unban`, with HTTP Basic auth as `admin` and the admin password.
A ban appends a `<userid>,<PlayerUId>` line to the ban-list file, the shape the live server writes (a 32-hex PlayerUId),
and an unban removes that userid's lines, so the Pterodactyl read sees both. The file routes are served by
`tests.fake_ptero_files.handle`, which owns the file text.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from tests.fake_ptero_files import FakePteroState
from tests.fake_ptero_files import handle as handle_ptero

PALWORLD_PASSWORD = "pal-admin-test"
SERVER = "palsrv1"
BANLIST_PATH = "banlist.txt"
REST_PREFIX = "/v1/api/"
ROUTES = ("ban", "unban")


@dataclass
class FakePalworldState:
    password: str = PALWORLD_PASSWORD
    requests: list[tuple[str, str, Any]] = field(default_factory=list)  # (method, path, parsed body) of REST calls
    bans_applied: list[str] = field(default_factory=list)                # userids the ban route accepted
    fail_next: dict[str, int] = field(default_factory=dict)              # route -> status, once each


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/json"})


def _basic(password: str) -> str:
    return "Basic " + base64.b64encode(f"admin:{password}".encode("utf-8")).decode("ascii")


def _body(request: httpx.Request) -> Any:
    try:
        return json.loads(request.content)
    except ValueError:
        return None


def _file_text(ptero: FakePteroState) -> str:
    return ptero.files.get((SERVER, BANLIST_PATH), "")


def player_uid(userid: str) -> str:
    """A stable 32-hex PlayerUId for a userid, standing in for the one the live server writes after the comma."""
    return hashlib.md5(userid.encode("utf-8")).hexdigest().upper()


def _userid_of(line: str) -> str:
    return line.split(",", 1)[0].strip()


def _with_line(text: str, userid: str) -> str:
    if userid in [_userid_of(line) for line in text.splitlines()]:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"{userid},{player_uid(userid)}\n"


def _without(text: str, userid: str) -> str:
    kept = [line for line in text.splitlines() if _userid_of(line) != userid]
    return "".join(line + "\n" for line in kept)


def moderator_ban(ptero: FakePteroState, userid: str) -> None:
    """Adds a ban line directly to the file, as a moderator's ban through the game would appear."""
    ptero.files[(SERVER, BANLIST_PATH)] = _with_line(_file_text(ptero), userid)


def handle_rest(state: FakePalworldState, ptero: FakePteroState, request: httpx.Request) -> httpx.Response | None:
    if not request.url.path.startswith(REST_PREFIX):
        return None
    route = request.url.path[len(REST_PREFIX):]
    body = _body(request)
    state.requests.append((request.method, request.url.path, body))
    if route not in ROUTES or request.method != "POST":
        return _json(404, {"error": "not found"})
    if request.headers.get("authorization") != _basic(state.password):
        return _json(401, {"error": "unauthorized"})
    if route in state.fail_next:
        return httpx.Response(state.fail_next.pop(route))
    userid = body.get("userid") if isinstance(body, dict) else None
    if not isinstance(userid, str):
        return _json(400, {"error": "userid is required"})
    if route == "ban":
        if not isinstance(body.get("message"), str):
            return _json(400, {"error": "message is required"})
        ptero.files[(SERVER, BANLIST_PATH)] = _with_line(_file_text(ptero), userid)
        state.bans_applied.append(userid)
    else:
        ptero.files[(SERVER, BANLIST_PATH)] = _without(_file_text(ptero), userid)
    return _json(200, {"ok": True})


def make_transport(pal_state: FakePalworldState, ptero_state: FakePteroState) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        response = handle_rest(pal_state, ptero_state, request)
        if response is None:
            response = handle_ptero(ptero_state, request)
        return response if response is not None else _json(404, {"error": "not found"})

    return httpx.MockTransport(handler)
