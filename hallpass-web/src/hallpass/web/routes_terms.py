# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The terms page and the acceptance form. Both work before a first acceptance, so they use current_person_any_terms.
A child sees the terms read only: a linked parent accepts them for the child, and a child's acceptance is refused."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from hallpass import authentik_sync, grants, terms, text
from hallpass.auth import current_person_any_terms, end_session, require_csrf
from hallpass.registry import Person
from hallpass.web.pages import message_page, page
from hallpass.web.routes_auth import safe_next

router = APIRouter()


@router.get("/terms")
async def terms_page(request: Request, person: Annotated[Person, Depends(current_person_any_terms)],
                     next_path: Annotated[str | None, Query(alias="next")] = None) -> Response:
    deps = request.app.state.deps
    is_child = person.account_type == "child"

    def read(conn) -> tuple:
        state = terms.state_for(conn, person.id)
        covered = is_child and not terms.services_blocked_for(conn, person)
        return state, covered

    state, covered = await deps.db.run(read)
    current = state.current
    child_line = None
    if is_child:
        child_line = text.TERMS_CHILD_COVERED if covered else text.TERMS_CHILD_NOT_COVERED
    return page(request, "terms.html", viewer=person, current=current,
                target=safe_next(next_path, deps.config) or "/",
                show_not_now=not is_child and state.accepted_any and current is not None and not current.blocking,
                not_now=text.TERMS_NOT_NOW, not_published=text.TERMS_NOT_PUBLISHED,
                read_only=is_child, child_line=child_line)


@router.post("/terms/accept", dependencies=[Depends(require_csrf)])
async def accept_terms(request: Request, person: Annotated[Person, Depends(current_person_any_terms)],
                       version_id: Annotated[int, Form()],
                       next_path: Annotated[str | None, Form(alias="next")] = None) -> Response:
    if person.account_type == "child":
        # Refused before anything is written. A linked parent accepts the terms for a child.
        raise HTTPException(403, text.CHILD_CANNOT_ACCEPT)
    deps = request.app.state.deps
    now = deps.clock()

    def change(conn) -> bool:
        # Checked inside the same transaction as the write, so a version published meanwhile is never accepted by mistake.
        current = terms.current(conn)
        if current is None or current.id != version_id:
            return False
        grants.accept_terms(conn, person.id, version_id, now)
        return True

    try:
        accepted = await deps.db.run(change)
    except grants.AcceptanceLocked:
        # Locked between the sign-in check above and this write. Same answer as the auth layer gives a locked person.
        end_session(request)
        raise HTTPException(403, text.LOCKED) from None
    except grants.ChildCannotAccept:
        raise HTTPException(403, text.CHILD_CANNOT_ACCEPT) from None
    if not accepted:
        return message_page(request, text.TERMS_CHANGED, 409)
    # A parent's acceptance can cover linked children, so their Fluxer membership may change too.
    affected = await deps.db.run(lambda c: authentik_sync.affected_by(c, person.id))
    await authentik_sync.sync_people(deps, affected)
    return RedirectResponse(safe_next(next_path, deps.config) or "/", status_code=303)
