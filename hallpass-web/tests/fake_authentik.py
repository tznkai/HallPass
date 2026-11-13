# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An in-process fake of the Authentik API paths hallpass uses.

Behaviour follows the live checks recorded in docs/authentik-notes.md: PATCH replaces `attributes` as a whole,
the authenticated-sessions list ignores `user`, deactivation ends a user's sessions, and bulk_delete is per user
and refuses a missing or empty `user_pks`.
"""

from __future__ import annotations

import uuid as uuid_lib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

TOKEN = "test-token"
ATTRIBUTE_PREFIX = "attributes."


class InvitationUsed(Exception):
    """The invitation is gone: it was used (single use) or never existed."""


@dataclass
class FakeAuthentikState:
    users: dict[int, dict] = field(default_factory=dict)
    invitations: dict[str, dict] = field(default_factory=dict)
    flows: dict[str, str] = field(default_factory=lambda: {"invite": "flow-1"})
    groups: dict[str, set[int]] = field(default_factory=dict)
    sessions: dict[int, list[str]] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    fail_next: int | None = None


def add_user(state: FakeAuthentikState, *, uuid: str, username: str, attributes: dict | None = None) -> dict:
    """Add a user directly, as an admin would, without an invitation."""
    pk = max(state.users, default=0) + 1
    user = {"pk": pk, "uuid": uuid, "username": username, "name": username, "is_active": True,
            "attributes": dict(attributes or {})}
    state.users[pk] = user
    return user


def enrol(state: FakeAuthentikState, invitation_pk: str, username: str) -> dict:
    """A person follows the invitation link: a new user is created with the invitation's fixed attributes."""
    invitation = state.invitations.get(invitation_pk)
    if invitation is None:
        raise InvitationUsed(invitation_pk)
    attributes = {key[len(ATTRIBUTE_PREFIX):]: value
                  for key, value in invitation["fixed_data"].items() if key.startswith(ATTRIBUTE_PREFIX)}
    user = add_user(state, uuid=str(uuid_lib.uuid4()), username=username, attributes=attributes)
    if invitation["single_use"]:
        del state.invitations[invitation_pk]
    return user


def make_app(state: FakeAuthentikState) -> Starlette:
    def guard(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def wrapped(request: Request) -> Response:
            query = request.url.query
            state.requests.append((request.method, request.url.path + (f"?{query}" if query else "")))
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                return JSONResponse({"detail": "forbidden"}, status_code=403)
            if state.fail_next is not None:
                status, state.fail_next = state.fail_next, None
                return JSONResponse({"detail": "injected failure"}, status_code=status)
            return await handler(request)
        return wrapped

    async def list_flows(request: Request) -> Response:
        slug = request.query_params.get("slug", "")
        results = [{"pk": state.flows[slug], "slug": slug}] if slug in state.flows else []
        return JSONResponse({"results": results})

    async def create_invitation(request: Request) -> Response:
        body = await request.json()
        pk = str(uuid_lib.uuid4())
        state.invitations[pk] = {"pk": pk, "name": body["name"], "single_use": bool(body.get("single_use")),
                                 "fixed_data": dict(body.get("fixed_data") or {}), "flow": body["flow"]}
        return JSONResponse(state.invitations[pk], status_code=201)

    async def delete_invitation(request: Request) -> Response:
        pk = request.path_params["pk"]
        if pk not in state.invitations:
            return JSONResponse({"detail": "not found"}, status_code=404)
        del state.invitations[pk]
        return Response(status_code=204)

    async def list_users(request: Request) -> Response:
        uuid = request.query_params.get("uuid")
        results = [user for user in state.users.values() if user["uuid"] == uuid]
        return JSONResponse({"results": results})

    def _user_pk(request: Request) -> int | None:
        try:
            pk = int(request.path_params["pk"])
        except ValueError:
            return None
        return pk if pk in state.users else None

    async def get_user(request: Request) -> Response:
        pk = _user_pk(request)
        if pk is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return JSONResponse(state.users[pk])

    async def patch_user(request: Request) -> Response:
        pk = _user_pk(request)
        if pk is None:
            return JSONResponse({"detail": "not found"}, status_code=404)
        body = await request.json()
        user = state.users[pk]
        for key, value in body.items():
            if key not in ("pk", "uuid"):
                user[key] = value              # replaces the key as a whole, as PATCH does in Authentik
        if body.get("is_active") is False:
            state.sessions[pk] = []            # Authentik's post_save handler ends sessions of a deactivated user
        return JSONResponse(user)

    async def list_sessions(request: Request) -> Response:
        # Real Authentik ignores `?user=` here and returns every user's sessions.
        results = [{"uuid": sid, "user": pk} for pk, sids in state.sessions.items() for sid in sids]
        return JSONResponse({"results": results})

    async def bulk_delete_sessions(request: Request) -> Response:
        values = request.query_params.getlist("user_pks")
        if not values or not all(value.isdigit() for value in values):
            return JSONResponse({"detail": "user_pks is required"}, status_code=400)
        deleted = 0
        for pk in {int(value) for value in values}:
            deleted += len(state.sessions.pop(pk, []))
        return JSONResponse({"deleted": deleted})

    async def add_user_to_group(request: Request) -> Response:
        group = request.path_params["group"]
        if group not in state.groups:
            return JSONResponse({"detail": "not found"}, status_code=404)
        state.groups[group].add(int((await request.json())["pk"]))
        return Response(status_code=204)

    async def remove_user_from_group(request: Request) -> Response:
        group = request.path_params["group"]
        if group not in state.groups:
            return JSONResponse({"detail": "not found"}, status_code=404)
        state.groups[group].discard(int((await request.json())["pk"]))
        return Response(status_code=204)

    routes = [
        Route("/api/v3/flows/instances/", guard(list_flows), methods=["GET"]),
        Route("/api/v3/stages/invitation/invitations/", guard(create_invitation), methods=["POST"]),
        Route("/api/v3/stages/invitation/invitations/{pk}/", guard(delete_invitation), methods=["DELETE"]),
        Route("/api/v3/core/users/", guard(list_users), methods=["GET"]),
        Route("/api/v3/core/users/{pk}/", guard(get_user), methods=["GET"]),
        Route("/api/v3/core/users/{pk}/", guard(patch_user), methods=["PATCH"]),
        Route("/api/v3/core/authenticated_sessions/", guard(list_sessions), methods=["GET"]),
        Route("/api/v3/core/authenticated_sessions/bulk_delete/", guard(bulk_delete_sessions), methods=["DELETE"]),
        Route("/api/v3/core/groups/{group}/add_user/", guard(add_user_to_group), methods=["POST"]),
        Route("/api/v3/core/groups/{group}/remove_user/", guard(remove_user_from_group), methods=["POST"]),
    ]
    return Starlette(routes=routes)
