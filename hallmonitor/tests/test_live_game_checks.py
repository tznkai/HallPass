# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure helpers and fake-transport runs of the game-server live checks. No network: every request goes to a MockTransport."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "live"))
import palworld_check  # noqa: E402
import ptero_files_check  # noqa: E402
import ship_check  # noqa: E402
from _live import Console  # noqa: E402
from hallmonitor.adapters.steam import STEAM64_BASE, steam2  # noqa: E402

HOST = "fake.test"
PTERO_BASE = "https://fake.test"
PAL_BASE = "https://fake.test:8212"
KEY = "SENTINEL-KEY-abc"
PAL_PASSWORD = "SENTINEL-PAL-pass"
SERVER = "srv1abcd"
PATH = "banlist.txt"
STEAM = str(STEAM64_BASE + 2 * 1234 + 1)   # a SteamID64 built at runtime, never written out
SID2 = steam2(STEAM, "0")                  # STEAM_0:1:1234


def _ptero_client(handler) -> httpx.Client:
    return ptero_files_check.make_client(PTERO_BASE, KEY, transport=httpx.MockTransport(handler))


# ptero_files_check ----------------------------------------------------------------------------------------

def test_shape_masks_digits_and_letters_and_keeps_punctuation():
    assert ptero_files_check.shape("steam_1001 Alice!") == "aaaaa_9999 aaaaa!"


def test_shape_truncates_a_long_line():
    assert ptero_files_check.shape("a" * 100) == "a" * 80 + "..."


def test_read_prints_line_count_and_masked_first_line_and_never_the_content(capsys):
    body = f"steam_{STEAM}\nsecond line\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/client/servers/{SERVER}/files/contents"
        assert request.url.params["file"] == PATH
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, text=body)

    console = Console([KEY, SERVER, HOST])
    text = ptero_files_check.read_file(_ptero_client(handler), SERVER, PATH, console)

    assert text == body
    out = capsys.readouterr().out
    # The shape of the id is a run of nines, which the console's long-run redaction then shows as <id>.
    assert out.strip() == "STEP read: status=200 lines=2 first_line=aaaaa_<id>"
    assert STEAM not in out and "second line" not in out and KEY not in out


