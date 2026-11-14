# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake Fluxer admin API for tests, served through an httpx MockTransport.

It serves exactly the routes the adapter may call, with the shapes recorded in docs/service-notes.md. There are no ban,
enforcement-decision or flags routes: any request to a path it does not serve is recorded and answered 404, so a
test can see that the adapter tried one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

FLUXER_KEY = "k" * 40
DISABLED = 1 << 38
HAS_SESSION_STARTED = 1 << 39
AUDIT_HEADER = "X-Audit-Log-Reason"
# Decoy values a real user object carries. The adapter must never return, log or store them.
DECOY_EMAIL = "decoy-address@mail.invalid"
DECOY_IP = ".".join(["198", "51", "100", "7"])   # built from parts so the file holds no dotted address

USER_PATH = re.compile(r"/v1/admin/users/([0-9]+)")
SESSIONS_PATH = re.compile(r"/v1/admin/users/([0-9]+)/sessions")
SSO_PATH = "/v1/admin/sso-identities"


@dataclass
class FakeFluxerAdminState:
    key: str = FLUXER_KEY
    users: dict[str, dict[str, Any]] = field(default_factory=dict)            # id -> {"id", "flags", decoys}
    sso: dict[tuple[str, str], str] = field(default_factory=dict)             # (provider_id, subject) -> user id
    sessions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)   # user id -> [{"id", "deleted_at"}]
    requests: list[tuple[str, str]] = field(default_factory=list)             # (method, path with query)
    fail_next: dict[str, int] = field(default_factory=dict)                   # route name -> status, once each
    malformed_next: set[str] = field(default_factory=set)                     # route names whose next 200 is not JSON
    keep_sessions: bool = False                                               # DELETE answers 200 but ends nothing


def add_user(state: FakeFluxerAdminState, user_id: str, flags: int = HAS_SESSION_STARTED) -> None:
    state.users[user_id] = {"id": user_id, "flags": str(flags), "email": DECOY_EMAIL, "last_active_ip": DECOY_IP}
    state.sessions.setdefault(user_id, [])


def link_sso(state: FakeFluxerAdminState, provider_id: str, subject: str, user_id: str) -> None:
    state.sso[(provider_id, subject)] = user_id


def external_ban(state: FakeFluxerAdminState, user_id: str) -> None:
    """Sets the DISABLED bit, as a moderator's ban would. The adapter is never meant to do this."""
    user = state.users[user_id]
    user["flags"] = str(int(user["flags"]) | DISABLED)


def sign_in(state: FakeFluxerAdminState, user_id: str) -> None:
    sessions = state.sessions.setdefault(user_id, [])
    sessions.append({"id": f"session-{len(sessions) + 1}", "deleted_at": None})


def active_sessions(state: FakeFluxerAdminState, user_id: str) -> int:
    return sum(1 for session in state.sessions.get(user_id, []) if session["deleted_at"] is None)


def mutations(state: FakeFluxerAdminState) -> list[tuple[str, str]]:
    return [request for request in state.requests if request[0] != "GET"]


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/json"})


def make_transport(state: FakeFluxerAdminState) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode("ascii") if request.url.query else ""
        state.requests.append((request.method, f"{request.url.path}?{query}" if query else request.url.path))
        if request.headers.get("authorization") != f"Admin {state.key}":
            return _json(401, {"message": "unauthorized"})
        path = request.url.path
        if request.method == "GET" and path == SSO_PATH:
            return _sso_lookup(state, request)
        user_match = USER_PATH.fullmatch(path)
        if request.method == "GET" and user_match:
            return _get_user(state, user_match.group(1))
        sessions_match = SESSIONS_PATH.fullmatch(path)
        if sessions_match:
            user_id = sessions_match.group(1)
            if request.method == "GET":
                return _list_sessions(state, user_id)
            if request.method == "DELETE":
                return _end_sessions(state, user_id, request)
        return _json(404, {"message": "not found"})

    return httpx.MockTransport(handler)


def _take_failure(state: FakeFluxerAdminState, route: str) -> httpx.Response | None:
    """The fault for the next call on `route`, if one is armed: a non-JSON 200, or the status in fail_next."""
    if route in state.malformed_next:
        state.malformed_next.discard(route)
        return httpx.Response(200, content=b"<html>not json</html>", headers={"content-type": "text/html"})
    if route in state.fail_next:
        return _json(state.fail_next.pop(route), {"message": "failed"})
    return None


def _sso_lookup(state: FakeFluxerAdminState, request: httpx.Request) -> httpx.Response:
    failure = _take_failure(state, "sso")
    if failure is not None:
        return failure
    provider_id = request.url.params.get("provider_id")
    subject = request.url.params.get("subject")
    if provider_id is None or subject is None:
        return _json(400, {"message": "provider_id and subject are required"})
    user_id = state.sso.get((provider_id, subject))
    if user_id is None:
        return _json(200, {"sso_identity": None})
    identity = {"provider_id": provider_id, "subject": subject, "user_id": user_id,
                "claimed_at": "2026-11-13T12:00:00Z"}
    return _json(200, {"sso_identity": identity})


def _get_user(state: FakeFluxerAdminState, user_id: str) -> httpx.Response:
    failure = _take_failure(state, "user")
    if failure is not None:
        return failure
    if user_id not in state.users:
        return _json(404, {"message": "unknown user"})
    return _json(200, {"users": [state.users[user_id]]})


def _list_sessions(state: FakeFluxerAdminState, user_id: str) -> httpx.Response:
    failure = _take_failure(state, "sessions")
    if failure is not None:
        return failure
    return _json(200, {"sessions": state.sessions.get(user_id, [])})


def _end_sessions(state: FakeFluxerAdminState, user_id: str, request: httpx.Request) -> httpx.Response:
    if not request.headers.get(AUDIT_HEADER):
        return _json(400, {"message": "audit log reason required"})
    failure = _take_failure(state, "sessions_delete")
    if failure is not None:
        return failure
    if state.keep_sessions:
        return _json(200, {"terminated_count": 0})
    terminated = 0
    for session in state.sessions.get(user_id, []):
        if session["deleted_at"] is None:
            session["deleted_at"] = "2026-11-13T12:00:00Z"
            terminated += 1
    return _json(200, {"terminated_count": terminated})
