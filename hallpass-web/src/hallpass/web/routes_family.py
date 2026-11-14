# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The parent's page: linked children, their status, and the approval that moves an awaiting child to active."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from hallpass import grants, registry, terms, text
from hallpass.auth import current_person, require_csrf
from hallpass.registry import Person
from hallpass.web.pages import page

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
    return RedirectResponse("/children", status_code=303)
