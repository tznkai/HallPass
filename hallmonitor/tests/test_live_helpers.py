# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the live-check redactor, the Fluxer checks' ban gate and the live scripts' error exits. No network."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "live"))
import fluxer_check  # noqa: E402
from _live import Console, instance_secrets  # noqa: E402

TOKEN = "SENTINEL-TOKEN-xyz"
HOST = "fluxer.invalid"
BASE = "https://fluxer.invalid:8443/api"
ADMIN_BASE = "https://fluxer.invalid"
USER_ID = "1001"
DECISION_ID = "5001"
LONG_ID = "1" * 15  # built at runtime: no 15-digit run is written in this file
SHORT_ID = "1" * 14


def live_console() -> Console:
    return Console([TOKEN, *instance_secrets(BASE)])


def test_token_is_replaced_and_matched_regardless_of_case():
    assert Console([TOKEN]).redact(f"Authorization: Admin {TOKEN}") == "Authorization: Admin <redacted>"
    assert Console([TOKEN]).redact(TOKEN.upper()) == "<redacted>"


def test_full_base_url_is_replaced():
    assert live_console().redact(f"GET {BASE}/v1/admin/users") == "GET <redacted>/v1/admin/users"


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
    assert live_console().redact("FLUXER.Invalid:8443 and Fluxer.INVALID") == "<redacted> and <redacted>"


def test_no_secrets_still_masks_long_ids():
    assert Console([]).redact(f"x {LONG_ID}") == "x <id>"


def test_malformed_instance_url_keeps_its_raw_form_as_a_secret():
    # httpx refuses this port, so the URL has no parsed host; its raw text is still hidden.
    raw = "https://fluxer.invalid:notaport"
    assert instance_secrets(raw) == [raw]
    assert Console(instance_secrets(raw)).redact(f"at {raw}") == "at <redacted>"


def _refuse_client(*_args, **_kwargs):
    raise httpx.InvalidURL(f"cannot build a client for {ADMIN_BASE} with {TOKEN}")


def test_unexpected_error_in_main_is_redacted_and_exits_1(monkeypatch, capsys):
    monkeypatch.setenv("FLUXER_ADMIN_URL", ADMIN_BASE)
    monkeypatch.setenv("FLUXER_ADMIN_KEY", TOKEN)
    monkeypatch.setattr(sys, "argv", ["fluxer_check.py", "--user", USER_ID])
    monkeypatch.setattr(fluxer_check, "make_client", _refuse_client)
    assert fluxer_check.main() == 1
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert HOST not in out
    assert out.strip() == "InvalidURL: cannot build a client for <redacted> with <redacted>"


def test_missing_key_is_a_config_error_exit_2(monkeypatch, capsys):
    monkeypatch.setenv("FLUXER_ADMIN_URL", ADMIN_BASE)
    monkeypatch.delenv("FLUXER_ADMIN_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["fluxer_check.py", "--user", USER_ID])
    assert fluxer_check.main() == 2
    assert capsys.readouterr().out.strip() == "config error: FLUXER_ADMIN_KEY is not set"


def _user_body() -> dict:
    return {"users": [{"id": USER_ID, "flags": "0", "temp_banned_until": None,
                       "enforcement_decision_id": DECISION_ID}]}


def _checks(handler, *, try_ban: bool) -> fluxer_check.FluxerChecks:
    client = httpx.Client(base_url=ADMIN_BASE, transport=httpx.MockTransport(handler))
    return fluxer_check.FluxerChecks(client, live_console(), USER_ID, None, None, None, try_ban=try_ban)


def test_without_try_ban_no_ban_or_decision_request_is_sent(capsys):
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/api-keys"):
            return httpx.Response(403, json={})
        if request.url.path.endswith("/sessions") and request.method == "GET":
            return httpx.Response(200, json={"sessions": []})
        return httpx.Response(200, json=_user_body())

    failures = _checks(handler, try_ban=False).run_steps()

    assert failures == 0
    assert not [path for _, path in seen if "/ban" in path or "enforcement" in path]
    out = capsys.readouterr().out
    for name in ("suspend", "decision_owner", "lift"):
        assert f"STEP {name}: skipped: holds no longer ban (see docs/service-notes.md)" in out.splitlines()


def test_a_ban_left_active_by_a_failed_lift_is_lifted_in_the_finally(capsys):
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "PUT" and path.endswith("/ban"):
            return httpx.Response(200, json={})
        if path.endswith("/end"):
            return httpx.Response(500, json={})  # the lift through the decision fails, every time
        if path.endswith("/api-keys"):
            return httpx.Response(403, json={})
        return httpx.Response(200, json=_user_body())

    failures = _checks(handler, try_ban=True).run_steps()

    assert failures >= 1
    end_path = f"/v1/admin/enforcement-decisions/{DECISION_ID}/end"
    assert seen.count(("POST", end_path)) == 2   # the lift step, then the cleanup in the finally
    assert "STEP lift after failure: status=500" in capsys.readouterr().out.splitlines()
