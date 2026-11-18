# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logout tokens: the full check of a back-channel logout token, the end-session URL, and the Authlib adapter that
supplies the discovery document, the keys and the ID token's sid. The keys and tokens are fakes made in the test run."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import OctKey

from hallpass.auth import AuthlibOidc
from hallpass.logout import (
    CLOCK_SKEW_SECONDS,
    LogoutClaims,
    LogoutTokenRefused,
    Provider,
    ProviderError,
    end_session_url,
    verify_logout_token,
)
from tests.helpers import StubOidcClient
from tests.logout_tokens import (
    CLIENT_ID,
    END_SESSION,
    ISSUER,
    SID,
    SUB,
    jwks,
    logout_claims,
    logout_token,
    sign,
    signing_key,
)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
LEAK = "idp-text-with-a-token-abc123-and-a-host-authentik-invalid"


def verify(token: str, keys: dict | None = None) -> LogoutClaims:
    return verify_logout_token(token, jwks=keys if keys is not None else jwks(), issuer=ISSUER,
                               client_id=CLIENT_ID, now=NOW)


def test_a_valid_token_with_sid_and_sub_gives_both():
    assert verify(logout_token(NOW)) == LogoutClaims(sid=SID, sub=SUB, issued_at=NOW)


def test_a_valid_token_with_only_sub_gives_no_sid():
    assert verify(logout_token(NOW, sid=None)) == LogoutClaims(sid=None, sub=SUB, issued_at=NOW)


def test_an_audience_list_that_contains_the_client_id_is_accepted():
    assert verify(logout_token(NOW, aud=["other-client", CLIENT_ID])) == LogoutClaims(sid=SID, sub=SUB, issued_at=NOW)


@pytest.mark.parametrize(("token", "reason"), [
    pytest.param(lambda: sign(logout_claims(NOW), key=signing_key("other")), "bad_signature",
                 id="signed-by-another-key"),
    pytest.param(lambda: sign(logout_claims(NOW), kid="rotated-away", key=None), "unknown_kid", id="unknown-kid"),
    pytest.param(lambda: logout_token(NOW, iss="https://elsewhere.invalid/"), "bad_issuer", id="wrong-issuer"),
    pytest.param(lambda: logout_token(NOW, aud="another-client"), "bad_audience", id="wrong-audience"),
    pytest.param(lambda: logout_token(NOW, aud=None), "bad_audience", id="no-audience"),
    pytest.param(lambda: logout_token(NOW, iat=None), "missing_iat", id="no-iat"),
    pytest.param(lambda: logout_token(NOW, exp=None), "missing_exp", id="no-exp"),
    pytest.param(lambda: logout_token(NOW, iat=int(NOW.timestamp()) - 120, exp=int(NOW.timestamp()) - 60),
                 "expired", id="expired-at-the-skew"),
    pytest.param(lambda: logout_token(NOW, iat=int(NOW.timestamp()) + 61, exp=int(NOW.timestamp()) + 181),
                 "issued_in_future", id="issued-beyond-the-skew"),
    pytest.param(lambda: logout_token(NOW, events=None), "missing_event", id="no-events"),
    pytest.param(lambda: logout_token(NOW, events={"http://schemas.openid.net/event/other": {}}),
                 "missing_event", id="other-event"),
    pytest.param(lambda: logout_token(NOW, nonce="n-1"), "has_nonce", id="nonce-present"),
    pytest.param(lambda: logout_token(NOW, sid=None, sub=None), "no_subject", id="neither-sid-nor-sub"),
    pytest.param(lambda: logout_token(NOW, sid=7), "malformed_claim", id="sid-not-a-string"),
    pytest.param(lambda: logout_token(NOW, exp=float("nan")), "malformed_claim", id="exp-nan"),
    pytest.param(lambda: logout_token(NOW, exp=float("inf")), "malformed_claim", id="exp-infinite"),
    pytest.param(lambda: logout_token(NOW, iat=float("inf")), "malformed_claim", id="iat-infinite"),
    pytest.param(lambda: logout_token(NOW, iat=float("nan")), "malformed_claim", id="iat-nan"),
    pytest.param(lambda: logout_token(NOW, sid=None, sub=""), "no_subject", id="empty-sub-and-sid"),
    pytest.param(lambda: "not-a-jwt", "malformed", id="not-a-token"),
    pytest.param(lambda: jwt.encode({"alg": "HS256", "kid": "test-key"}, logout_claims(NOW),
                                    OctKey.generate_key(256)), "malformed", id="hs256-not-allowed"),
])
def test_each_failed_check_is_refused_with_its_reason(token, reason):
    with pytest.raises(LogoutTokenRefused) as refused:
        verify(token())
    assert refused.value.reason == reason


def test_an_integer_beyond_the_float_range_is_a_number_and_is_not_a_crash():
    claims = verify(logout_token(NOW, exp=10 ** 400))

    assert claims == LogoutClaims(sid=SID, sub=SUB, issued_at=NOW)


def test_a_malformed_key_set_is_refused_as_bad_keys():
    with pytest.raises(LogoutTokenRefused) as refused:
        verify(logout_token(NOW), keys={"keys": "not-a-list"})
    assert refused.value.reason == "bad_keys"


