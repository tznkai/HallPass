# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in: the OIDC seam, enrollment binding, sessions, CSRF and the admin check."""

from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Protocol
from urllib.parse import urlsplit

# Authlib 1.9.5 warns at import that its httpx integration is deprecated. That is upstream, not hallpass code, so
# only that message is muted, and only for this import. authlib.deprecate must load first: it installs its own
# "always" filter for that warning class, which would otherwise sit in front of the one below.
import authlib.deprecate  # noqa: F401

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="The httpx module is deprecated", category=DeprecationWarning)
    from authlib.common.errors import AuthlibBaseError
    from authlib.integrations.starlette_client import OAuth

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from joserfc.errors import JoseError
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from hallpass import registry, terms, text
from hallpass.authentik import AuthentikClient, AuthentikError, AuthentikUser
from hallpass.config import Config
from hallpass.db import Database
from hallpass.logs import log_event
from hallpass.registry import Person

logger = logging.getLogger(__name__)
SESSION_COOKIE = "hallpass_session"


@dataclass(frozen=True)
class Identity:
    sub: str
    username: str | None
    enrollment_ref: str | None


class OidcClient(Protocol):
    async def begin(self, request: Request, redirect_uri: str) -> Response: ...

    async def finish(self, request: Request) -> Identity: ...


class SignInError(Exception):
    """reason is one of "not_registered", "locked", "unavailable" or "sign_in_failed"."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# What the OIDC libraries raise for a refused or unusable sign-in. AuthlibBaseError is the base of authlib's OAuth
# errors (an IdP error reply, a state mismatch). JoseError is joserfc's base, which authlib uses to validate the ID
# token: a bad signature, an undecodable token, or a failed issuer, audience, nonce or expiry check. httpx covers an
# unreachable or non-2xx IdP or metadata endpoint, and KeyError and ValueError cover a reply without the claims
# hallpass reads.
_OIDC_ERRORS = (httpx.HTTPError, AuthlibBaseError, JoseError, KeyError, ValueError)


def _oidc_failure(error: Exception) -> SignInError:
    """Logs the failure by reason only. The exception text can carry URLs or the IdP's own words, so it is dropped."""
    reason = "unavailable" if isinstance(error, httpx.HTTPError) else "sign_in_failed"
    log_event(logger, "sign_in", outcome="oidc_failed", reason=reason)
    return SignInError(reason)


class AuthlibOidc:
    """The production OIDC client. Verified against the live IdP, not in unit tests."""

    def __init__(self, *, metadata_url: str, client_id: str, client_secret: str) -> None:
        oauth = OAuth()
        oauth.register("authentik", server_metadata_url=metadata_url, client_id=client_id,
                       client_secret=client_secret, client_kwargs={"scope": "openid profile hallpass"})
        self._client = oauth.create_client("authentik")

    async def begin(self, request: Request, redirect_uri: str) -> Response:
        try:
            return await self._client.authorize_redirect(request, redirect_uri)
        except _OIDC_ERRORS as error:
            raise _oidc_failure(error) from None

    async def finish(self, request: Request) -> Identity:
        try:
            token = await self._client.authorize_access_token(request)
            userinfo = token["userinfo"]
            return Identity(sub=userinfo["sub"], username=userinfo.get("preferred_username"),
                            enrollment_ref=userinfo.get("hallpass_enrollment"))
        except _OIDC_ERRORS as error:
            raise _oidc_failure(error) from None


class RedirectToLogin(Exception):
    """The app's handler turns this into a 303 to /login?next=<next_path>."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path
        super().__init__("sign-in required")


class RedirectToTerms(Exception):
    """The app's handler turns this into a 303 to /terms?next=<next_path>."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path
        super().__init__("terms acceptance required")


def add_session_middleware(app: FastAPI, config: Config) -> None:
    # HttpOnly is always set by Starlette's session cookie; Secure comes from https_only.
    app.add_middleware(SessionMiddleware, secret_key=config.session_secret, session_cookie=SESSION_COOKIE,
                       max_age=config.session_max_age_seconds, same_site="lax", https_only=True,
                       domain=config.cookie_domain)


# CSRF and session helpers ----------------------------------------------------------------------------

