# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The parent's page: linked children, their status, and the approval that moves an awaiting child to active."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from hallpass import authentik_sync, desired, grants, holds, registry, terms, text
from hallpass.auth import current_person, require_csrf
from hallpass.holds import HoldRefused
from hallpass.registry import Person
from hallpass.web.pages import message_page, page
from hallpass.web.routes_me import service_rows

router = APIRouter()


def _children_context(conn, person: Person) -> dict:
    """One row per linked child. A parent may approve a child who is awaiting a grant, or who is invited and not yet
    approved; an invited child who is already approved waits for their first sign-in to become active, unless they are
    enrolled already, in which case the approval makes them active."""
    current = terms.current(conn)
    rows = []
    for child in registry.children_of(conn, person.id):
        child_grants = grants.grants_for(conn, child.id)
        can_approve = current is not None and grants.can_approve(child, has_counted_grant=bool(child_grants))
        rows.append({
            "child": child,
            "grants": child_grants,
            "can_approve": can_approve,
            # An enrolled child who is invited again is approved at once, so only a child not yet enrolled waits.
            "approved_waiting": child.status == "invited" and child.authentik_uuid is None and bool(child_grants),
            "ask": text.GRANT_ASK.format(child=child.display_name, version=current.version) if can_approve else "",
        })
    return {"rows": rows, "approved_waiting_text": text.GRANT_APPROVED_WAITING}


async def _children_page(request: Request, person: Person, *, status: int = 200, error: str | None = None) -> Response:
    context = await request.app.state.deps.db.run(lambda c: _children_context(c, person))
    return page(request, "children.html", viewer=person, status_code=status, error=error, **context)


@router.get("/children")
async def children_page(request: Request, person: Annotated[Person, Depends(current_person)]) -> Response:
    return await _children_page(request, person)


@router.post("/children/{child_id}/grant", dependencies=[Depends(require_csrf)])
async def grant_child(request: Request, child_id: int, person: Annotated[Person, Depends(current_person)]) -> Response:
    deps = request.app.state.deps
    now = deps.clock()

    def change(conn) -> None:
        grants.grant(conn, child_id=child_id, parent_id=person.id, now=now)

    try:
        await deps.db.run(change)
    except grants.GrantRefused as refused:
        return await _children_page(request, person, status=403, error=text.GRANT_REFUSED[refused.reason])
    await authentik_sync.sync_people(deps, [child_id])
    return RedirectResponse("/children", status_code=303)


def _child_context(conn, parent: Person, child_id: int, now: datetime) -> dict | None:
    """The child's service rows with the parent's controls, or None when the parent is not linked to that child.
    A parent may revoke where no hold of theirs stands, and restore only where they may lift a parent hold."""
    if child_id not in {child.id for child in registry.children_of(conn, parent.id)}:
        return None
    child = registry.get_person(conn, child_id)
    _, pauses = desired.refresh_with_pauses(conn, now)
    rows = service_rows(conn, child, pauses)
    held = holds.services_with_holds(conn, child.id)
    for row in rows:
        liftable = holds.liftable_parent_holds(child.conflict_mode, parent.id, held.get(row["service_id"], []))
        row["revoke"] = not liftable
        row["restore"] = row["auto_suspendable"] and bool(liftable)
    return {"child": child, "rows": rows, "controls_for": child.id}


async def _child_page(request: Request, person: Person, child_id: int, *, status: int = 200,
                      error: str | None = None) -> Response:
    deps = request.app.state.deps
    context = await deps.db.run(lambda c: _child_context(c, person, child_id, deps.clock()))
    if context is None:
        return message_page(request, error or text.FORBIDDEN, status)
    return page(request, "child.html", viewer=person, status_code=status, error=error, **context)


@router.get("/children/{child_id}")
async def child_page(request: Request, child_id: int, person: Annotated[Person, Depends(current_person)]) -> Response:
    """A linked parent's view of a child's services, with the revoke and restore controls. It shows no tokens and
    no feed forms."""
    deps = request.app.state.deps
    context = await deps.db.run(lambda c: _child_context(c, person, child_id, deps.clock()))
    if context is None:
        raise HTTPException(403, text.FORBIDDEN)
    return page(request, "child.html", viewer=person, **context)


async def _apply_hold_change(request: Request, person: Person, child_id: int, change) -> Response:
    """Runs one hold change, then syncs the child's Authentik state. A refusal re-renders the child page at 403."""
    deps = request.app.state.deps
    try:
        await deps.db.run(change)
    except HoldRefused as refused:
        return await _child_page(request, person, child_id, status=403, error=refused.message)
    await authentik_sync.sync_people(deps, [child_id])
    return RedirectResponse(f"/children/{child_id}", status_code=303)


@router.post("/children/{child_id}/holds", dependencies=[Depends(require_csrf)])
async def place_child_hold(request: Request, child_id: int, person: Annotated[Person, Depends(current_person)],
                           service_id: Annotated[str, Form()] = "") -> Response:
    """Revokes one service (its id) or everything the child has (`all`)."""
    deps = request.app.state.deps
    now = deps.clock()
    window = timedelta(hours=deps.config.conflict_window_hours)
    try:
        scope = holds.service_scope(service_id)
    except ValueError:
        return await _child_page(request, person, child_id, status=400, error=text.INVALID_INPUT)

    def change(conn) -> None:
        holds.place(conn, actor=holds.Actor("parent", person.id), target_id=child_id, service_ids=scope, now=now,
                    window=window, admin_email=deps.config.admin_email)

    return await _apply_hold_change(request, person, child_id, change)


@router.post("/children/{child_id}/holds/{service_id}/lift", dependencies=[Depends(require_csrf)])
async def lift_child_hold(request: Request, child_id: int, service_id: int,
                          person: Annotated[Person, Depends(current_person)]) -> Response:
    """Restores one service: lifts the parent holds this parent may lift on it."""
    deps = request.app.state.deps
    now = deps.clock()
    window = timedelta(hours=deps.config.conflict_window_hours)

    def change(conn) -> None:
        holds.lift(conn, actor=holds.Actor("parent", person.id), target_id=child_id, service_ids=[service_id],
                   now=now, window=window, admin_email=deps.config.admin_email)

    return await _apply_hold_change(request, person, child_id, change)
