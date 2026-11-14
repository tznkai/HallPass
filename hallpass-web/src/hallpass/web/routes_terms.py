# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The terms page and the acceptance form. Both work before a first acceptance, so they use current_person_any_terms."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from hallpass import grants, terms, text
from hallpass.auth import current_person_any_terms, require_csrf
from hallpass.registry import Person
from hallpass.web.pages import message_page, page
from hallpass.web.routes_auth import safe_next

router = APIRouter()


@router.get("/terms")
async def terms_page(request: Request, person: Annotated[Person, Depends(current_person_any_terms)],
                     next_path: Annotated[str | None, Query(alias="next")] = None) -> Response:
    deps = request.app.state.deps
    state = await deps.db.run(lambda c: terms.state_for(c, person.id))
    current = state.current
    return page(request, "terms.html", viewer=person, current=current,
                target=safe_next(next_path, deps.config) or "/",
                show_not_now=state.accepted_any and current is not None and not current.blocking,
                not_now=text.TERMS_NOT_NOW, not_published=text.TERMS_NOT_PUBLISHED)


@router.post("/terms/accept", dependencies=[Depends(require_csrf)])
async def accept_terms(request: Request, person: Annotated[Person, Depends(current_person_any_terms)],
                       version_id: Annotated[int, Form()],
                       next_path: Annotated[str | None, Form(alias="next")] = None) -> Response:
    deps = request.app.state.deps
    now = deps.clock()

    def change(conn) -> bool:
        # Checked inside the same transaction as the write, so a version published meanwhile is never accepted by mistake.
        current = terms.current(conn)
        if current is None or current.id != version_id:
            return False
        grants.accept_terms(conn, person.id, version_id, now)
        return True

    if not await deps.db.run(change):
        return message_page(request, text.TERMS_CHANGED, 409)
    return RedirectResponse(safe_next(next_path, deps.config) or "/", status_code=303)
