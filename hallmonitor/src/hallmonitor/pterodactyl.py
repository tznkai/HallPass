# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small client for the Pterodactyl Client API: a server's files and its resources. The game adapters use it to read and
write their ban-list files, and to learn how long a server has run since a change.

Every failure raises PteroError with a fixed reason: auth (401 or 403), not_found (404), http (any other status, or a
reply that is not the expected shape) or unreachable (a transport failure). A message never carries the key, the file
text or the server id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

Reason = Literal["unreachable", "auth", "not_found", "http"]
REQUEST_TIMEOUT_SECONDS = 30.0
MILLISECONDS_PER_SECOND = 1000
RUNNING = "running"


class PteroError(Exception):
    """A failed Pterodactyl call. `reason` is one of unreachable, auth, not_found or http."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason = reason


def _check_status(response: httpx.Response) -> None:
    status = response.status_code
    if status in (401, 403):
        raise PteroError("auth")
    if status == 404:
        raise PteroError("not_found")
    if not 200 <= status < 300:
        raise PteroError("http")


def _attributes(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise PteroError("http") from None
    if not isinstance(body, dict) or not isinstance(body.get("attributes"), dict):
        raise PteroError("http")
    return body["attributes"]


@dataclass
class PteroFiles:
    base_url: str
    key: str = field(repr=False)
    server: str = field(repr=False)
    http: httpx.AsyncClient = field(repr=False, kw_only=True)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    async def _request(self, method: str, action: str, *, params: dict[str, str] | None = None,
                       headers: dict[str, str] | None = None, content: bytes | None = None) -> httpx.Response:
        request_headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json", **(headers or {})}
        try:
            response = await self.http.request(method, f"{self.base_url}/api/client/servers/{self.server}{action}",
                                               params=params, headers=request_headers, content=content,
                                               timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            raise PteroError("unreachable") from None
        _check_status(response)
        return response

    async def read(self, path: str) -> str:
        """The text of one file in the server."""
        response = await self._request("GET", "/files/contents", params={"file": path})
        return response.text

    async def write(self, path: str, text: str) -> None:
        """Replaces one file's text. The server answers 204 on success; any other 2xx is an `http` failure."""
        response = await self._request("POST", "/files/write", params={"file": path},
                                       headers={"Content-Type": "text/plain"}, content=text.encode("utf-8"))
        if response.status_code != 204:
            raise PteroError("http")

    async def uptime_seconds(self) -> float | None:
        """Seconds the server has been running, or None when it is not running. Pterodactyl reports the uptime in
        milliseconds, so it is divided here."""
        attributes = _attributes(await self._request("GET", "/resources"))
        if attributes.get("current_state") != RUNNING:
            return None
        try:
            uptime_ms = attributes["resources"]["uptime"]
        except (KeyError, TypeError):
            raise PteroError("http") from None
        if not isinstance(uptime_ms, int) or isinstance(uptime_ms, bool):
            raise PteroError("http")
        return uptime_ms / MILLISECONDS_PER_SECOND
