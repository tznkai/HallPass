# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in, sign-out and back-channel logout. The home page is routes_me; the admin pages live in routes_admin."""

from __future__ import annotations

import logging
import secrets
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse, Response

from hallpass import authentik_sync, grants, logout, oidc_session, registry, revocation, terms, text
from hallpass.auth import SignInError, end_session, require_csrf, resolve_sign_in, start_session
from hallpass.config import Config
from hallpass.logout import LogoutClaims, LogoutTokenRefused, ProviderError
from hallpass.logs import log_event
from hallpass.web.pages import message_page, page

logger = logging.getLogger(__name__)
router = APIRouter()
SIGNED_OUT = "/signed-out"

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
    # Starting a sign-in ends any session the browser already holds. The cookie is then re-signed without the old
    # person, sid and start time, so a request to /login never renews an existing session.
    end_session(request)
    target = safe_next(next_path, deps.config)
    if target is not None:
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
    if settled.status == "locked":
        # Locked after resolve_sign_in passed the person and before the settle read the status. Refused as a locked
        # person is, so no session starts.
        return _refused(request, SignInError("locked"))
    if not was_bound or settled.status != person.status:
        await authentik_sync.sync_people(deps, [person.id])     # the first sign-in binds the person to Authentik
    person = settled
    destination = request.session.get("next") or "/"
    state = await deps.db.run(lambda c: terms.state_for(c, person.id))
    handle = await _keep_id_token(deps, identity.id_token)
    start_session(request, person.id, sid=identity.sid, handle=handle, started_at=deps.clock())
    log_event(logger, "sign_in", outcome="ok", person_id=person.id)
    # A child is never sent to /terms: a linked parent's acceptance covers them.
    if person.account_type != "child" and not state.accepted_current:
        return RedirectResponse(f"/terms?{urlencode({'next': destination})}", status_code=303)
    return RedirectResponse(destination, status_code=303)


async def _keep_id_token(deps, id_token: str | None) -> str | None:
    """Stores the raw ID token on the server and returns the handle the session cookie carries. No token, no handle."""
    if id_token is None:
        return None
    handle = secrets.token_urlsafe(32)
    now = deps.clock()
    max_age = deps.config.session_max_age_seconds
    await deps.db.run(lambda c: oidc_session.store(c, handle=handle, id_token=id_token, now=now,
                                                   max_age_seconds=max_age))
    return handle


async def _take_id_token(deps, handle: object) -> str | None:
    """The ID token stored under the session's handle, deleted as it is taken. None when there is no handle or row."""
    if not isinstance(handle, str) or not handle:
        return None
    return await deps.db.run(lambda c: oidc_session.take(c, handle))


async def _sign_out_target(deps, id_token: object) -> str:
    """Where a sign-out goes. The provider's end-session endpoint takes the stored ID token and the public sign-out
    address; without an ID token, or without an endpoint, or when discovery fails, it goes to the signed-out page. The
    address is built from discovery, the stored token and the public URL, and never from the request."""
    if not isinstance(id_token, str) or not id_token:
        log_event(logger, "sign_out", outcome="local", reason="no_id_token")
        return SIGNED_OUT
    try:
        provider = await deps.oidc.provider(keys=False)
    except ProviderError as error:
        log_event(logger, "sign_out", outcome="local", reason=error.reason)
        return SIGNED_OUT
    if not provider.end_session_endpoint:
        log_event(logger, "sign_out", outcome="local", reason="no_endpoint")
        return SIGNED_OUT
    log_event(logger, "sign_out", outcome="provider")
    return logout.end_session_url(provider.end_session_endpoint, id_token=id_token,
                                  public_url=deps.config.public_url)


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout_route(request: Request) -> RedirectResponse:
    deps = request.app.state.deps
    sid = request.session.get("sid")
    if isinstance(sid, str) and sid:
        # Recorded here as well as at the provider, so a copy of this session's cookie ends at once, not at the
        # provider's pace.
        now, max_age = deps.clock(), deps.config.session_max_age_seconds
        await deps.db.run(lambda c: revocation.revoke_session(c, sid=sid, now=now, max_age_seconds=max_age))
    id_token = await _take_id_token(deps, request.session.get("id_token_handle"))
    end_session(request)
    return RedirectResponse(await _sign_out_target(deps, id_token), status_code=303)


@router.get("/signed-out")
async def signed_out(request: Request) -> Response:
    """The page a sign-out ends on. It needs no session, and it shows the sign-in link."""
    return page(request, "signed_out.html", viewer=None, heading=text.SIGNED_OUT_HEADING,
                sign_in=text.SIGNED_OUT_SIGN_IN)


def _verify(deps, provider: logout.Provider, token: str) -> LogoutClaims:
    return logout.verify_logout_token(token, jwks=provider.jwks, issuer=provider.issuer,
                                      client_id=deps.config.oidc_client_id, now=deps.clock())


async def _verified_claims(deps, token: str) -> LogoutClaims:
    """Checks the token against the provider's keys. An unknown kid fetches the keys once more and tries again. A token
    that is too large, or not an RS256 JWS with a kid, is refused before the provider is asked for anything."""
    logout.check_size(token)
    logout.check_header(token)
    try:
        return _verify(deps, await deps.oidc.provider(), token)
    except LogoutTokenRefused as error:
        if error.reason != "unknown_kid":
            raise
    return _verify(deps, await deps.oidc.provider(refresh_keys=True), token)


async def _accept_logout_token(deps, raw: object) -> str | None:
    """Verifies one logout token and records what it ends. Returns None once recorded, or the reason it was refused,
    in which case nothing is recorded."""
    if not isinstance(raw, str) or not raw:
        return "missing_token"
    try:
        claims = await _verified_claims(deps, raw)
    except (LogoutTokenRefused, ProviderError) as error:
        return error.reason
    now, max_age = deps.clock(), deps.config.session_max_age_seconds
    if claims.sid is not None:
        await deps.db.run(lambda c: revocation.revoke_session(c, sid=claims.sid, now=now, max_age_seconds=max_age))
    else:
        await deps.db.run(lambda c: revocation.revoke_subject(c, sub=claims.sub, issued_at=claims.issued_at, now=now,
                                                              max_age_seconds=max_age))
    return None


@router.post("/auth/backchannel-logout")
async def backchannel_logout(request: Request) -> Response:
    """The provider's back-channel logout. It is public and takes no session and no CSRF token: the signed token is the
    only authority. It answers 200 once the token is recorded, and 400 with nothing recorded otherwise. The answer
    carries nothing the caller can use."""
    deps = request.app.state.deps
    reason = await _accept_logout_token(deps, (await request.form()).get("logout_token"))
    if reason is None:
        log_event(logger, "backchannel_logout", outcome="ok")
        return Response(status_code=200, headers={"Cache-Control": "no-store"})
    log_event(logger, "backchannel_logout", outcome="refused", reason=reason)
    return Response(status_code=400, headers={"Cache-Control": "no-store"})
