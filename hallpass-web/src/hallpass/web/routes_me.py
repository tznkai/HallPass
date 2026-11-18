# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The personal page: a person's services and status, their attached tokens, and the feed-link generator.

A person's page is only ever built for the signed-in person (`current_person`). Nothing here takes another person's id;
the parent's view of a child lives in routes_family and shows the child's service rows only. The admin person page
(routes_admin) reuses `service_rows` for the person it shows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from hallpass import access, desired, holds, registry, text, tokens
from hallpass.auth import current_person, require_csrf
from hallpass.registry import Person
from hallpass.services import Service
from hallpass.web.pages import page

router = APIRouter()
REASON_TEXT = {
    "retired": text.REASON_RETIRED,
    "not_entitled": text.REASON_NOT_ENTITLED,
    "not_active": text.REASON_NOT_ACTIVE,
    "terms": text.REASON_TERMS,
    "held": text.REASON_HELD,
}


def service_rows(conn: sqlite3.Connection, person: Person, pauses: desired.PairPauses) -> list[dict]:
    """One row per service the person can see: the status with its reasons, whether it can be suspended automatically,
    who placed each hold, a note when a game service has no external identity for the person, the report status
    hallmonitor's last report gives (None for a service it does not monitor), and whether one of the person's shared
    identities is paused. `pauses` is the second result of `desired.refresh_with_pauses`, which the caller runs first.
    The pause carries holder ids only, and the member's wording never names the other holder."""
    identity_slugs = {identity.service_slug for identity in registry.identities_of(conn, person.id)}
    marked = holds.marked_service_ids(conn, person.id)
    rows = []
    for service, decision in access.services_for(conn, person):
        missing_identity = service.kind == "game" and service.slug not in identity_slugs
        holders = holds.hold_labels(conn, person.id, service.id)
        is_marked = service.id in marked
        report_status = desired.report_status(desired.desired_for(conn, person.id, service.id),
                                              desired.report_for(conn, person.id, service.id), is_marked)
        pause = pauses.get((service.id, person.id))
        if service.auto_suspendable:
            suspendable = text.CAN_AUTO_SUSPEND
        elif holders and is_marked:
            suspendable = text.MANUALLY_SUSPENDED
        else:
            suspendable = text.CANNOT_AUTO_SUSPEND
        rows.append({
            "service_id": service.id,
            "name": service.name,
            "status": text.STATUS_ALLOWED if decision.desired == "allowed" else text.STATUS_SUSPENDED,
            "reasons": [REASON_TEXT[reason] for reason in decision.reasons],
            "suspendable": suspendable,
            "auto_suspendable": service.auto_suspendable,
            "holders": holders,
            "identity_note": text.NO_GAME_IDENTITY if missing_identity else "",
            "report_status": report_status,
            "shared_pause": pause,
            "shared_identity_note": text.SHARED_IDENTITY_PAUSED if pause is not None else "",
        })
    return rows


def _me_context(conn: sqlite3.Connection, person: Person, now: datetime) -> dict:
    _, pauses = desired.refresh_with_pauses(conn, now)
    return {
        "rows": service_rows(conn, person, pauses),
        "visible_tokens": tokens.visible_to(conn, person),
        "feed_services": tokens.feed_services_for(conn, person),
    }


def feed_link(base: str, raw: str) -> str:
    """The feed address with the token added to its query. Any fragment stays last, so the token is never put after
    a `#` where a server cannot read it."""
    parts = urlsplit(base)
    query = f"{parts.query}&token={raw}" if parts.query else f"token={raw}"
    return urlunsplit(parts._replace(query=query))


@router.get("/")
async def home(request: Request, person: Annotated[Person, Depends(current_person)]) -> Response:
    deps = request.app.state.deps
    now = deps.clock()
    context = await deps.db.run(lambda c: _me_context(c, person, now))
    return page(request, "me.html", viewer=person, **context)


def _issue(conn: sqlite3.Connection, person: Person, service_id: int, now: datetime) -> tuple[str, Service | None, str]:
    """Returns the outcome, the service and the raw token. The token is issued only when the outcome is "issued"."""
    service = next((s for s in tokens.feed_services_for(conn, person) if s.id == service_id), None)
    if service is None:
        return "forbidden", None, ""
    if not service.url:
        return "no_address", service, ""
    raw = tokens.issue_feed(conn, person_id=person.id, service_id=service.id, now=now)
    return "issued", service, raw


@router.post("/tokens/feed/{service_id}", dependencies=[Depends(require_csrf)])
async def generate_feed(request: Request, service_id: int, person: Annotated[Person, Depends(current_person)]) -> Response:
    deps = request.app.state.deps
    now = deps.clock()
    outcome, service, raw = await deps.db.run(lambda c: _issue(c, person, service_id, now))
    if outcome == "forbidden":
        raise HTTPException(403, text.FORBIDDEN)
    if outcome == "no_address":
        return page(request, "message.html", viewer=person, status_code=409, message=text.FEED_NO_ADDRESS)
    shown = page(request, "feed_issued.html", viewer=person, service_name=service.name,
                 feed_url=feed_link(service.url, raw), shown_once=text.FEED_SHOWN_ONCE)
    # The page carries a live token, so no cache or proxy may keep it.
    shown.headers["Cache-Control"] = "no-store"
    return shown
