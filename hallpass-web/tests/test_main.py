# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The entry point: a bad environment exits 2, a refused database exits 3, and nothing else runs first."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from hallpass.__main__ import MonitorServer, build_servers, main, serve_both
from tests.helpers import ENV, make_env


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """main() installs the JSON handler and captures warnings; put the root logger back as it was."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
        logging.captureWarnings(False)


def test_a_missing_setting_exits_2_and_names_the_variable(monkeypatch, capsys):
    monkeypatch.delenv("HALLPASS_PUBLIC_URL", raising=False)
    assert main() == 2
    assert "HALLPASS_PUBLIC_URL" in capsys.readouterr().err


def test_a_quarantined_database_exits_3(monkeypatch, tmp_path: Path, capsys, restored_logging):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HALLPASS_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "hallpass.db").write_bytes(b"this is not a sqlite database" * 64)

    assert main() == 3
    assert "database refused" in capsys.readouterr().err
    assert list((tmp_path / "data").glob("hallpass.db.corrupt*"))


class FakeServer:
    """Stands in for a uvicorn server: it runs until asked to exit, or ends at once, or fails with the given error."""

    def __init__(self, *, finish: bool = False, fail: Exception | None = None) -> None:
        self.should_exit = False
        self._finish = finish
        self._fail = fail

    async def serve(self) -> None:
        if self._fail is not None:
            raise self._fail
        while not self._finish and not self.should_exit:
            await asyncio.sleep(0.01)


async def test_when_one_listener_ends_the_other_is_asked_to_stop() -> None:
    public, monitor = FakeServer(finish=True), FakeServer()

    await asyncio.wait_for(serve_both(public, monitor), timeout=5)

    assert monitor.should_exit is True


async def test_when_the_monitor_listener_fails_the_public_one_is_asked_to_stop() -> None:
    public, monitor = FakeServer(), FakeServer(fail=OSError("bind failed"))

    with pytest.raises(OSError):
        await asyncio.wait_for(serve_both(public, monitor), timeout=5)

    assert public.should_exit is True


def test_the_monitor_listener_installs_no_signal_handlers(monkeypatch) -> None:
    async def app(scope, receive, send) -> None:
        return None

    def refuse(*args):
        raise AssertionError("a signal handler was installed")

    monkeypatch.setattr(signal, "signal", refuse)
    server = MonitorServer(uvicorn.Config(app, port=8081, log_config=None))
    with server.capture_signals():
        pass


async def test_the_listeners_bind_their_own_ports_and_only_the_public_one_trusts_the_proxy(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    try:
        public, monitor = build_servers(env.config, env.application.state.deps)

        assert (public.config.port, public.config.proxy_headers) == (env.config.bind_port, True)
        assert (monitor.config.port, monitor.config.proxy_headers) == (env.config.monitor_port, False)
        assert isinstance(monitor, MonitorServer)
    finally:
        await env.close()
