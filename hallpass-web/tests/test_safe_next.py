# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""safe_next, the only check on where a sign-in returns the person: absolute URLs on this host or the cookie domain."""

from __future__ import annotations

import pytest

from hallpass.config import Config
from hallpass.web.routes_auth import safe_next

BASE_ENV = {
    "HALLPASS_PUBLIC_URL": "https://hallpass.test",
    "HALLPASS_SESSION_SECRET": "session-secret-for-tests-only-0123456789",
    "AUTHENTIK_URL": "https://authentik.test",
    "AUTHENTIK_TOKEN": "test-token",
    "AUTHENTIK_INVITE_FLOW": "invite",
    "OIDC_METADATA_URL": "https://authentik.test/application/o/hallpass/.well-known/openid-configuration",
    "OIDC_CLIENT_ID": "hallpass-client",
    "OIDC_CLIENT_SECRET": "oidc-secret-for-tests-only",
}
COOKIE_DOMAIN = ".family.test"


def config_with(cookie_domain: str | None) -> Config:
    env = dict(BASE_ENV)
    if cookie_domain is not None:
        env["HALLPASS_COOKIE_DOMAIN"] = cookie_domain
    return Config.from_env(env)


@pytest.mark.parametrize(("cookie_domain", "value", "accepted"), [
    # The public host is accepted with and without a cookie domain.
    pytest.param(None, "https://hallpass.test/x", True, id="public-host"),
    pytest.param(COOKIE_DOMAIN, "https://hallpass.test/x", True, id="public-host-with-cookie-domain"),
    # The cookie-domain host and its subdomains are accepted only when a cookie domain is set.
    pytest.param(COOKIE_DOMAIN, "https://family.test/x", True, id="cookie-domain-host"),
    pytest.param(COOKIE_DOMAIN, "https://kid.family.test/x", True, id="cookie-domain-subdomain"),
    pytest.param(None, "https://family.test/x", False, id="cookie-domain-host-without-cookie-domain"),
    pytest.param(None, "https://kid.family.test/x", False, id="subdomain-without-cookie-domain"),
    # Look-alike suffixes are not subdomains.
    pytest.param(None, "https://evilhallpass.test/", False, id="lookalike-public-host"),
    pytest.param(COOKIE_DOMAIN, "https://evilfamily.test/", False, id="lookalike-cookie-domain"),
    # Userinfo can make the host look right; both orders are refused.
    pytest.param(None, "https://hallpass.test@evil.invalid/", False, id="userinfo-after-public-host"),
    pytest.param(None, "https://evil.invalid@hallpass.test/", False, id="userinfo-before-public-host"),
    pytest.param(COOKIE_DOMAIN, "https://hallpass.test@evil.invalid/", False, id="userinfo-with-cookie-domain"),
    pytest.param(COOKIE_DOMAIN, "https://evil.invalid@family.test/", False, id="userinfo-before-cookie-domain"),
    # A port on the cookie-domain host is refused, as is a port on the public host.
    pytest.param(COOKIE_DOMAIN, "https://family.test:8443/x", False, id="port-on-cookie-domain-host"),
    pytest.param(COOKIE_DOMAIN, "https://kid.family.test:8443/x", False, id="port-on-cookie-domain-subdomain"),
    pytest.param(None, "https://hallpass.test:8443/x", False, id="port-on-public-host"),
    # A URL that urlsplit cannot parse is refused, not raised.
    pytest.param(None, "https://[::1/x", False, id="unparseable"),
    pytest.param(COOKIE_DOMAIN, "https://[::1/x", False, id="unparseable-with-cookie-domain"),
])
def test_safe_next_accepts_and_refuses_absolute_urls(cookie_domain: str | None, value: str, accepted: bool):
    assert safe_next(value, config_with(cookie_domain)) == (value if accepted else None)
