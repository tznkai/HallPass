# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared harness for the hand-run Authentik live checks: environment, client, redacted output, prompts."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

import httpx

DIGIT_RUN = re.compile(r"[0-9]{15,}")
REDACTED = "<redacted>"


class LiveConfigError(RuntimeError):
    """A required environment variable is unset. The message names the variable, never its value."""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LiveConfigError(f"{name} is not set")
    return value


def instance_secrets(base_url: str) -> list[str]:
    """The forms of the instance address that must never reach the terminal log."""
    url = httpx.URL(base_url)
    return [base_url, url.host, url.netloc.decode("ascii")]


def make_client(base_url: str, token: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
        transport=transport,
    )


class Console:
    """The only print path. Every line, prompt and error passes the redactor first."""

    def __init__(self, secrets: Iterable[str]) -> None:
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return DIGIT_RUN.sub("<id>", text)

    def line(self, text: str) -> None:
        print(self.redact(text), flush=True)

    def step(self, name: str, result: str) -> None:
        self.line(f"STEP {name}: {result}")

    def error(self, exc: BaseException) -> str:
        return self.redact(f"{type(exc).__name__}: {exc}")

    def ask(self, question: str) -> str:
        return input(self.redact(question) + " ").strip()

    def ask_bool(self, question: str) -> bool:
        while True:
            answer = self.ask(f"{question} [y/n]").lower()
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
