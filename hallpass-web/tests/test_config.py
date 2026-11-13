# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config parsing: required variables, refusals name the variable and never its value, and the template lists all."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from hallpass.config import Config, ConfigError

PROJECT = Path(__file__).resolve().parents[1]
ADMIN = "00000000-0000-4000-8000-000000000001"
SESSION_SECRET = "session-secret-for-tests-only-0123456789"
AUTHENTIK_TOKEN = "authentik-token-for-tests"
OIDC_SECRET = "oidc-client-secret-for-tests"

GOOD_ENV = {
    "HALLPASS_PUBLIC_URL": "https://hallpass.invalid/",
    "HALLPASS_SESSION_SECRET": SESSION_SECRET,
    "HALLPASS_COOKIE_DOMAIN": ".hallpass.invalid",
    "HALLPASS_SESSION_MAX_AGE_SECONDS": "3600",
    "HALLPASS_DATA_DIR": "state",
    "HALLPASS_BIND": "127.0.0.1:9090",
    "HALLPASS_ADMIN_SUBS": ADMIN,
    "HALLPASS_INVITE_EXPIRY_HOURS": "24",
    "HALLPASS_LOG_LEVEL": "debug",
    "AUTHENTIK_URL": "https://authentik.invalid",
    "AUTHENTIK_TOKEN": AUTHENTIK_TOKEN,
    "AUTHENTIK_INVITE_FLOW": "hallpass-invite",
    "OIDC_METADATA_URL": "https://authentik.invalid/application/o/hallpass/.well-known/openid-configuration",
    "OIDC_CLIENT_ID": "hallpass-client",
    "OIDC_CLIENT_SECRET": OIDC_SECRET,
}

REQUIRED = [
    "HALLPASS_PUBLIC_URL", "HALLPASS_SESSION_SECRET", "AUTHENTIK_URL", "AUTHENTIK_TOKEN",
    "AUTHENTIK_INVITE_FLOW", "OIDC_METADATA_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET",
]


def test_complete_env_parses_with_every_field() -> None:
    config = Config.from_env(GOOD_ENV)
    assert config.public_url == "https://hallpass.invalid"
    assert config.session_secret == SESSION_SECRET
    assert config.cookie_domain == ".hallpass.invalid"
    assert config.session_max_age_seconds == 3600
    assert config.data_dir == Path("state")
    assert (config.bind_host, config.bind_port) == ("127.0.0.1", 9090)
    assert config.admin_subs == frozenset({ADMIN})
    assert config.invite_expiry_hours == 24
    assert config.log_level == "DEBUG"
    assert config.authentik_url == "https://authentik.invalid"
    assert config.authentik_token == AUTHENTIK_TOKEN
    assert config.authentik_invite_flow == "hallpass-invite"
    assert config.oidc_client_id == "hallpass-client"
    assert config.oidc_client_secret == OIDC_SECRET


def test_defaults_apply_when_optional_variables_are_blank() -> None:
    env = {**GOOD_ENV, "HALLPASS_SESSION_MAX_AGE_SECONDS": "", "HALLPASS_DATA_DIR": "  ", "HALLPASS_BIND": "",
           "HALLPASS_INVITE_EXPIRY_HOURS": "", "HALLPASS_LOG_LEVEL": "", "HALLPASS_COOKIE_DOMAIN": "",
           "HALLPASS_ADMIN_SUBS": ""}
    config = Config.from_env(env)
    assert config.session_max_age_seconds == 43200
    assert config.data_dir == Path("data")
    assert (config.bind_host, config.bind_port) == ("127.0.0.1", 8080)
    assert config.invite_expiry_hours == 168
    assert config.log_level == "INFO"
    assert config.cookie_domain is None
    assert config.admin_subs == frozenset()


@pytest.mark.parametrize("name", REQUIRED)
def test_each_required_variable_missing_names_that_variable(name: str) -> None:
    env = {key: value for key, value in GOOD_ENV.items() if key != name}
    with pytest.raises(ConfigError) as info:
        Config.from_env(env)
    assert name in str(info.value)


def test_short_session_secret_is_refused_without_echoing_it() -> None:
    short = "too-short-secret"
    with pytest.raises(ConfigError, match="HALLPASS_SESSION_SECRET") as info:
        Config.from_env({**GOOD_ENV, "HALLPASS_SESSION_SECRET": short})
    assert short not in str(info.value)


