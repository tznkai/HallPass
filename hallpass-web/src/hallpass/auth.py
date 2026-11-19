# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in: the OIDC seam, enrollment binding, sessions, CSRF and the admin check."""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import sqlite3
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Protocol
from urllib.parse import urlsplit

# Authlib 1.9.5 warns at import that its httpx integration is deprecated. That is upstream, not HallPass Console code, so
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

from hallpass import display_names, holds, registry, revocation, terms, text
from hallpass.authentik import AuthentikClient, AuthentikError, AuthentikUser
from hallpass.config import Config
from hallpass.db import Database
from hallpass.logout import Provider, ProviderError
from hallpass.logs import log_event
from hallpass.registry import Person

logger = logging.getLogger(__name__)
SESSION_COOKIE = "hallpass_session"
# A forced key refetch (an unknown kid on a logout token) is honoured at most once in this many seconds, so an
# unauthenticated sender cannot make the Console fetch the provider's keys on every request.
FORCED_REFRESH_COOLDOWN_SECONDS = 60


@dataclass(frozen=True)
class Identity:
    """What a sign-in tells the Console. sid is the ID token's session id (None when the token has none), which
    back-channel logout matches. id_token is the raw token: it is kept on the server, never in the cookie, and
    sign-out sends it back as the hint."""
    sub: str
    username: str | None
    enrollment_ref: str | None
    sid: str | None = None
    id_token: str | None = None


class OidcClient(Protocol):
    async def begin(self, request: Request, redirect_uri: str) -> Response: ...

    async def finish(self, request: Request) -> Identity: ...

    async def provider(self, *, keys: bool = True, refresh_keys: bool = False) -> Provider: ...


class SignInError(Exception):
    """reason is one of "not_registered", "locked", "unavailable" or "sign_in_failed"."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# What the OIDC libraries raise for a refused or unusable sign-in. AuthlibBaseError is the base of authlib's OAuth
# errors (an IdP error reply, a state mismatch). JoseError is joserfc's base, which authlib uses to validate the ID
# token: a bad signature, an undecodable token, or a failed issuer, audience, nonce or expiry check. httpx covers an
# unreachable or non-2xx IdP or metadata endpoint, and KeyError and ValueError cover a reply without the claims
# the Console reads.
_OIDC_ERRORS = (httpx.HTTPError, AuthlibBaseError, JoseError, KeyError, ValueError)


def _oidc_failure(error: Exception) -> SignInError:
    """Logs the failure by reason only. The exception text can carry URLs or the IdP's own words, so it is dropped."""
    reason = "unavailable" if isinstance(error, httpx.HTTPError) else "sign_in_failed"
    log_event(logger, "sign_in", outcome="oidc_failed", reason=reason)
    return SignInError(reason)


