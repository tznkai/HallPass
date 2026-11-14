# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The parent's page: linked children, their status, and the approval that moves an awaiting child to active."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from hallpass import authentik_sync, grants, registry, terms, text
from hallpass.auth import current_person, require_csrf
from hallpass.registry import Person
from hallpass.web.pages import page
from hallpass.web.routes_me import service_rows

router = APIRouter()


def _children_context(conn, person: Person) -> dict:
    rows = [{"child": child, "grants": grants.grants_for(conn, child.id)}
            for child in registry.children_of(conn, person.id)]
    return {"rows": rows, "current": terms.current(conn)}


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


def _child_context(conn, parent: Person, child_id: int) -> dict | None:
    """The child's service rows, or None when the parent is not linked to that child."""
    if child_id not in {child.id for child in registry.children_of(conn, parent.id)}:
        return None
    child = registry.get_person(conn, child_id)
    return {"child": child, "rows": service_rows(conn, child)}


@router.get("/children/{child_id}")
async def child_page(request: Request, child_id: int, person: Annotated[Person, Depends(current_person)]) -> Response:
    """A linked parent's view of a child's services. It shows no tokens and no feed forms."""
    context = await request.app.state.deps.db.run(lambda c: _child_context(c, person, child_id))
    if context is None:
        raise HTTPException(403, text.FORBIDDEN)
    return page(request, "child.html", viewer=person, **context)
