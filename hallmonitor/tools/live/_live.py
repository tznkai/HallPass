# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared harness for the hand-run Fluxer live checks: environment, client, redacted output."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

import httpx

DIGIT_RUN = re.compile(r"[0-9]{15,}")
REDACTED = "<redacted>"
AUDIT_LOG_REASON = "hallmonitor live check"


class LiveConfigError(RuntimeError):
    """A required environment variable is unset. The message names the variable, never its value."""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LiveConfigError(f"{name} is not set")
    return value


def instance_secrets(base_url: str) -> list[str]:
    """The forms of the instance address that must never reach the terminal log. A malformed URL keeps its raw form."""
    try:
        url = httpx.URL(base_url)
    except httpx.InvalidURL:
        return [base_url]
    return [base_url, url.host, url.netloc.decode("ascii", "replace")]


def make_client(base_url: str, key: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Admin {key}", "X-Audit-Log-Reason": AUDIT_LOG_REASON},
        timeout=30.0,
        transport=transport,
    )


class Console:
    """The only print path. Every line and error passes the redactor first."""

    def __init__(self, secrets: Iterable[str]) -> None:
        # Longest first: in an alternation the first branch that matches wins, so a longer secret that contains
        # a shorter one is replaced whole. Matching ignores case, since a host may be echoed in another spelling.
        ordered = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(s) for s in ordered), re.IGNORECASE) if ordered else None

    def redact(self, text: str) -> str:
        if self._pattern is not None:
            text = self._pattern.sub(REDACTED, text)
        return DIGIT_RUN.sub("<id>", text)

    def line(self, text: str) -> None:
        print(self.redact(text), flush=True)

    def step(self, name: str, result: str) -> None:
        self.line(f"STEP {name}: {result}")

    def error(self, exc: BaseException) -> str:
        return self.redact(f"{type(exc).__name__}: {exc}")
