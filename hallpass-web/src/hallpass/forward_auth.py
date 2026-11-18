# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The forward-auth endpoint Caddy calls before it proxies a service.

Two branches. A feed service takes a feed token in the forwarded URI's `token` query parameter and never a session. Any
other service reads the session cookie that Caddy forwards, and a missing session gets a redirect to sign-in. Both
branches end in `access.evaluate`, so forward auth and the personal page cannot disagree. The endpoint only reads the
session. It takes no dependency on `current_person`, so a person who has not accepted the terms gets a 403, never a
redirect to /terms.

Nothing logged here carries a token, a URI or a host. Logs name the service slug, the person id, the outcome and the
first reason.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse, Response

from hallpass import access, registry, revocation, services, tokens
from hallpass.logs import log_event
from hallpass.registry import Person
from hallpass.services import Service

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass(frozen=True)
class Verdict:
    person: Person | None
    access: access.Access | None


def _allowed(person: Person) -> Response:
    return Response(status_code=200, headers={"X-Hallpass-Person": person.ref,
                                               "X-Hallpass-Type": person.account_type})


def _denied(service: str | None, *, person_id: int | None, reason: str) -> Response:
    log_event(logger, "forward_auth", service=service, person_id=person_id, outcome="deny", reason=reason)
    return Response(status_code=403)


def _sign_in_location(request: Request) -> str:
    """The sign-in URL with the forwarded address as `next`. /login checks `next` against the cookie domain."""
    deps = request.app.state.deps
    proto = request.headers.get("x-forwarded-proto") or "https"
    host = request.headers.get("x-forwarded-host", "")
    uri = request.headers.get("x-forwarded-uri", "")
    return f"{deps.config.public_url}/login?{urlencode({'next': f'{proto}://{host}{uri}'})}"


def _feed_token(forwarded_uri: str) -> str:
    """The `token` query parameter of the forwarded URI, or an empty string. A malformed URI has no token."""
    try:
        query = urlsplit(forwarded_uri).query
    except ValueError:
        return ""
    return (parse_qs(query).get("token") or [""])[0]


def _feed_verdict(conn: sqlite3.Connection, service: Service, raw: str) -> Verdict:
    owner = tokens.feed_owner(conn, service_id=service.id, raw=raw)
    if owner is None:
        return Verdict(person=None, access=None)
    person = registry.get_person(conn, owner)
    if person is None:
        return Verdict(person=None, access=None)
    return Verdict(person=person, access=access.evaluate(conn, person, service))


def _session_revoked(conn: sqlite3.Connection, session: Mapping[str, object], person_id: int) -> bool:
    """Whether back-channel logout or a sign-out at the provider has ended this session. An unknown person is left to
    the verdict, which denies them."""
    person = registry.get_person(conn, person_id)
    if person is None:
        return False
    return revocation.session_revoked(conn, sid=session.get("sid"), sub=person.authentik_uuid,
                                      started_at=session.get("started_at"))


def _session_verdict(conn: sqlite3.Connection, service: Service, person_id: int) -> Verdict:
    person = registry.get_person(conn, person_id)
    if person is None or person.status == "locked":
        return Verdict(person=person, access=None)
    return Verdict(person=person, access=access.evaluate(conn, person, service))


def _answer(service: str, verdict: Verdict) -> Response:
    """Logs and answers a verdict for a service whose session or token was checked."""
    person = verdict.person
    if person is None:
        return _denied(service, person_id=None, reason="no_person")
    if verdict.access is None:
        return _denied(service, person_id=person.id, reason="locked")
    if verdict.access.desired == "allowed":
        log_event(logger, "forward_auth", service=service, person_id=person.id, outcome="allow")
        return _allowed(person)
    reason = verdict.access.reasons[0]
    return _denied(service, person_id=person.id, reason=reason)


@router.get("/forward-auth")
async def forward_auth(request: Request, service: Annotated[str, Query()] = "") -> Response:
    deps = request.app.state.deps
    found = await deps.db.run(lambda c: services.get_by_slug(c, service))
    if found is None or found.retired:
        return _denied(None, person_id=None, reason="unknown_service")

    if found.kind == "feed":
        raw = _feed_token(request.headers.get("x-forwarded-uri", ""))
        if not raw:
            return _denied(service, person_id=None, reason="no_token")
        verdict = await deps.db.run(lambda c: _feed_verdict(c, found, raw))
        return _answer(service, verdict)

    person_id = request.session.get("person_id")
    if person_id is not None and await deps.db.run(lambda c: _session_revoked(c, request.session, person_id)):
        request.session.clear()
        person_id = None
    if person_id is None:
        log_event(logger, "forward_auth", service=service, outcome="signin")
        return RedirectResponse(_sign_in_location(request), status_code=302)
    verdict = await deps.db.run(lambda c: _session_verdict(c, found, person_id))
    return _answer(service, verdict)
