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
SMTP_PASSWORD = "smtp-password-for-tests"

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
    "SMTP_HOST": "smtp.hallpass.invalid",
    "SMTP_PORT": "2525",
    "SMTP_USERNAME": "hallpass-mailer",
    "SMTP_PASSWORD": SMTP_PASSWORD,
    "SMTP_FROM": "hallpass@hallpass.invalid",
    "SMTP_STARTTLS": "false",
    "HALLPASS_ADMIN_EMAIL": "admin@hallpass.invalid",
    "HALLPASS_CONFLICT_WINDOW_HOURS": "12",
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
    assert config.smtp_host == "smtp.hallpass.invalid"
    assert config.smtp_port == 2525
    assert config.smtp_username == "hallpass-mailer"
    assert config.smtp_password == SMTP_PASSWORD
    assert config.smtp_from == "hallpass@hallpass.invalid"
    assert config.smtp_starttls is False
    assert config.admin_email == "admin@hallpass.invalid"
    assert config.conflict_window_hours == 12


def test_smtp_and_admin_email_are_optional_when_blank() -> None:
    env = {**GOOD_ENV, "SMTP_HOST": "", "SMTP_FROM": "", "SMTP_PASSWORD": "", "SMTP_PORT": "",
           "SMTP_STARTTLS": "", "HALLPASS_ADMIN_EMAIL": ""}
    config = Config.from_env(env)
    assert config.smtp_host is None
    assert config.smtp_from is None
    assert config.smtp_password is None
    assert config.smtp_port == 587
    assert config.smtp_starttls is True
    assert config.admin_email is None


def test_smtp_settings_are_absent_when_no_host_is_set() -> None:
    env = {key: value for key, value in GOOD_ENV.items() if not key.startswith("SMTP_")}
    config = Config.from_env(env)
    assert config.smtp_host is None and config.smtp_password is None and config.smtp_from is None


@pytest.mark.parametrize("bad", ["0", "65536", "99999", "not-a-port"])
def test_smtp_port_outside_1_to_65535_is_refused_and_named(bad: str) -> None:
    with pytest.raises(ConfigError, match="SMTP_PORT") as info:
        Config.from_env({**GOOD_ENV, "SMTP_PORT": bad})
    assert bad not in str(info.value)


@pytest.mark.parametrize("bad", ["maybe", "yes-please"])
def test_smtp_starttls_must_be_true_or_false(bad: str) -> None:
    with pytest.raises(ConfigError, match="SMTP_STARTTLS"):
        Config.from_env({**GOOD_ENV, "SMTP_STARTTLS": bad})


@pytest.mark.parametrize("bad", ["0", "-3", "soon"])
def test_conflict_window_must_be_a_positive_integer(bad: str) -> None:
    with pytest.raises(ConfigError, match="HALLPASS_CONFLICT_WINDOW_HOURS"):
        Config.from_env({**GOOD_ENV, "HALLPASS_CONFLICT_WINDOW_HOURS": bad})


def test_smtp_host_needs_a_from_address() -> None:
    with pytest.raises(ConfigError, match="SMTP_FROM"):
        Config.from_env({**GOOD_ENV, "SMTP_FROM": ""})


@pytest.mark.parametrize("name", ["SMTP_FROM", "HALLPASS_ADMIN_EMAIL"])
def test_malformed_email_is_refused_without_echoing_it(name: str) -> None:
    bad = "not an address"
    with pytest.raises(ConfigError, match=name) as info:
        Config.from_env({**GOOD_ENV, name: bad})
    assert bad not in str(info.value)


def test_malformed_smtp_host_is_refused_without_echoing_it() -> None:
    bad = "smtp host/with path"
    with pytest.raises(ConfigError, match="SMTP_HOST") as info:
        Config.from_env({**GOOD_ENV, "SMTP_HOST": bad})
    assert bad not in str(info.value)


def test_defaults_apply_when_optional_variables_are_blank() -> None:
    env = {**GOOD_ENV, "HALLPASS_SESSION_MAX_AGE_SECONDS": "", "HALLPASS_DATA_DIR": "  ", "HALLPASS_BIND": "",
           "HALLPASS_INVITE_EXPIRY_HOURS": "", "HALLPASS_LOG_LEVEL": "", "HALLPASS_COOKIE_DOMAIN": "",
           "HALLPASS_ADMIN_SUBS": "", "HALLPASS_CONFLICT_WINDOW_HOURS": ""}
    config = Config.from_env(env)
    assert config.session_max_age_seconds == 43200
    assert config.conflict_window_hours == 24
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
    "http://example.com:9000/",
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
    for secret in (SESSION_SECRET, AUTHENTIK_TOKEN, OIDC_SECRET, SMTP_PASSWORD):
        assert secret not in text


def test_secrets_lists_every_secret_value_and_skips_empty_ones() -> None:
    config = Config.from_env(GOOD_ENV)
    assert sorted(config.secrets()) == sorted([SESSION_SECRET, AUTHENTIK_TOKEN, OIDC_SECRET, SMTP_PASSWORD])
    without_password = Config.from_env({**GOOD_ENV, "SMTP_PASSWORD": ""})
    assert sorted(without_password.secrets()) == sorted([SESSION_SECRET, AUTHENTIK_TOKEN, OIDC_SECRET])


def test_config_is_frozen() -> None:
    config = Config.from_env(GOOD_ENV)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.public_url = "https://other.invalid"   # type: ignore[misc]


def test_env_example_lists_every_config_variable() -> None:
    template = (PROJECT / ".env.example").read_text("utf-8")
    listed = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", template, re.MULTILINE))
    assert set(REQUIRED) <= listed
    assert set(GOOD_ENV) <= listed, f"missing from .env.example: {sorted(set(GOOD_ENV) - listed)}"
