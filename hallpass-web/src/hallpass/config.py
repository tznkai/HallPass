# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Environment-variable configuration. The process reads only its environment; it never loads a .env file."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MIN_SESSION_SECRET_LENGTH = 32
_POSITIVE_INT = re.compile(r"[1-9][0-9]{0,9}")
_PORT = re.compile(r"[1-9][0-9]{0,4}")
_COOKIE_DOMAIN = re.compile(r"\.?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
_FLOW_SLUG = re.compile(r"[A-Za-z0-9_-]{1,100}")


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


def _url(env: Mapping[str, str], name: str, schemes: tuple[str, ...]) -> str:
    value = _required(env, name)
    message = f"{name} must be a {' or '.join(s + '://' for s in schemes)} URL"
    try:
        parts = urlsplit(value)
    except ValueError:           # e.g. "http://[::1" — never let the raw value surface
        raise ConfigError(message) from None
    if parts.scheme not in schemes or not parts.netloc or any(c.isspace() for c in value):
        raise ConfigError(message)
    return value


def _positive_int(env: Mapping[str, str], name: str, default: str) -> int:
    raw = _opt(env, name) or default
    if not _POSITIVE_INT.fullmatch(raw):
        raise ConfigError(f"{name} must be a positive integer")
    return int(raw)


def _bind(env: Mapping[str, str], name: str) -> tuple[str, int]:
    raw = _opt(env, name) or "127.0.0.1:8080"
    host, sep, port = raw.rpartition(":")
    host = host.removeprefix("[").removesuffix("]")
    if not sep or not host or not _PORT.fullmatch(port) or int(port) > 65535:
        raise ConfigError(f"{name} must be host:port")
    return host, int(port)


def _admin_subs(env: Mapping[str, str], name: str) -> frozenset[str]:
    raw = _opt(env, name)
    if raw is None:
        return frozenset()
    subs = set()
    for part in raw.split(","):
        try:
            subs.add(str(uuid.UUID(part.strip())))
        except ValueError:
            raise ConfigError(f"{name} must be comma-separated uuids") from None
    return frozenset(subs)


@dataclass(frozen=True)
class Config:
    public_url: str
    session_secret: str = field(repr=False)
    authentik_url: str
    authentik_token: str = field(repr=False)
    authentik_invite_flow: str
    oidc_metadata_url: str
    oidc_client_id: str
    oidc_client_secret: str = field(repr=False)
    cookie_domain: str | None = None
    session_max_age_seconds: int = 43200
    data_dir: Path = Path("data")
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    admin_subs: frozenset[str] = frozenset()
    invite_expiry_hours: int = 168
    log_level: str = "INFO"

    def secrets(self) -> list[str]:
        """Every non-empty secret value, for the log redaction filter. Add each new secret field here."""
        return [value for value in (self.session_secret, self.authentik_token, self.oidc_client_secret) if value]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        public_url = _url(env, "HALLPASS_PUBLIC_URL", ("https",)).rstrip("/")
        session_secret = _required(env, "HALLPASS_SESSION_SECRET")
        if len(session_secret) < MIN_SESSION_SECRET_LENGTH:
            raise ConfigError(f"HALLPASS_SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters")
        cookie_domain = _opt(env, "HALLPASS_COOKIE_DOMAIN")
        if cookie_domain is not None and not _COOKIE_DOMAIN.fullmatch(cookie_domain):
            raise ConfigError("HALLPASS_COOKIE_DOMAIN must be a domain name such as .example.com")
        flow = _required(env, "AUTHENTIK_INVITE_FLOW")
        if not _FLOW_SLUG.fullmatch(flow):
            raise ConfigError("AUTHENTIK_INVITE_FLOW must be a flow slug")
        log_level = (_opt(env, "HALLPASS_LOG_LEVEL") or "INFO").upper()
        if log_level not in LOG_LEVELS:
            raise ConfigError(f"HALLPASS_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
        bind_host, bind_port = _bind(env, "HALLPASS_BIND")
        return cls(
            public_url=public_url,
            session_secret=session_secret,
            authentik_url=_url(env, "AUTHENTIK_URL", ("http", "https")),
            authentik_token=_required(env, "AUTHENTIK_TOKEN"),
            authentik_invite_flow=flow,
            oidc_metadata_url=_url(env, "OIDC_METADATA_URL", ("http", "https")),
            oidc_client_id=_required(env, "OIDC_CLIENT_ID"),
            oidc_client_secret=_required(env, "OIDC_CLIENT_SECRET"),
            cookie_domain=cookie_domain,
            session_max_age_seconds=_positive_int(env, "HALLPASS_SESSION_MAX_AGE_SECONDS", "43200"),
            data_dir=Path(_opt(env, "HALLPASS_DATA_DIR") or "data"),
            bind_host=bind_host,
            bind_port=bind_port,
            admin_subs=_admin_subs(env, "HALLPASS_ADMIN_SUBS"),
            invite_expiry_hours=_positive_int(env, "HALLPASS_INVITE_EXPIRY_HOURS", "168"),
            log_level=log_level,
        )
