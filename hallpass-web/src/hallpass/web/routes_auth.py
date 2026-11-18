# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in and sign-out. The home page is routes_me; the admin pages live in routes_admin."""

from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse, Response

from hallpass import authentik_sync, grants, registry, terms, text
from hallpass.auth import SignInError, end_session, require_csrf, resolve_sign_in, start_session
from hallpass.config import Config
from hallpass.logs import log_event
from hallpass.web.pages import message_page

logger = logging.getLogger(__name__)
router = APIRouter()

REFUSALS = {
    "not_registered": (text.NOT_REGISTERED, 403),
    "locked": (text.LOCKED, 403),
    "unavailable": (text.SIGNIN_UNAVAILABLE, 503),
    "sign_in_failed": (text.SIGN_IN_FAILED, 400),
}


def _refused(request: Request, error: SignInError) -> Response:
    """Ends any partial session and shows the refusal page for the reason. Used by both sign-in steps."""
    end_session(request)
    log_event(logger, "sign_in", outcome="refused", reason=error.reason)
    message, status = REFUSALS[error.reason]
    return message_page(request, message, status)


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
    try:
        return await deps.oidc.begin(request, f"{deps.config.public_url}/auth/callback")
    except SignInError as error:
        return _refused(request, error)


@router.get("/auth/callback")
async def callback(request: Request) -> Response:
    deps = request.app.state.deps
    try:
        identity = await deps.oidc.finish(request)
        was_bound = await deps.db.run(lambda c: registry.get_by_uuid(c, identity.sub)) is not None
        person = await resolve_sign_in(deps.db, deps.authentik, deps.config, identity, deps.clock())
    except SignInError as error:
        return _refused(request, error)
    # Each sign-in settles an invited child: enrolled now, so active when a parent has granted, else awaiting_grant.
    settled = await deps.db.run(lambda c: grants.settle_child_at_sign_in(c, person.id, deps.clock()))
    if not was_bound or settled.status != person.status:
        await authentik_sync.sync_people(deps, [person.id])     # the first sign-in binds the person to Authentik
    person = settled
    destination = request.session.get("next") or "/"
    state = await deps.db.run(lambda c: terms.state_for(c, person.id))
    start_session(request, person.id)
    log_event(logger, "sign_in", outcome="ok", person_id=person.id)
    # A child is never sent to /terms: a linked parent's acceptance covers them.
    if person.account_type != "child" and not state.accepted_current:
        return RedirectResponse(f"/terms?{urlencode({'next': destination})}", status_code=303)
    return RedirectResponse(destination, status_code=303)


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout(request: Request) -> RedirectResponse:
    end_session(request)
    return RedirectResponse("/", status_code=303)

