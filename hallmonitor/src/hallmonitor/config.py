# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Environment-variable configuration. The process reads only its environment; it never loads a .env file."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MIN_TOKEN_LENGTH = 32
MIN_INTERVAL_SECONDS = 10
SERVICE_PREFIX = "HM_SERVICE_"
SECRET_KEY_PARTS = ("password", "key", "token")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_INTEGER = re.compile(r"[0-9]{1,9}")
SERVICE_SLUG_MAX = 64
# The same slug rule as the Console (its services.is_valid_slug), kept as a separate copy: the projects share no code.
_SERVICE_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SERVICE_KEY = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")


class ConfigError(ValueError):
    """Message names the variable, never its value."""


def _opt(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _required(env: Mapping[str, str], name: str) -> str:
    value = _opt(env, name)
    if value is None:
        raise ConfigError(f"{name} is required")
    return value


def _http_url(env: Mapping[str, str], name: str) -> str:
    """A HallPass Console URL. The bearer token travels to it, so it must be http or https; plain http is for a private
    network the operator controls."""
    value = _required(env, name)
    message = f"{name} must be an http:// or https:// URL"
    try:
        parts = urlsplit(value)
    except ValueError:           # never let the raw value surface
        raise ConfigError(message) from None
    if parts.scheme not in ("http", "https") or not parts.netloc or any(c.isspace() for c in value):
        raise ConfigError(message)
    return value


def _admin_url(env: Mapping[str, str], name: str) -> str:
    """The Fluxer admin base. The admin key travels to it: https, or http on a loopback host for local work."""
    value = _required(env, name)
    message = f"{name} must be an https:// URL, or http:// on 127.0.0.1, ::1 or localhost"
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise ConfigError(message) from None
    if not parts.netloc or any(c.isspace() for c in value):
        raise ConfigError(message)
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK_HOSTS):
        return value
    raise ConfigError(message)


def _token(env: Mapping[str, str], name: str) -> str:
    value = _required(env, name)
    if len(value) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"{name} must be at least {MIN_TOKEN_LENGTH} characters")
    return value


def _integer(env: Mapping[str, str], name: str, default: str, minimum: int) -> int:
    raw = _opt(env, name) or default
    if not _INTEGER.fullmatch(raw) or int(raw) < minimum:
        raise ConfigError(f"{name} must be an integer of at least {minimum}")
    return int(raw)


def _log_level(env: Mapping[str, str], name: str) -> str:
    level = (_opt(env, name) or "INFO").upper()
    if level not in LOG_LEVELS:
        raise ConfigError(f"{name} must be one of {', '.join(LOG_LEVELS)}")
    return level


def _service_settings(env: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """HM_SERVICE_<SLUG>__<KEY> variables, grouped by service. The name splits at its first `__`. The slug is
    lower-cased with `_` turned into `-`, and it must be a valid slug: single hyphens only, up to 64 characters. The key
    is lower-cased, and it must be lower-case letters and digits joined by single underscores, so a `__` or a leading
    or trailing `_` in the key is refused. An empty value is not a setting, so a blank line in the template changes
    nothing."""
    settings: dict[str, dict[str, str]] = {}
    for name, raw in env.items():
        if not name.startswith(SERVICE_PREFIX):
            continue
        slug_part, separator, key_part = name[len(SERVICE_PREFIX):].partition("__")
        if not separator or not slug_part or not key_part:
            raise ConfigError(f"{name} must be HM_SERVICE_<SLUG>__<KEY>")
        slug = slug_part.lower().replace("_", "-")
        if len(slug) > SERVICE_SLUG_MAX or not _SERVICE_SLUG.fullmatch(slug):
            raise ConfigError(f"{name} names a service slug that is not valid")
        key = key_part.lower()
        if not _SERVICE_KEY.fullmatch(key):
            raise ConfigError(f"{name} must be HM_SERVICE_<SLUG>__<KEY>, with a key of letters and digits joined by _")
        value = raw.strip()
        if value:
            settings.setdefault(slug, {})[key] = value
    return settings


@dataclass(frozen=True)
class Config:
    hallpass_url: str
    hallpass_token: str = field(repr=False)
    data_dir: Path = Path("data")
    interval_seconds: int = 60
    log_level: str = "INFO"
    fluxer_admin_url: str | None = None
    fluxer_admin_key: str | None = field(default=None, repr=False)
    service_settings: Mapping[str, Mapping[str, str]] = field(default_factory=dict, repr=False)

    def secrets(self) -> list[str]:
        """Every non-empty secret value, for the log redaction filter. Add each new secret field here."""
        values: list[str | None] = [self.hallpass_token, self.fluxer_admin_key]
        for settings in self.service_settings.values():
            values.extend(value for key, value in settings.items() if any(part in key for part in SECRET_KEY_PARTS))
        return [value for value in values if value]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        url = _opt(env, "FLUXER_ADMIN_URL")
        key = _opt(env, "FLUXER_ADMIN_KEY")
        if (url is None) != (key is None):
            missing = "FLUXER_ADMIN_KEY" if key is None else "FLUXER_ADMIN_URL"
            raise ConfigError(f"{missing} is required when the other Fluxer admin variable is set")
        fluxer_url = None if url is None else _admin_url(env, "FLUXER_ADMIN_URL")
        data_dir = _opt(env, "HALLMONITOR_DATA_DIR") or "data"
        return cls(
            hallpass_url=_http_url(env, "HALLPASS_MONITOR_URL").rstrip("/"),
            hallpass_token=_token(env, "HALLPASS_MONITOR_TOKEN"),
            data_dir=Path(data_dir),
            interval_seconds=_integer(env, "HALLMONITOR_INTERVAL_SECONDS", "60", MIN_INTERVAL_SECONDS),
            log_level=_log_level(env, "HALLMONITOR_LOG_LEVEL"),
            fluxer_admin_url=fluxer_url,
            fluxer_admin_key=key,
            service_settings=_service_settings(env),
        )