def test_read_of_a_missing_file_prints_the_status_and_returns_none(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    console = Console([KEY, SERVER, HOST])
    assert ptero_files_check.read_file(_ptero_client(handler), SERVER, PATH, console) is None
    assert capsys.readouterr().out.strip() == "STEP read: status=404"


def test_an_unreachable_server_prints_the_error_redacted(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {PTERO_BASE}/api/client/servers/{SERVER}")

    console = Console([KEY, SERVER, HOST])
    assert ptero_files_check.read_file(_ptero_client(handler), SERVER, PATH, console) is None
    out = capsys.readouterr().out
    assert "ConnectError" in out
    assert HOST not in out and SERVER not in out


def test_write_sends_the_same_text_as_raw_body_and_prints_the_status(capsys):
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["method"] = request.method
        sent["path"] = request.url.path
        sent["file"] = request.url.params["file"]
        sent["content_type"] = request.headers["content-type"]
        sent["body"] = request.content.decode("utf-8")
        return httpx.Response(204)

    console = Console([KEY, SERVER, HOST])
    assert ptero_files_check.write_file(_ptero_client(handler), SERVER, PATH, "x\ny\n", console) is True
    assert sent == {
        "method": "POST", "path": f"/api/client/servers/{SERVER}/files/write", "file": PATH,
        "content_type": "text/plain", "body": "x\ny\n",
    }
    assert capsys.readouterr().out.strip() == "STEP write: status=204"


def test_resources_prints_the_state_and_whether_uptime_is_present(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/client/servers/{SERVER}/resources"
        return httpx.Response(200, json={"attributes": {"current_state": "running",
                                                         "resources": {"uptime": 3600000}}})

    ptero_files_check.check_resources(_ptero_client(handler), SERVER, Console([KEY, SERVER, HOST]))
    assert capsys.readouterr().out.strip() == "STEP resources: current_state=running uptime_present=True"


def test_a_missing_config_variable_is_a_config_error_naming_the_variable(monkeypatch, capsys):
    monkeypatch.setenv("PTERO_URL", PTERO_BASE)
    monkeypatch.setenv("PTERO_SERVER", SERVER)
    monkeypatch.delenv("PTERO_KEY", raising=False)
    assert ptero_files_check.main(["--path", PATH]) == 2
    assert capsys.readouterr().out.strip() == "config error: PTERO_KEY is not set"


def test_main_writes_back_unchanged_content_and_exits_0(monkeypatch, capsys):
    monkeypatch.setenv("PTERO_URL", PTERO_BASE)
    monkeypatch.setenv("PTERO_KEY", KEY)
    monkeypatch.setenv("PTERO_SERVER", SERVER)
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files/contents"):
            return httpx.Response(200, text="one\ntwo\n")
        if request.url.path.endswith("/files/write"):
            writes.append(request.content.decode("utf-8"))
            return httpx.Response(204)
        return httpx.Response(200, json={"attributes": {"current_state": "offline", "resources": {}}})

    assert ptero_files_check.main(["--path", PATH], transport=httpx.MockTransport(handler)) == 0
    assert writes == ["one\ntwo\n"]
    out = capsys.readouterr().out
    assert "STEP write: status=204" in out
    assert "current_state=offline uptime_present=False" in out
    assert KEY not in out and SERVER not in out and HOST not in out


# palworld_check ------------------------------------------------------------------------------------------

def test_key_paths_lists_key_names_without_any_value():
    body = {"players": [{"name": "Alice", "userId": "steam_" + STEAM}, {"name": "Bob", "level": 3}], "count": 2}
    assert palworld_check.key_paths(body) == ["count", "players", "players[].level", "players[].name",
                                              "players[].userId"]


def test_digits_arg_accepts_only_digits():
    assert palworld_check.digits_arg(STEAM) == STEAM
    for bad in ("", "12a", "-5", "1 2"):
        with pytest.raises(argparse.ArgumentTypeError):
            palworld_check.digits_arg(bad)


def _palworld_handler(log: list[tuple[str, str, object]]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        log.append((request.method, request.url.path, body))
        assert request.headers["authorization"].startswith("Basic ")
        if request.url.path == "/v1/api/players":
            return httpx.Response(200, json={"players": [{"name": "Alice", "userId": "steam_" + STEAM}]})
        return httpx.Response(200, json={})
    return handler


def test_ban_then_unban_with_the_steam_userid_and_no_value_printed(capsys):
    log: list[tuple[str, str, object]] = []
    client = httpx.Client(base_url=PAL_BASE, auth=httpx.BasicAuth("admin", PAL_PASSWORD),
                          transport=httpx.MockTransport(_palworld_handler(log)))
    console = Console([PAL_PASSWORD, HOST, STEAM, "steam_" + STEAM])
    palworld_check.run_checks(console, client, STEAM, pause=lambda: None)

    userid = "steam_" + STEAM
    assert log == [
        ("GET", "/v1/api/players", None),
        ("POST", "/v1/api/ban", {"userid": userid, "message": "hallmonitor test"}),
        ("POST", "/v1/api/unban", {"userid": userid}),
    ]
    out = capsys.readouterr().out
    assert "players[].name" in out
    assert "Alice" not in out and STEAM not in out and PAL_PASSWORD not in out


def test_the_ban_is_lifted_even_when_the_wait_is_interrupted(capsys):
    log: list[tuple[str, str, object]] = []
    client = httpx.Client(base_url=PAL_BASE, auth=httpx.BasicAuth("admin", PAL_PASSWORD),
                          transport=httpx.MockTransport(_palworld_handler(log)))

    def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        palworld_check.run_checks(Console([PAL_PASSWORD, STEAM]), client, STEAM, pause=interrupted)
    assert log[-1] == ("POST", "/v1/api/unban", {"userid": "steam_" + STEAM})


def test_palworld_main_rejects_a_steam_id_with_letters_before_any_request(monkeypatch, capsys):
    monkeypatch.setenv("PAL_REST_URL", PAL_BASE)
    monkeypatch.setenv("PAL_ADMIN_PASSWORD", PAL_PASSWORD)
    with pytest.raises(SystemExit) as info:
        palworld_check.main(["--steam", "12a"])
    assert info.value.code == 2
    assert "--steam" in capsys.readouterr().err


def test_palworld_main_without_the_password_is_a_config_error(monkeypatch, capsys):
    monkeypatch.setenv("PAL_REST_URL", PAL_BASE)
    monkeypatch.delenv("PAL_ADMIN_PASSWORD", raising=False)
    assert palworld_check.main(["--steam", STEAM]) == 2
    assert capsys.readouterr().out.strip() == "config error: PAL_ADMIN_PASSWORD is not set"


# ship_check ----------------------------------------------------------------------------------------------

def test_mask_ids_keeps_the_universe_and_parity_digits_and_hides_the_account_number():
    assert ship_check.mask_ids("1 STEAM_0:1:123 : permanent") == "1 STEAM_0:1:<n> : permanent"
    assert ship_check.mask_ids("ID filter list: 2 entries") == "ID filter list: 2 entries"


class FakeRcon:
    """Records each command; raises on the commands named in `fail_on`."""

    def __init__(self, fail_on: frozenset[str] = frozenset()) -> None:
        self.commands: list[str] = []
        self._fail_on = fail_on

    async def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        if cmd.split(" ")[0] in self._fail_on:
            raise ship_check.RconError("timeout")
        return f"ID filter list: 1 entries\n1 {SID2} : permanent\n" if cmd == "listid" else "ok"


async def test_the_source_sequence_runs_in_order_and_prints_masked_output(capsys):
    client = FakeRcon()
    console = Console([STEAM, SID2, "fake-rcon-pass", HOST])
    ok = await ship_check.run_checks(console, client, STEAM, "0")

    assert ok is True
    assert client.commands == [
        "listid", f"banid 0 {SID2}", "listid", "writeid", f"removeid {SID2}", "writeid", "listid",
    ]
    out = capsys.readouterr().out
    assert "STEP listid (before):" in out
    assert SID2 not in out and STEAM not in out and "STEAM_0:1:<n>" in out


async def test_a_failed_ban_still_removes_the_id(capsys):
    client = FakeRcon(fail_on=frozenset({"banid"}))
    with pytest.raises(ship_check.RconError):
        await ship_check.run_checks(Console([STEAM]), client, STEAM, "0")
    assert f"removeid {SID2}" in client.commands
    assert client.commands[-1] == "writeid"


def test_ship_main_rejects_a_steam_id_that_is_not_digits(monkeypatch, capsys):
    monkeypatch.setenv("SHIP_RCON_HOST", HOST)
    monkeypatch.setenv("SHIP_RCON_PASSWORD", "fake-rcon-pass")
    with pytest.raises(SystemExit) as info:
        ship_check.main(["--steam", "7656x"])
    assert info.value.code == 2
    assert "--steam" in capsys.readouterr().err


def test_ship_main_without_the_host_is_a_config_error(monkeypatch, capsys):
    monkeypatch.delenv("SHIP_RCON_HOST", raising=False)
    monkeypatch.setenv("SHIP_RCON_PASSWORD", "fake-rcon-pass")
    assert ship_check.main(["--steam", STEAM]) == 2
    assert capsys.readouterr().out.strip() == "config error: SHIP_RCON_HOST is not set"
