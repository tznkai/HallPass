# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the live-check redactor and the live scripts' error exits (tools/live). No network."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "live"))
import authentik_check  # noqa: E402
import authentik_sessions_check  # noqa: E402
from _live import Console, instance_secrets  # noqa: E402

TOKEN = "SENTINEL-TOKEN-xyz"
HOST = "hallpass.invalid"
BASE = "https://hallpass.invalid:8443/api"
LONG_ID = "1" * 15  # built at runtime: no 15-digit run is written in this file
SHORT_ID = "1" * 14


def live_console() -> Console:
    return Console([TOKEN, *instance_secrets(BASE)])


def test_token_is_replaced_and_matched_regardless_of_case():
    assert Console([TOKEN]).redact(f"auth Bearer {TOKEN}") == "auth Bearer <redacted>"
    assert Console([TOKEN]).redact(TOKEN.lower()) == "<redacted>"


def test_full_base_url_is_replaced():
    assert live_console().redact(f"GET {BASE}/v1/x") == "GET <redacted>/v1/x"


def test_host_and_host_port_are_replaced():
    assert live_console().redact(f"reach {HOST}:8443 and {HOST}") == "reach <redacted> and <redacted>"


def test_longer_overlapping_secret_is_replaced_before_its_prefix():
    # If the host were replaced first, the output would be "<redacted>:8443".
    assert Console([HOST, f"{HOST}:8443"]).redact(f"{HOST}:8443") == "<redacted>"


def test_fifteen_digit_run_becomes_id_and_fourteen_digits_stay():
    assert live_console().redact(f"user {LONG_ID}") == "user <id>"
    assert live_console().redact(f"user {SHORT_ID}") == f"user {SHORT_ID}"


def test_error_text_is_redacted_by_error():
    exc = RuntimeError(f"auth {TOKEN} at {HOST}:8443 user {LONG_ID}")
    assert live_console().error(exc) == "RuntimeError: auth <redacted> at <redacted> user <id>"


def test_matching_ignores_case_for_hosts():
    assert live_console().redact("HALLPASS.Invalid:8443 and Hallpass.INVALID") == "<redacted> and <redacted>"


def test_no_secrets_still_masks_long_ids():
    assert Console([]).redact(f"x {LONG_ID}") == "x <id>"


def test_malformed_instance_url_keeps_its_raw_form_as_a_secret():
    # httpx refuses this port, so the URL has no parsed host; its raw text is still hidden.
    raw = "https://hallpass.invalid:notaport"
    assert instance_secrets(raw) == [raw]
    assert Console(instance_secrets(raw)).redact(f"at {raw}") == "at <redacted>"


def _refuse_client(*_args, **_kwargs):
    raise httpx.InvalidURL(f"cannot build a client for {BASE} with {TOKEN}")


def _set_authentik_env(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "AUTHENTIK_URL": BASE,
        "AUTHENTIK_TOKEN": TOKEN,
        "AUTHENTIK_INVITE_FLOW": "hallpass-enroll",
        "AUTHENTIK_TOKEN_IDENTIFIER": "hallpass-api",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_unexpected_error_in_authentik_check_is_redacted_and_exits_1(monkeypatch, capsys):
    _set_authentik_env(monkeypatch)
    monkeypatch.setattr(authentik_check, "make_client", _refuse_client)
    assert authentik_check.main() == 1
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert HOST not in out
    assert out.strip() == "InvalidURL: cannot build a client for <redacted> with <redacted>"


def test_unexpected_error_in_sessions_check_is_redacted_and_exits_1(monkeypatch, capsys):
    _set_authentik_env(monkeypatch)
    monkeypatch.setattr(authentik_sessions_check, "make_client", _refuse_client)
    assert authentik_sessions_check.main() == 1
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert HOST not in out
    assert out.strip() == "InvalidURL: cannot build a client for <redacted> with <redacted>"


def test_missing_token_is_a_config_error_exit_2(monkeypatch, capsys):
    _set_authentik_env(monkeypatch)
    monkeypatch.delenv("AUTHENTIK_TOKEN")
    assert authentik_sessions_check.main() == 2
    assert capsys.readouterr().out.strip() == "config error: AUTHENTIK_TOKEN is not set"
