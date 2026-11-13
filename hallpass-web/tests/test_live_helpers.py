# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the live-check redactor and the live scripts' error exits (tools/live). No network."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
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


# Session check: per-user comparison, and no user pk in any printed line. The pks below are short and distinctive
# so that the output check cannot pass by accident; they are not 15+ digit runs.
TEST_PK = 424242
OTHER_PK = 5550123
NEW_LOGIN_PK = 6660234
compare_others = authentik_sessions_check.compare_others


def test_compare_others_compares_each_user_not_the_total():
    # User 1 lost a session and user 2 gained one: the total is unchanged, but the per-user check sees both.
    assert compare_others({1: 2, 2: 1}, {1: 1, 2: 2}, exclude_pk=9) == (1, 1)


def test_compare_others_ignores_the_test_user():
    assert compare_others({9: 1, 2: 1}, {2: 1}, exclude_pk=9) == (0, 0)


def test_compare_others_counts_a_user_who_lost_all_sessions_as_dropped():
    assert compare_others({2: 1}, {}, exclude_pk=9) == (1, 0)


def test_compare_others_counts_a_new_login_as_rose_only():
    assert compare_others({}, {2: 1}, exclude_pk=9) == (0, 1)


def test_compare_others_unchanged_users_count_nothing():
    assert compare_others({2: 3}, {2: 3}, exclude_pk=9) == (0, 0)


@dataclass
class SessionServerState:
    """The in-process Authentik session API: a list of session rows, one per session, with a user pk each."""

    sessions: list[dict] = field(default_factory=list)
    drop_another_user_on_delete: bool = False
    login_during_delete_pk: int | None = NEW_LOGIN_PK


class FakeSessionServer:
    def __init__(self) -> None:
        self.state = SessionServerState(sessions=[{"user": {"pk": OTHER_PK}}, {"user": {"pk": OTHER_PK}}])

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == authentik_sessions_check.SESSIONS:
            return httpx.Response(200, json={"results": self.state.sessions, "pagination": {"next": 0}})
        if request.method == "POST" and path == authentik_sessions_check.USERS:
            return httpx.Response(201, json={"pk": TEST_PK, "uuid": "fake-uuid"})
        if request.method == "POST" and path == f"{authentik_sessions_check.USERS}{TEST_PK}/set_password/":
            return httpx.Response(204)
        if request.method == "DELETE" and path == authentik_sessions_check.BULK_DELETE:
            pk = int(request.url.params["user_pks"])
            before = len(self.state.sessions)
            self.state.sessions = [s for s in self.state.sessions if s["user"]["pk"] != pk]
            deleted = before - len(self.state.sessions)
            if self.state.drop_another_user_on_delete:
                # The wrong-user delete the check exists to catch: one of another user's sessions goes too.
                self.state.sessions = self.state.sessions[1:]
            if self.state.login_during_delete_pk is not None:
                self.state.sessions.append({"user": {"pk": self.state.login_during_delete_pk}})
            return httpx.Response(200, json={"deleted": deleted})
        if request.method == "DELETE" and path == f"{authentik_sessions_check.USERS}{TEST_PK}/":
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "not found"})


def _run_session_check(monkeypatch, capsys, fake: FakeSessionServer) -> tuple[int, str]:
    def sign_in(self, question):  # the human signs the test user in; the fake records that session
        fake.state.sessions.append({"user": {"pk": TEST_PK}})
        return ""

    monkeypatch.setattr(Console, "ask", sign_in)
    console = Console([TOKEN])
    client = authentik_sessions_check.make_client(
        "https://hallpass.invalid:8443", TOKEN, transport=httpx.MockTransport(fake.handle)
    )
    check = authentik_sessions_check.SessionCheck(client, console)
    try:
        failures = check.run_steps()
    finally:
        check.cleanup_all()
        client.close()
    return failures, capsys.readouterr().out


def test_session_check_passes_when_others_only_log_in(monkeypatch, capsys):
    failures, out = _run_session_check(monkeypatch, capsys, FakeSessionServer())
    assert failures == 0
    assert "others_dropped=0" in out
    assert "others_rose=1" in out
    assert "others_survived=True" in out
    assert "test_user_remaining=0" in out
    assert "deleted_matches=True" in out


def test_session_check_fails_when_another_users_session_is_deleted_and_a_login_offsets_it(monkeypatch, capsys):
    fake = FakeSessionServer()
    fake.state.drop_another_user_on_delete = True
    failures, out = _run_session_check(monkeypatch, capsys, fake)
    # The total is unchanged (one drop, one login), so only the per-user comparison can catch this.
    assert failures == 1
    assert "others_dropped=1" in out
    assert "others_rose=1" in out
    assert "others_survived=False" in out


def test_session_check_prints_no_user_pk(monkeypatch, capsys):
    fake = FakeSessionServer()
    fake.state.drop_another_user_on_delete = True
    _, out = _run_session_check(monkeypatch, capsys, fake)
    assert "per_user_pk" not in out
    for pk in (TEST_PK, OTHER_PK, NEW_LOGIN_PK):
        assert str(pk) not in out
    assert "total=" in out
    assert "users_with_sessions=" in out
