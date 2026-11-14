# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake hallpass monitor listener for tests, served through an httpx MockTransport.

It serves `GET /api/monitor/v1/desired` and `POST /api/monitor/v1/reports`, checks the bearer token, and records what
it receives. Bodies are kept as plain dicts, so a test compares them with the wire shape directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

TOKEN = "f" * 40
DESIRED_PATH = "/api/monitor/v1/desired"
REPORTS_PATH = "/api/monitor/v1/reports"


@dataclass
class FakeHallpassState:
    services: list[dict[str, Any]] = field(default_factory=list)   # the desired body's services, as served
    reports: list[dict[str, Any]] = field(default_factory=list)    # every received report entry, in order
    unmanaged: list[dict[str, Any]] = field(default_factory=list)  # every received unmanaged entry, in order
    token: str = TOKEN
    fail_desired: bool = False                                     # True makes the desired route unreachable
    fail_reports: bool = False                                     # True makes the reports route answer 500


def make_transport(state: FakeHallpassState) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {state.token}":
            return httpx.Response(401, json={"detail": "unauthorized"})
        if request.method == "GET" and request.url.path == DESIRED_PATH:
            if state.fail_desired:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"services": state.services})
        if request.method == "POST" and request.url.path == REPORTS_PATH:
            if state.fail_reports:
                return httpx.Response(500, json={"detail": "internal"})
            body = json.loads(request.content)
            state.reports.extend(body["reports"])
            state.unmanaged.extend(body["unmanaged"])
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)
