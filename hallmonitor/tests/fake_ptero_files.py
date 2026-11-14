# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake Pterodactyl Client API for the file and resource routes, served through an httpx MockTransport.

`handle(state, request)` answers the three routes a server's files and resources need, and returns None for any path
it does not own, so a fake that serves more than one API can compose this handler with its own. `make_transport`
serves this handler alone, with a 404 for anything else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

PTERO_KEY = "p" * 40
ROUTE = re.compile(r"/api/client/servers/([^/]+)/(files/contents|files/write|resources)")
ROUTE_NAMES = {"files/contents": "contents", "files/write": "write", "resources": "resources"}


@dataclass
class FakePteroState:
    files: dict[tuple[str, str], str] = field(default_factory=dict)   # (server, path) -> text
    uptime_ms: int = 0                                                 # resources.uptime, in milliseconds
    state: str = "running"                                             # attributes.current_state
    key: str = PTERO_KEY
    requests: list[tuple[str, str]] = field(default_factory=list)      # (method, path) of the routes it owns
    fail_next: dict[str, int] = field(default_factory=dict)            # route name -> status, once each


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/json"})


def _error(status: int, code: str) -> httpx.Response:
    return _json(status, {"errors": [{"code": code}]})


def handle(state: FakePteroState, request: httpx.Request) -> httpx.Response | None:
    match = ROUTE.fullmatch(request.url.path)
    if match is None:
        return None
    server, route = match.group(1), ROUTE_NAMES[match.group(2)]
    state.requests.append((request.method, request.url.path))
    if request.headers.get("authorization") != f"Bearer {state.key}":
        return _error(401, "AuthenticationException")
    if route in state.fail_next:
        return httpx.Response(state.fail_next.pop(route))
    if route == "contents" and request.method == "GET":
        return _read(state, server, request)
    if route == "write" and request.method == "POST":
        return _write(state, server, request)
    if route == "resources" and request.method == "GET":
        return _resources(state)
    return _error(405, "MethodNotAllowedHttpException")


def _read(state: FakePteroState, server: str, request: httpx.Request) -> httpx.Response:
    path = request.url.params.get("file", "")
    if (server, path) not in state.files:
        return _error(404, "NotFoundHttpException")
    return httpx.Response(200, text=state.files[(server, path)], headers={"content-type": "text/plain"})


def _write(state: FakePteroState, server: str, request: httpx.Request) -> httpx.Response:
    path = request.url.params.get("file", "")
    state.files[(server, path)] = request.content.decode("utf-8")
    return httpx.Response(204)


def _resources(state: FakePteroState) -> httpx.Response:
    attributes = {"current_state": state.state, "resources": {"uptime": state.uptime_ms}}
    return _json(200, {"object": "stats", "attributes": attributes})


def make_transport(state: FakePteroState) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        response = handle(state, request)
        return response if response is not None else _error(404, "NotFoundHttpException")

    return httpx.MockTransport(handler)
