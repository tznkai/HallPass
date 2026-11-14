# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Environment configuration: required names, validation messages that never echo a value, service settings."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hallmonitor.config import Config, ConfigError

TOKEN = "t" * 40
FLUXER_KEY = "k" * 40
RCON = "r" * 20


def base_env(**extra: str) -> dict[str, str]:
    env = {"HALLPASS_MONITOR_URL": "https://hallpass.invalid", "HALLPASS_MONITOR_TOKEN": TOKEN}
    env.update(extra)
    return env


def test_complete_env_loads_defaults() -> None:
    config = Config.from_env(base_env())
    assert config.hallpass_url == "https://hallpass.invalid"
    assert config.hallpass_token == TOKEN
    assert config.data_dir == Path("data")
    assert config.interval_seconds == 60
    assert config.log_level == "INFO"
    assert config.fluxer_admin_url is None
    assert config.fluxer_admin_key is None
    assert dict(config.service_settings) == {}


@pytest.mark.parametrize("name", ["HALLPASS_MONITOR_URL", "HALLPASS_MONITOR_TOKEN"])
def test_required_variables_are_checked(name: str) -> None:
    env = base_env()
    del env[name]
    with pytest.raises(ConfigError, match=name):
        Config.from_env(env)


def test_short_token_is_refused_without_echoing_it() -> None:
    short = "s3cret-short"
    with pytest.raises(ConfigError, match="HALLPASS_MONITOR_TOKEN") as info:
        Config.from_env(base_env(HALLPASS_MONITOR_TOKEN=short))
    assert short not in str(info.value)


@pytest.mark.parametrize("url", ["ftp://hallpass.invalid", "hallpass.invalid", "https://"])
def test_hallpass_url_must_be_http_or_https(url: str) -> None:
    with pytest.raises(ConfigError, match="HALLPASS_MONITOR_URL"):
        Config.from_env(base_env(HALLPASS_MONITOR_URL=url))


def test_interval_has_a_minimum_of_ten_seconds() -> None:
    assert Config.from_env(base_env(HALLMONITOR_INTERVAL_SECONDS="10")).interval_seconds == 10
    for bad in ("9", "0", "soon"):
        with pytest.raises(ConfigError, match="HALLMONITOR_INTERVAL_SECONDS"):
            Config.from_env(base_env(HALLMONITOR_INTERVAL_SECONDS=bad))


def test_log_level_is_checked() -> None:
    assert Config.from_env(base_env(HALLMONITOR_LOG_LEVEL="debug")).log_level == "DEBUG"
    with pytest.raises(ConfigError, match="HALLMONITOR_LOG_LEVEL"):
        Config.from_env(base_env(HALLMONITOR_LOG_LEVEL="verbose"))


def test_fluxer_url_and_key_are_both_or_neither() -> None:
    url_only = base_env(FLUXER_ADMIN_URL="https://fluxer.invalid")
    with pytest.raises(ConfigError, match="FLUXER_ADMIN_KEY"):
        Config.from_env(url_only)
    key_only = base_env(FLUXER_ADMIN_KEY=FLUXER_KEY)
    with pytest.raises(ConfigError, match="FLUXER_ADMIN_URL"):
        Config.from_env(key_only)
    both = Config.from_env(base_env(FLUXER_ADMIN_URL="https://fluxer.invalid", FLUXER_ADMIN_KEY=FLUXER_KEY))
    assert both.fluxer_admin_url == "https://fluxer.invalid"


def test_fluxer_admin_url_must_be_https_or_loopback() -> None:
    with pytest.raises(ConfigError, match="FLUXER_ADMIN_URL"):
        Config.from_env(base_env(FLUXER_ADMIN_URL="http://fluxer.invalid", FLUXER_ADMIN_KEY=FLUXER_KEY))
    loopback = Config.from_env(base_env(FLUXER_ADMIN_URL="http://127.0.0.1:9", FLUXER_ADMIN_KEY=FLUXER_KEY))
    assert loopback.fluxer_admin_url == "http://127.0.0.1:9"


def test_service_settings_are_parsed_from_variables() -> None:
    config = Config.from_env(base_env(**{"HM_SERVICE_MINECRAFT__RCON_PASSWORD": RCON,
                                         "HM_SERVICE_THE_SHIP__SSO_PROVIDER_ID": "prov-1"}))
    assert config.service_settings["minecraft"]["rcon_password"] == RCON
    assert config.service_settings["the-ship"]["sso_provider_id"] == "prov-1"


@pytest.mark.parametrize("name", ["HM_SERVICE_MINECRAFT__", "HM_SERVICE___RCON"])
def test_service_variable_needs_a_slug_and_a_key(name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        Config.from_env(base_env(**{name: "x"}))


# The name splits at its first `__`, so each of these splits there and the key after it is what fails the key rule:
# a `__` or a leading or trailing `_` in the key is refused.
@pytest.mark.parametrize("name", ["HM_SERVICE_A__B__HOST", "HM_SERVICE_A___HOST", "HM_SERVICE_THE_SHIP___HOST",
                                  "HM_SERVICE_A__HOST_"])
def test_service_variable_with_an_invalid_key_is_refused(name: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(f"{name} must be HM_SERVICE_<SLUG>__<KEY>")):
        Config.from_env(base_env(**{name: "x"}))


# The slug is the part before the first `__`, lower-cased with `_` turned into `-`. It must be a valid slug: no
# leading, trailing or doubled hyphen, only [a-z0-9] and single hyphens, and at most 64 characters.
@pytest.mark.parametrize("name", ["HM_SERVICE__A__HOST",                    # slug "-a": leading hyphen
                                  "HM_SERVICE_" + "A" * 65 + "__HOST",      # 65 characters
                                  "HM_SERVICE_A.B__HOST"])                  # "." is not in [a-z0-9]
def test_service_variable_with_an_invalid_slug_is_refused(name: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(f"{name} names a service slug that is not valid")):
        Config.from_env(base_env(**{name: "x"}))


def test_service_slug_with_single_hyphens_is_accepted() -> None:
    config = Config.from_env(base_env(**{"HM_SERVICE_A_B_C__HOST": "example"}))
    assert config.service_settings["a-b-c"]["host"] == "example"


def test_service_slug_of_64_characters_is_accepted() -> None:
    slug = "a" * 64
    config = Config.from_env(base_env(**{f"HM_SERVICE_{slug.upper()}__HOST": "example"}))
    assert config.service_settings[slug]["host"] == "example"


def test_secrets_hold_tokens_and_secret_service_settings_only() -> None:
    config = Config.from_env(base_env(FLUXER_ADMIN_URL="https://fluxer.invalid", FLUXER_ADMIN_KEY=FLUXER_KEY,
                                      **{"HM_SERVICE_MINECRAFT__RCON_PASSWORD": RCON,
                                         "HM_SERVICE_FLUXER__SSO_PROVIDER_ID": "prov-1"}))
    secrets = config.secrets()
    assert {TOKEN, FLUXER_KEY, RCON} <= set(secrets)
    assert "prov-1" not in secrets


def test_repr_hides_every_secret() -> None:
    config = Config.from_env(base_env(FLUXER_ADMIN_URL="https://fluxer.invalid", FLUXER_ADMIN_KEY=FLUXER_KEY,
                                      **{"HM_SERVICE_MINECRAFT__RCON_PASSWORD": RCON}))
    text = repr(config)
    for secret in (TOKEN, FLUXER_KEY, RCON):
        assert secret not in text