def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _deps(request: Request):
    return request.app.state.deps


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def require_csrf(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin != _origin(_deps(request).config.public_url):
        raise HTTPException(403, text.CSRF_FAILED)
    expected = request.session.get("csrf")
    submitted = (await request.form()).get("csrf")
    if not expected or not isinstance(submitted, str):
        raise HTTPException(403, text.CSRF_FAILED)
    if not hmac.compare_digest(submitted.encode(), expected.encode()):
        raise HTTPException(403, text.CSRF_FAILED)


def start_session(request: Request, person_id: int) -> None:
    request.session.clear()
    request.session["person_id"] = person_id
    request.session["csrf"] = secrets.token_urlsafe(32)


def end_session(request: Request) -> None:
    request.session.clear()


# Sign-in resolution ----------------------------------------------------------------------------------

async def _authentik_user(authentik: AuthentikClient, sub: str) -> AuthentikUser:
    try:
        return await authentik.user_by_uuid(sub)
    except AuthentikError as error:
        log_event(logger, "sign_in", outcome="authentik_failed", reason=error.reason)
        raise SignInError("unavailable") from None


def _bind_enrolled(conn: sqlite3.Connection, *, ref: str, user: AuthentikUser, now: datetime) -> Person | None:
    """Re-checks the ref inside the transaction, so two sign-ins with one ref cannot both bind it."""
    person = registry.get_by_enrollment_ref(conn, ref)
    if person is None or person.status == "locked":
        return person          # a locked person is never bound; the caller refuses it
    registry.bind_identity(conn, person.id, uuid=user.uuid, authentik_pk=user.pk, username=user.username)
    registry.log_decision(conn, at=now, actor_kind="person", actor_person_id=person.id,
                          target_person_id=person.id, action="enrolled")
    return registry.get_person(conn, person.id)


async def _enroll(db: Database, authentik: AuthentikClient, identity: Identity, now: datetime) -> Person | None:
    candidate = await db.run(lambda c: registry.get_by_enrollment_ref(c, identity.enrollment_ref))
    if candidate is None:
        return None
    if candidate.status == "locked":
        return candidate       # refused by the caller; no Authentik call and nothing is bound
    user = await _authentik_user(authentik, identity.sub)
    return await db.run(lambda c: _bind_enrolled(c, ref=identity.enrollment_ref, user=user, now=now))


def _bootstrap_admin(conn: sqlite3.Connection, *, user: AuthentikUser, display_name: str, now: datetime) -> Person:
    existing = registry.get_by_uuid(conn, user.uuid)
    if existing is not None:
        return existing
    person = registry.create_person(conn, display_name=display_name, account_type="adult", now=now)
    registry.bind_identity(conn, person.id, uuid=user.uuid, authentik_pk=user.pk, username=user.username)
    registry.log_decision(conn, at=now, actor_kind="system", actor_person_id=None,
                          target_person_id=person.id, action="admin_bootstrapped")
    return registry.get_person(conn, person.id)


async def resolve_sign_in(db: Database, authentik: AuthentikClient, config: Config, identity: Identity,
                          now: datetime) -> Person:
    person = await db.run(lambda c: registry.get_by_uuid(c, identity.sub))
    if person is None and identity.enrollment_ref is not None:
        person = await _enroll(db, authentik, identity, now)
    if person is None and identity.sub in config.admin_subs:
        user = await _authentik_user(authentik, identity.sub)
        display_name = identity.username or "Administrator"
        person = await db.run(lambda c: _bootstrap_admin(c, user=user, display_name=display_name, now=now))
    if person is None:
        raise SignInError("not_registered")
    if person.status == "locked":
        raise SignInError("locked")
    return person


# Request dependencies --------------------------------------------------------------------------------

async def current_person_any_terms(request: Request) -> Person:
    """The signed-in, unlocked person, whatever their terms state. Only the terms routes and sign-out use it directly.
    It also records on request.state whether the person has linked children, for the nav link."""
    deps = _deps(request)
    person_id = request.session.get("person_id")
    if person_id is None:
        raise RedirectToLogin(request.url.path)
    person = await deps.db.run(lambda c: registry.get_person(c, person_id))
    if person is None:
        end_session(request)
        raise RedirectToLogin(request.url.path)
    if person.status == "locked":
        end_session(request)
        raise HTTPException(403, text.LOCKED)
    request.state.has_children = await deps.db.run(lambda c: bool(registry.children_of(c, person.id)))
    return person


async def current_person(request: Request) -> Person:
    """The page gate: as current_person_any_terms, and a person who must still accept the terms is sent to /terms."""
    person = await current_person_any_terms(request)
    state = await _deps(request).db.run(lambda c: terms.state_for(c, person.id))
    if terms.pages_blocked(state):
        raise RedirectToTerms(request.url.path)
    return person


def _require_admin_person(request: Request, person: Person) -> Person:
    if person.authentik_uuid is None or person.authentik_uuid not in _deps(request).config.admin_subs:
        raise HTTPException(403, text.FORBIDDEN)
    return person


async def require_admin(request: Request, person: Annotated[Person, Depends(current_person)]) -> Person:
    return _require_admin_person(request, person)


async def require_admin_any_terms(request: Request,
                                  person: Annotated[Person, Depends(current_person_any_terms)]) -> Person:
    """The admin check without the page gate. Only the admin terms pages use it, so the first version can be published."""
    return _require_admin_person(request, person)