def test_end_session_url_carries_exactly_the_hint_and_the_signed_out_address():
    url = end_session_url(END_SESSION, id_token="header.payload.signature", public_url="https://hallpass.test")

    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == END_SESSION
    query = parse_qs(parts.query, keep_blank_values=True)
    assert query == {"id_token_hint": ["header.payload.signature"],
                     "post_logout_redirect_uri": ["https://hallpass.test/signed-out"]}


async def test_authlib_adapter_reads_the_issuer_the_endpoint_and_the_keys():
    stub = StubOidcClient(metadata={"issuer": ISSUER, "end_session_endpoint": END_SESSION}, jwks=jwks("k1"))
    oidc = AuthlibOidc(metadata_url="https://authentik.test/.well-known", client_id=CLIENT_ID, client_secret="s")
    oidc._client = stub

    provider = await oidc.provider(refresh_keys=True)

    assert provider == Provider(issuer=ISSUER, end_session_endpoint=END_SESSION, jwks=jwks("k1"))
    assert stub.jwks_calls == [True]


async def test_authlib_adapter_turns_a_discovery_failure_into_a_provider_error_without_the_text():
    stub = StubOidcClient(metadata_error=httpx.ConnectError(LEAK))
    oidc = AuthlibOidc(metadata_url="https://authentik.test/.well-known", client_id=CLIENT_ID, client_secret="s")
    oidc._client = stub

    with pytest.raises(ProviderError) as failed:
        await oidc.provider()

    assert failed.value.reason == "provider_unavailable"
    assert LEAK not in str(failed.value)


async def test_authlib_adapter_keeps_the_sid_and_the_raw_id_token_from_sign_in():
    stub = StubOidcClient(token={"id_token": "raw.id.token",
                                 "userinfo": {"sub": SUB, "preferred_username": "kid1", "sid": SID}})
    oidc = AuthlibOidc(metadata_url="https://authentik.test/.well-known", client_id=CLIENT_ID, client_secret="s")
    oidc._client = stub

    identity = await oidc.finish(None)

    assert (identity.sub, identity.sid, identity.id_token) == (SUB, SID, "raw.id.token")


async def test_authlib_adapter_leaves_the_sid_empty_when_the_id_token_has_none():
    stub = StubOidcClient(token={"id_token": "raw.id.token", "userinfo": {"sub": SUB}})
    oidc = AuthlibOidc(metadata_url="https://authentik.test/.well-known", client_id=CLIENT_ID, client_secret="s")
    oidc._client = stub

    identity = await oidc.finish(None)

    assert identity.sid is None
    assert identity.id_token == "raw.id.token"


async def test_concurrent_forced_key_refetches_share_one_fetch():
    stub = StubOidcClient(metadata={"issuer": ISSUER, "end_session_endpoint": END_SESSION}, jwks=jwks("k1"))
    oidc = AuthlibOidc(metadata_url="https://authentik.test/.well-known", client_id=CLIENT_ID, client_secret="s",
                       monotonic=lambda: 0.0)
    oidc._client = stub

    await asyncio.gather(oidc.provider(refresh_keys=True), oidc.provider(refresh_keys=True))

    assert stub.jwks_calls == [True, False]


async def test_a_refresh_that_waits_on_the_lock_gets_the_rotated_keys_not_the_cached_ones():
    stub = StubOidcClient(metadata={"issuer": ISSUER, "end_session_endpoint": END_SESSION}, jwks=jwks("k1"))
    stub.seed(jwks("k1"))
    stub.rotate(jwks("k2"))
    oidc = AuthlibOidc(metadata_url="https://authentik.test/.well-known", client_id=CLIENT_ID, client_secret="s",
                       monotonic=lambda: 0.0)
    oidc._client = stub

    first, second = await asyncio.gather(oidc.provider(refresh_keys=True), oidc.provider(refresh_keys=True))

    assert (first.jwks, second.jwks) == (jwks("k2"), jwks("k2"))
    assert stub.jwks_calls == [True, False]


def test_a_token_expired_just_inside_the_clock_skew_is_accepted():
    now = int(NOW.timestamp())

    claims = verify(logout_token(NOW, iat=now - 120, exp=now - CLOCK_SKEW_SECONDS + 1))

    assert claims.sub == SUB


def test_a_token_issued_at_the_edge_of_the_clock_skew_ahead_is_accepted_and_its_time_is_kept():
    now = int(NOW.timestamp())

    claims = verify(logout_token(NOW, iat=now + CLOCK_SKEW_SECONDS, exp=now + 180))

    assert claims.issued_at == NOW + timedelta(seconds=CLOCK_SKEW_SECONDS)


def test_a_token_longer_than_8192_characters_is_too_large_before_any_key_is_read():
    with pytest.raises(LogoutTokenRefused) as refused:
        verify("x" * 8193, keys={"keys": "not-a-list"})
    assert refused.value.reason == "too_large"


def test_a_token_of_exactly_8192_characters_is_not_called_too_large():
    with pytest.raises(LogoutTokenRefused) as refused:
        verify("x" * 8192)
    assert refused.value.reason == "malformed"