@pytest.mark.parametrize("bad", ["not-a-uuid", f"{ADMIN},bogus", "0000"])
def test_invalid_admin_uuid_is_refused_and_named(bad: str) -> None:
    with pytest.raises(ConfigError, match="HALLPASS_ADMIN_SUBS") as info:
        Config.from_env({**GOOD_ENV, "HALLPASS_ADMIN_SUBS": bad})
    assert bad not in str(info.value)


def test_public_url_must_be_https() -> None:
    with pytest.raises(ConfigError, match="HALLPASS_PUBLIC_URL"):
        Config.from_env({**GOOD_ENV, "HALLPASS_PUBLIC_URL": "http://hallpass.invalid"})


SERVICE_FIELDS = {"AUTHENTIK_URL": "authentik_url", "OIDC_METADATA_URL": "oidc_metadata_url"}
LOOPBACK_URLS = ["http://127.0.0.1:9000", "http://[::1]:9000", "http://localhost:9000"]


@pytest.mark.parametrize("name", list(SERVICE_FIELDS))
@pytest.mark.parametrize("url", LOOPBACK_URLS)
def test_http_is_accepted_for_a_loopback_service(name: str, url: str) -> None:
    config = Config.from_env({**GOOD_ENV, name: url})
    assert getattr(config, SERVICE_FIELDS[name]) == url


@pytest.mark.parametrize("name", list(SERVICE_FIELDS))
@pytest.mark.parametrize("url", [
    "http://authentik.invalid/",
    "http://192.0.2.10:9000/",
    "http://127.0.0.2:9000/",
    "http://localhost.invalid:9000/",
    "ftp://authentik.invalid/",
])
def test_plain_http_to_another_host_is_refused_and_named(name: str, url: str) -> None:
    with pytest.raises(ConfigError, match=name) as info:
        Config.from_env({**GOOD_ENV, name: url})
    assert url not in str(info.value)


@pytest.mark.parametrize("name", list(SERVICE_FIELDS))
def test_https_is_accepted_for_a_service(name: str) -> None:
    config = Config.from_env({**GOOD_ENV, name: "https://authentik.invalid/x"})
    assert getattr(config, SERVICE_FIELDS[name]) == "https://authentik.invalid/x"


@pytest.mark.parametrize("name", list(SERVICE_FIELDS))
def test_an_unparseable_service_url_is_refused_without_its_value(name: str) -> None:
    bad = "https://[::1"
    with pytest.raises(ConfigError, match=name) as info:
        Config.from_env({**GOOD_ENV, name: bad})
    assert bad not in str(info.value)


def test_bad_bind_and_bad_cookie_domain_are_refused() -> None:
    with pytest.raises(ConfigError, match="HALLPASS_BIND"):
        Config.from_env({**GOOD_ENV, "HALLPASS_BIND": "no-port-here"})
    with pytest.raises(ConfigError, match="HALLPASS_COOKIE_DOMAIN"):
        Config.from_env({**GOOD_ENV, "HALLPASS_COOKIE_DOMAIN": "not a domain"})


def test_repr_contains_none_of_the_secret_values() -> None:
    text = repr(Config.from_env(GOOD_ENV))
    for secret in (SESSION_SECRET, AUTHENTIK_TOKEN, OIDC_SECRET):
        assert secret not in text


def test_secrets_lists_the_three_secret_values_and_skips_empty_ones() -> None:
    config = Config.from_env(GOOD_ENV)
    assert sorted(config.secrets()) == sorted([SESSION_SECRET, AUTHENTIK_TOKEN, OIDC_SECRET])


def test_config_is_frozen() -> None:
    config = Config.from_env(GOOD_ENV)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.public_url = "https://other.invalid"   # type: ignore[misc]


def test_env_example_lists_every_config_variable() -> None:
    template = (PROJECT / ".env.example").read_text("utf-8")
    listed = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", template, re.MULTILINE))
    assert set(REQUIRED) <= listed
    assert set(GOOD_ENV) <= listed, f"missing from .env.example: {sorted(set(GOOD_ENV) - listed)}"