class AuthlibOidc:
    """The production OIDC client. Verified against the live IdP, not in unit tests."""

    def __init__(self, *, metadata_url: str, client_id: str, client_secret: str,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._keys_lock = asyncio.Lock()
        self._last_fetch: float | None = None           # when the last key fetch went out, on the injected clock
        self._keys: Mapping[str, Any] | None = None     # the key set the last successful fetch returned
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
            sid = userinfo.get("sid")
            id_token = token.get("id_token")
            return Identity(sub=userinfo["sub"], username=userinfo.get("preferred_username"),
                            enrollment_ref=userinfo.get("hallpass_enrollment"),
                            sid=sid if isinstance(sid, str) and sid else None,
                            id_token=id_token if isinstance(id_token, str) and id_token else None)
        except _OIDC_ERRORS as error:
            raise _oidc_failure(error) from None

    async def provider(self, *, keys: bool = True, refresh_keys: bool = False) -> Provider:
        """The discovery document's issuer and end-session endpoint and, when keys is set, the provider's keys.
        Sign-out asks for no keys, so a JWKS failure cannot stop it. Every key fetch, forced or not, is at least
        FORCED_REFRESH_COOLDOWN_SECONDS after the last one, failed fetches included. Inside that window the cached keys
        are returned. With none cached, the call raises ProviderError("provider_unavailable") until the window has
        passed, so every logout token is refused meanwhile, and the sessions those tokens would have ended end at max
        age or at sign-out instead. The error's text is dropped."""
        try:
            metadata = await self._client.load_server_metadata()
            jwks = await self._fetch_keys(refresh=refresh_keys) if keys else None
            return Provider(issuer=metadata["issuer"], end_session_endpoint=metadata.get("end_session_endpoint"),
                            jwks=jwks)
        except (*_OIDC_ERRORS, RuntimeError):
            raise ProviderError("provider_unavailable") from None

    async def _fetch_keys(self, *, refresh: bool) -> Mapping[str, Any]:
        """The provider's keys. A read that needs the network goes out only when the cooldown has passed since the last
        fetch attempt, so there is at most one outbound fetch per window, and an empty cache does not lift that. With no
        keys cached, a read inside the window raises provider_unavailable, so the logout token is refused for up to the
        window. The lock makes a concurrent caller wait for the fetch in flight, then read the keys it stored."""
        async with self._keys_lock:
            now = self._monotonic()
            cooled = self._last_fetch is None or now - self._last_fetch >= FORCED_REFRESH_COOLDOWN_SECONDS
            if self._keys is None or refresh:
                if cooled:
                    self._last_fetch = now
                    self._keys = await self._client.fetch_jwk_set(force=refresh or self._keys is None)
                    return self._keys
                if self._keys is None:
                    raise ProviderError("provider_unavailable")
            # Inside the cooldown, or with keys already cached: the client answers from its own cache, sending nothing.
            return await self._client.fetch_jwk_set(force=False)


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


def start_session(request: Request, person_id: int, *, sid: str | None, handle: str | None,
                  started_at: datetime) -> None:
    """Starts a session. It keeps the sid that back-channel logout matches, the handle of the server-side ID token that
    sign-out takes, and the start time that a subject-wide revocation is compared with. The cookie holds no token."""
    request.session.clear()
    request.session["person_id"] = person_id
    request.session["csrf"] = secrets.token_urlsafe(32)
    request.session["sid"] = sid
    request.session["id_token_handle"] = handle
    request.session["started_at"] = revocation.stamp(started_at)


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


def _bootstrap_display_name(username: object) -> str:
    """The name a bootstrapped admin starts with: the Authentik username without control characters, or
    "Administrator" when nothing usable is left. A name must never fail the sign-in."""
    raw = username if isinstance(username, str) else ""
    cleaned = "".join(ch for ch in raw if not display_names.is_unsafe(ch)).strip()
    return cleaned or "Administrator"


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
        display_name = _bootstrap_display_name(identity.username)
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
    sid, started_at = request.session.get("sid"), request.session.get("started_at")
    now, max_age = deps.clock(), deps.config.session_max_age_seconds
    revoked = await deps.db.run(lambda c: revocation.session_ended(c, sid=sid, sub=person.authentik_uuid,
                                                                    started_at=started_at, now=now,
                                                                    max_age_seconds=max_age))
    if revoked:
        end_session(request)
        raise RedirectToLogin(request.url.path)
    request.state.has_children = await deps.db.run(lambda c: bool(registry.children_of(c, person.id)))
    # The admin's nav shows the open review count. Only an admin's request pays for that query.
    is_admin = person.authentik_uuid is not None and person.authentik_uuid in deps.config.admin_subs
    request.state.open_reviews = await deps.db.run(holds.open_review_count) if is_admin else 0
    return person


async def current_person(request: Request) -> Person:
    """The page gate: as current_person_any_terms, and an adult or parent who must still accept the terms is sent to
    /terms. A child is never sent there: their terms are covered by a parent."""
    person = await current_person_any_terms(request)
    blocked = await _deps(request).db.run(lambda c: terms.pages_blocked_for(c, person))
    if blocked:
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
