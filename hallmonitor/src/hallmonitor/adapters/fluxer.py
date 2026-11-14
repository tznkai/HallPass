# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Fluxer adapter. Hallmonitor never bans a Fluxer account, and never lifts a ban or changes a flag.

A Fluxer hold is three things, and this adapter does only the last one:
  1. hallpass removes the person from Authentik's Fluxer group;
  2. Fluxer "Require SSO" stops any other way in;
  3. this adapter ends the person's Fluxer sessions, the only mutating call (DELETE .../sessions).

Restoring needs no call: hallpass puts the person back in the group. The paths and shapes come from
docs/service-notes.md (Fluxer Admin API and Design changes).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import httpx

from hallmonitor.adapters.base import AdapterError, Capabilities, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.contract import Subject
from hallmonitor.logs import log_event

log = logging.getLogger(__name__)
ADMIN_PREFIX = "/v1/admin"
DISABLED = 1 << 38            # set by a ban, which only someone else places; read here, never written
AUDIT_HEADER = "X-Audit-Log-Reason"
AUDIT_REASON = "hallmonitor"
SSO_SUBJECT = "sso_subject"
FLUXER_USER_ID = "fluxer_user_id"
REQUEST_TIMEOUT_SECONDS = 30.0


def _first(subject: Subject, kind: str) -> str | None:
    return next((identity.value for identity in subject.identities if identity.kind == kind), None)


def _has_identity(subject: Subject) -> bool:
    return _first(subject, SSO_SUBJECT) is not None or _first(subject, FLUXER_USER_ID) is not None


def _json_body(response: httpx.Response) -> Any:
    """The JSON of a 2xx response. Any other status is an `http` failure."""
    if not 200 <= response.status_code < 300:
        raise AdapterError("http")
    try:
        return response.json()
    except ValueError:
        raise AdapterError("unexpected") from None


def _user(body: Any) -> dict[str, Any]:
    """The user from a `{"users": [user]}` body or a bare user object. Only `id` and `flags` are kept, so no other
    field of the user (names, emails, addresses) can be logged, stored or returned."""
    raw = body
    if isinstance(body, dict) and isinstance(body.get("users"), list):
        users = [entry for entry in body["users"] if isinstance(entry, dict)]
        raw = users[0] if users else None
    if not isinstance(raw, dict) or "id" not in raw or "flags" not in raw:
        raise AdapterError("unexpected")
    return {"id": str(raw["id"]), "flags": raw["flags"]}


def _is_disabled(flags: Any) -> bool:
    """`flags` is a string holding a 64-bit integer. It is parsed as an int, never as a float."""
    try:
        return bool(int(flags) & DISABLED)
    except (TypeError, ValueError):
        raise AdapterError("unexpected") from None


def _session_entries(body: Any) -> list[Any]:
    if not isinstance(body, dict) or not isinstance(body.get("sessions"), list):
        raise AdapterError("unexpected")
    return body["sessions"]


