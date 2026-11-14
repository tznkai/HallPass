# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client for hallpass's monitor API: fetch the desired state, post the reports. It holds only the monitor token."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx

from hallmonitor.contract import ContractError, Report, ServiceDesired, parse_desired, reports_body

DESIRED_PATH = "/api/monitor/v1/desired"
REPORTS_PATH = "/api/monitor/v1/reports"
TIMEOUT_SECONDS = 30.0


class MonitorError(Exception):
    """`reason` is `unreachable`, `http` (with the status) or `contract` (a body that does not match)."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class MonitorClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_desired(self) -> list[ServiceDesired]:
        try:
            response = await self._http.get(DESIRED_PATH)
        except httpx.HTTPError:
            raise MonitorError("unreachable") from None
        if response.status_code != 200:
            raise MonitorError("http", response.status_code)
        try:
            return parse_desired(response.json())
        except (ValueError, ContractError):
            raise MonitorError("contract") from None

    async def post_reports(self, reports: Sequence[Report], unmanaged: Mapping[str, int]) -> None:
        body = reports_body(reports, unmanaged)
        try:
            response = await self._http.post(REPORTS_PATH, json=body)
        except httpx.HTTPError:
            raise MonitorError("unreachable") from None
        if response.status_code != 204:
            raise MonitorError("http", response.status_code)
