# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test signing keys and logout tokens. Keys are generated in the test run, and no network is used."""

from __future__ import annotations

from datetime import datetime
from functools import cache
from typing import Any

from joserfc import jwt
from joserfc.jwk import RSAKey

ISSUER = "https://authentik.test/application/o/hallpass/"
CLIENT_ID = "hallpass-client"
END_SESSION = "https://authentik.test/application/o/hallpass/end-session/"
EVENT = "http://schemas.openid.net/event/backchannel-logout"
SUB = "00000000-0000-4000-8000-0000000000d1"
SID = "sid-test-session-one"
SECOND_SID = "sid-test-session-two"


@cache
def signing_key(kid: str = "test-key") -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": kid, "alg": "RS256", "use": "sig"})


def jwks(*kids: str) -> dict[str, Any]:
    """A public JWKS holding one key per kid. The default is the key the provider signs with in tests."""
    names = kids or ("test-key",)
    return {"keys": [signing_key(kid).as_dict(private=False) for kid in names]}


def logout_claims(now: datetime, **overrides: Any) -> dict[str, Any]:
    """A valid logout token's claims. An override of None drops that claim."""
    issued = int(now.timestamp())
    claims: dict[str, Any] = {"iss": ISSUER, "aud": CLIENT_ID, "iat": issued, "exp": issued + 120,
                              "jti": "token-test-1", "events": {EVENT: {}}, "sub": SUB, "sid": SID}
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def sign(claims: dict[str, Any], *, kid: str = "test-key", key: RSAKey | None = None) -> str:
    """Signs claims as RS256. The key defaults to the one the provider's JWKS holds under `kid`."""
    return jwt.encode({"alg": "RS256", "kid": kid}, claims, key or signing_key(kid))


def logout_token(now: datetime, **overrides: Any) -> str:
    return sign(logout_claims(now, **overrides))
