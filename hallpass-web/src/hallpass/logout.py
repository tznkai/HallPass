# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Back-channel logout tokens and the sign-out redirect.

A logout token is checked in full before anything is recorded: the signature against the provider's keys, then the
issuer, audience, timestamps, event, nonce and subject. Each failure names a reason token, and nothing else about the
token leaves this module. The end-session URL is built from the discovery endpoint, the stored ID token and the public
URL, and from nothing the request sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from joserfc import jwt
from joserfc.errors import BadSignatureError, InvalidKeyIdError, JoseError, MissingKeyError
from joserfc.jwk import KeySet

EVENT_KEY = "http://schemas.openid.net/event/backchannel-logout"
# The provider signs with RS256. Naming the algorithm stops a token from choosing its own, including "none" and HMAC.
ALGORITHMS = ["RS256"]


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
    jwks: Mapping[str, Any]


@dataclass(frozen=True)
class LogoutClaims:
    sid: str | None
    sub: str | None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
    if claims["exp"] <= now.timestamp():
        raise LogoutTokenRefused("expired")
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
    return LogoutClaims(sid=sid, sub=sub)


def verify_logout_token(token: str, *, jwks: Mapping[str, Any], issuer: str, client_id: str,
                        now: datetime) -> LogoutClaims:
    """Checks a logout token in full and returns its sid and sub. Raises LogoutTokenRefused with the reason otherwise.

    An unknown kid is refused as "unknown_kid", so the caller can fetch the keys again and retry once.
    """
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
