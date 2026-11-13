# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small async client for the Authentik API paths hallpass uses.

Errors carry a reason and an HTTP status, never a response body or a URL, so they are safe to log and to show.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from hallpass.logs import log_event

logger = logging.getLogger(__name__)

FLOWS = "/api/v3/flows/instances/"
INVITATIONS = "/api/v3/stages/invitation/invitations/"
USERS = "/api/v3/core/users/"
SESSIONS = "/api/v3/core/authenticated_sessions/"
GROUPS = "/api/v3/core/groups/"


class AuthentikError(Exception):
    """reason is "http" (status set), "unreachable" or "not_found"."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        self.reason = reason
        self.status = status
        detail = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"Authentik request failed: {reason}{detail}")


@dataclass(frozen=True)
class Invitation:
    pk: str
    url: str


@dataclass(frozen=True)
class AuthentikUser:
    pk: int
    uuid: str
    username: str
    is_active: bool
    attributes: dict


def _user(data: Mapping[str, Any]) -> AuthentikUser:
    return AuthentikUser(pk=int(data["pk"]), uuid=str(data["uuid"]), username=str(data["username"]),
                         is_active=bool(data["is_active"]), attributes=dict(data.get("attributes") or {}))


class AuthentikClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base, headers={"Authorization": f"Bearer {token}"},
                                       transport=transport, timeout=timeout)
        self._flow_pks: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, action: str, method: str, path: str, *, params: Mapping[str, str] | None = None,
                    body: Mapping[str, Any] | None = None) -> httpx.Response:
        try:
            response = await self._http.request(method, path, params=params, json=body)
        except httpx.HTTPError:
            # The transport error text can carry the host; it is dropped here, not logged or re-raised.
            log_event(logger, "authentik_call", action=action, outcome="unreachable")
            raise AuthentikError("unreachable") from None
        if not response.is_success:
            log_event(logger, "authentik_call", action=action, http_status=response.status_code, outcome="http")
            raise AuthentikError("http", response.status_code)
        log_event(logger, "authentik_call", action=action, http_status=response.status_code, outcome="ok")
        return response

    async def _flow_pk(self, slug: str) -> str:
        if slug not in self._flow_pks:
            response = await self._call("flow_lookup", "GET", FLOWS, params={"slug": slug})
            match = next((r for r in response.json().get("results", []) if r.get("slug") == slug), None)
            if match is None:
                raise AuthentikError("not_found")
            self._flow_pks[slug] = str(match["pk"])
        return self._flow_pks[slug]

    async def create_invitation(self, *, name: str, flow_slug: str, enrollment_ref: str,
                                expires: datetime) -> Invitation:
        flow_pk = await self._flow_pk(flow_slug)
        response = await self._call("create_invitation", "POST", INVITATIONS, body={
            "name": name,
            "expires": expires.isoformat(),
            "single_use": True,
            "flow": flow_pk,
            "fixed_data": {"attributes.hallpass_enrollment": enrollment_ref},
        })
        pk = str(response.json()["pk"])
        return Invitation(pk=pk, url=f"{self._base}/if/flow/{flow_slug}/?itoken={pk}")

    async def delete_invitation(self, pk: str) -> None:
        try:
            await self._call("delete_invitation", "DELETE", f"{INVITATIONS}{pk}/")
        except AuthentikError as error:
            if error.status != 404:
                raise

    async def user_by_uuid(self, uuid: str) -> AuthentikUser:
        # A blank uuid would make the filter match nothing useful; refuse it before any request.
        if not uuid:
            raise ValueError("uuid is required")
        response = await self._call("user_by_uuid", "GET", USERS, params={"uuid": uuid})
        results = response.json().get("results", [])
        if not results:
            raise AuthentikError("not_found")
        return _user(results[0])

    async def set_active(self, pk: int, active: bool) -> None:
        await self._call("set_active", "PATCH", f"{USERS}{pk}/", body={"is_active": active})

    async def end_sessions(self, pk: int) -> int:
        """Delete one user's sessions. Refuses anything but a positive int before any request, so a missing pk
        can never reach Authentik as "all users"."""
        if isinstance(pk, bool) or not isinstance(pk, int) or pk <= 0:
            raise ValueError("pk must be a positive integer")
        response = await self._call("end_sessions", "DELETE", f"{SESSIONS}bulk_delete/",
                                    params={"user_pks": str(pk)})
        return int(response.json()["deleted"])

    async def update_attributes(self, pk: int, updates: Mapping[str, object | None]) -> None:
        """Merge into the user's attributes. A None value deletes the key. PATCH replaces the whole dict,
        so the merged dict is sent in full."""
        response = await self._call("get_user", "GET", f"{USERS}{pk}/")
        attributes = dict(_user(response.json()).attributes)
        for key, value in updates.items():
            if value is None:
                attributes.pop(key, None)
            else:
                attributes[key] = value
        await self._call("update_attributes", "PATCH", f"{USERS}{pk}/", body={"attributes": attributes})

    async def add_to_group(self, group_uuid: str, pk: int) -> None:
        await self._call("add_to_group", "POST", f"{GROUPS}{group_uuid}/add_user/", body={"pk": pk})

    async def remove_from_group(self, group_uuid: str, pk: int) -> None:
        await self._call("remove_from_group", "POST", f"{GROUPS}{group_uuid}/remove_user/", body={"pk": pk})
