# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in, sign-out and the home page. The admin pages live in routes_admin."""

from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from hallpass import text
from hallpass.auth import SignInError, current_person, end_session, require_csrf, resolve_sign_in, start_session
from hallpass.config import Config
from hallpass.logs import log_event
from hallpass.registry import Person
from hallpass.web.pages import message_page, page

logger = logging.getLogger(__name__)
router = APIRouter()

REFUSALS = {
    "not_registered": (text.NOT_REGISTERED, 403),
    "locked": (text.LOCKED, 403),
    "unavailable": (text.SIGNIN_UNAVAILABLE, 503),
}


def safe_next(value: str | None, config: Config) -> str | None:
    """A path to return to after sign-in, or None. Accepts a single-slash relative path, and an https URL on this
    host or on the cookie domain. Anything with whitespace, control characters or a backslash is refused, because
    browsers strip some of those and turn `/\\t/host` into a protocol-relative URL."""
    if not value or any(c.isspace() or ord(c) < 0x20 or c == "\\" for c in value):
        return None
    if value.startswith("/"):
        return value if value[1:2] not in ("/", "\\") else None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme != "https" or "@" in parts.netloc:
        return None
    if parts.netloc == urlsplit(config.public_url).netloc:
        return value
    if config.cookie_domain:
        domain = config.cookie_domain.lstrip(".").lower()
        host = parts.netloc.lower()
        if ":" not in host and (host == domain or host.endswith("." + domain)):
            return value
    return None


@router.get("/login")
async def login(request: Request, next_path: Annotated[str | None, Query(alias="next")] = None) -> Response:
    deps = request.app.state.deps
    target = safe_next(next_path, deps.config)
    if target is None:
        request.session.pop("next", None)
    else:
        request.session["next"] = target
    return await deps.oidc.begin(request, f"{deps.config.public_url}/auth/callback")


@router.get("/auth/callback")
async def callback(request: Request) -> Response:
    deps = request.app.state.deps
    identity = await deps.oidc.finish(request)
    try:
        person = await resolve_sign_in(deps.db, deps.authentik, deps.config, identity, deps.clock())
    except SignInError as error:
        end_session(request)
        log_event(logger, "sign_in", outcome="refused", reason=error.reason)
        message, status = REFUSALS[error.reason]
        return message_page(request, message, status)
    destination = request.session.get("next") or "/"
    start_session(request, person.id)
    log_event(logger, "sign_in", outcome="ok", person_id=person.id)
    return RedirectResponse(destination, status_code=303)


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout(request: Request) -> RedirectResponse:
    end_session(request)
    return RedirectResponse("/", status_code=303)


@router.get("/")
async def home(request: Request, person: Annotated[Person, Depends(current_person)]) -> HTMLResponse:
    return page(request, "home.html", viewer=person)