class FluxerAdapter:
    capabilities = Capabilities(suspend=True, restore=True, read_state=True, removes_connected=True)

    def __init__(self, *, http: httpx.AsyncClient, base_url: str, key: str, sso_provider_id: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._key = key
        self._provider_id = sso_provider_id

    async def _send(self, method: str, path: str, *, params: dict[str, str] | None = None,
                    headers: dict[str, str] | None = None) -> httpx.Response:
        """One request. A transport failure is `unreachable`; a 401 or 403 is `auth`. Other statuses are the caller's."""
        request_headers = {"Authorization": f"Admin {self._key}", **(headers or {})}
        try:
            response = await self._http.request(method, f"{self._base}{ADMIN_PREFIX}{path}", params=params,
                                                headers=request_headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            raise AdapterError("unreachable") from None
        if response.status_code in (401, 403):
            raise AdapterError("auth")
        return response

    async def _user_id(self, subject: Subject) -> str | None:
        """The Fluxer user id. A `fluxer_user_id` identity is an admin override and needs no lookup. Otherwise the
        Authentik uuid is looked up under the configured provider; a null identity means no Fluxer sign-in yet."""
        override = _first(subject, FLUXER_USER_ID)
        if override is not None:
            return override
        subject_value = _first(subject, SSO_SUBJECT)
        response = await self._send("GET", "/sso-identities",
                                    params={"provider_id": self._provider_id, "subject": subject_value or ""})
        body = _json_body(response)
        if not isinstance(body, dict) or "sso_identity" not in body:
            raise AdapterError("unexpected")
        identity = body["sso_identity"]
        if identity is None:
            return None
        if not isinstance(identity, dict) or "user_id" not in identity:
            raise AdapterError("unexpected")
        return str(identity["user_id"])

    async def _user_flags(self, user_id: str) -> tuple[bool, bool]:
        """(found, disabled). A 404 is an unknown user; any other failure raises."""
        response = await self._send("GET", f"/users/{quote(user_id, safe='')}")
        if response.status_code == 404:
            return False, False
        return True, _is_disabled(_user(_json_body(response))["flags"])

    async def _active_session_count(self, user_id: str) -> int:
        """Sessions whose `deleted_at` is null. Ended sessions stay listed, so they are not counted."""
        response = await self._send("GET", f"/users/{quote(user_id, safe='')}/sessions")
        entries = _session_entries(_json_body(response))
        return sum(1 for entry in entries if isinstance(entry, dict) and entry.get("deleted_at") is None)

    async def _observe_one(self, subject: Subject) -> Observed:
        if not _has_identity(subject):
            return Observed("error", "no_identity")
        user_id = await self._user_id(subject)
        if user_id is None:
            return Observed(subject.desired, "no_account")  # type: ignore[arg-type]
        found, disabled = await self._user_flags(user_id)
        if not found:
            return Observed("error", "unknown_user")
        if disabled:
            return Observed("banned_external")
        if subject.desired == "allowed":
            return Observed("allowed")
        return Observed("allowed" if await self._active_session_count(user_id) else "suspended")

    async def observe(self, subjects: Sequence[Subject]) -> dict[str, Observed]:
        return {subject.ref: await self._observe_one(subject) for subject in subjects}

    async def _end_sessions(self, user_id: str) -> bool:
        """The only mutating call. True when the server answered 2xx."""
        try:
            response = await self._send("DELETE", f"/users/{quote(user_id, safe='')}/sessions",
                                        headers={AUDIT_HEADER: AUDIT_REASON})
        except AdapterError:
            return False
        return 200 <= response.status_code < 300

    async def apply(self, subject: Subject, desired: str) -> Observed:
        if desired == "allowed":
            return Observed("allowed")          # restoring is hallpass putting the person back in the group
        if desired != "suspended":
            raise ValueError("desired must be allowed or suspended")
        if not _has_identity(subject):
            return Observed("error", "no_identity")
        user_id = await self._user_id(subject)
        if user_id is None:
            return Observed("suspended", "no_account")
        ended = await self._end_sessions(user_id)
        if not ended:
            log_event(log, "sessions_not_ended", service="fluxer", outcome="delete_failed", level=logging.WARNING)
            return Observed("error", "sessions_not_ended")
        if await self._active_session_count(user_id):
            return Observed("error", "sessions_not_ended")
        return Observed("suspended")

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
        return None   # Fluxer users cannot be enumerated cheaply


def create(ctx: AdapterContext) -> FluxerAdapter | None:
    provider_id = ctx.settings.get("sso_provider_id")
    url = ctx.config.fluxer_admin_url
    key = ctx.config.fluxer_admin_key
    if not (url and key and provider_id):
        log_event(log, "adapter_not_configured", adapter="fluxer", service=ctx.slug)
        return None
    return FluxerAdapter(http=ctx.http, base_url=url, key=key, sso_provider_id=provider_id)
