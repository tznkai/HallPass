# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Back-channel logout tokens and the sign-out redirect.

A logout token is checked in full before anything is recorded: the signature against the provider's keys, then the
issuer, audience, timestamps, event, nonce and subject. Each failure names a reason token, and nothing else about the
token leaves this module. The end-session URL is built from the discovery endpoint, the stored ID token and the public
URL, and from nothing the request sent.
"""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from joserfc import jwt
from joserfc.errors import BadSignatureError, InvalidKeyIdError, JoseError, MissingKeyError
from joserfc.jwk import KeySet

EVENT_KEY = "http://schemas.openid.net/event/backchannel-logout"
# The provider signs with RS256. Naming the algorithm stops a token from choosing its own, including "none" and HMAC.
ALGORITHMS = ["RS256"]
# A token may be this many seconds past its exp, or this many seconds ahead of now, because clocks differ.
CLOCK_SKEW_SECONDS = 60
# A logout token longer than this is refused before anything is verified.
MAX_TOKEN_CHARS = 8192


class LogoutTokenRefused(Exception):
    """reason is a short token such as "bad_signature" or "expired"; it never carries the token or its claims."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ProviderError(Exception):
    """The discovery document or the keys could not be read. reason is "provider_unavailable"."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Provider:
    issuer: str
    end_session_endpoint: str | None
    jwks: Mapping[str, Any] | None     # None when the caller asked for no keys


@dataclass(frozen=True)
class LogoutClaims:
    sid: str | None
    sub: str | None
    issued_at: datetime      # the token's iat: when the logout happened at the provider


def _is_number(value: object) -> bool:
    """A finite JSON number. An integer is always finite here: math.isfinite raises for one beyond the float range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return isinstance(value, int) or math.isfinite(value)


def _check_claims(claims: Mapping[str, Any], *, issuer: str, client_id: str, now: datetime) -> LogoutClaims:
    if claims.get("iss") != issuer:
        raise LogoutTokenRefused("bad_issuer")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
    if client_id not in audiences:
        raise LogoutTokenRefused("bad_audience")
    if "iat" not in claims:
        raise LogoutTokenRefused("missing_iat")
    if "exp" not in claims:
        raise LogoutTokenRefused("missing_exp")
    if not _is_number(claims["iat"]) or not _is_number(claims["exp"]):
        raise LogoutTokenRefused("malformed_claim")
    if claims["exp"] + CLOCK_SKEW_SECONDS <= now.timestamp():
        raise LogoutTokenRefused("expired")
    if claims["iat"] - CLOCK_SKEW_SECONDS > now.timestamp():
        raise LogoutTokenRefused("issued_in_future")
    events = claims.get("events")
    if not isinstance(events, dict) or EVENT_KEY not in events:
        raise LogoutTokenRefused("missing_event")
    if "nonce" in claims:
        raise LogoutTokenRefused("has_nonce")
    for name in ("sid", "sub"):
        if claims.get(name) is not None and not isinstance(claims[name], str):
            raise LogoutTokenRefused("malformed_claim")
    sid = claims.get("sid") or None
    sub = claims.get("sub") or None
    if sid is None and sub is None:
        raise LogoutTokenRefused("no_subject")
    try:
        issued_at = datetime.fromtimestamp(claims["iat"], UTC)
    except (OverflowError, OSError, ValueError):
        raise LogoutTokenRefused("malformed_claim") from None
    return LogoutClaims(sid=sid, sub=sub, issued_at=issued_at)


def check_size(token: str) -> None:
    """Refuses a token longer than MAX_TOKEN_CHARS, before anything about it is read or verified."""
    if len(token) > MAX_TOKEN_CHARS:
        raise LogoutTokenRefused("too_large")


def check_header(token: str) -> None:
    """Refuses a token that is not a compact JWS with an RS256 header and a string kid. The header is read without
    verifying anything, so no key is fetched on behalf of a token that could never verify."""
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise LogoutTokenRefused("malformed")
    try:
        header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
    except (ValueError, TypeError, RecursionError):
        raise LogoutTokenRefused("malformed") from None
    if not isinstance(header, dict) or header.get("alg") not in ALGORITHMS or not isinstance(header.get("kid"), str):
        raise LogoutTokenRefused("malformed")


def verify_logout_token(token: str, *, jwks: Mapping[str, Any], issuer: str, client_id: str,
                        now: datetime) -> LogoutClaims:
    """Checks a logout token in full and returns its sid and sub. Raises LogoutTokenRefused with the reason otherwise.

    An unknown kid is refused as "unknown_kid", so the caller can fetch the keys again and retry once.
    """
    check_size(token)
    try:
        key_set = KeySet.import_key_set(dict(jwks))
    except (JoseError, TypeError, ValueError, KeyError):
        raise LogoutTokenRefused("bad_keys") from None
    try:
        decoded = jwt.decode(token, key_set, algorithms=ALGORITHMS)
    except (InvalidKeyIdError, MissingKeyError):
        raise LogoutTokenRefused("unknown_kid") from None
    except BadSignatureError:
        raise LogoutTokenRefused("bad_signature") from None
    except (JoseError, TypeError, ValueError):
        raise LogoutTokenRefused("malformed") from None
    return _check_claims(decoded.claims, issuer=issuer, client_id=client_id, now=now)


def end_session_url(endpoint: str, *, id_token: str, public_url: str) -> str:
    """The provider's end-session address with exactly the ID token hint and the address to return to."""
    query = urlencode({"id_token_hint": id_token, "post_logout_redirect_uri": f"{public_url}/signed-out"})
    return urlunsplit(urlsplit(endpoint)._replace(query=query))
