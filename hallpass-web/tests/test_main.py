# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The entry point: the exit codes, the two listeners' lifecycle and the cleanup on a stop signal.

The subprocess tests run `python -m hallpass` with the fake settings from tests/helpers.py, on 127.0.0.1 ports chosen
for the test. They never read a .env file. The database and logs live in the test's temporary directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from hallpass.__main__ import EXIT_LISTENER_FAILED, EXIT_OK, MonitorServer, PublicServer, build_servers, main, serve_both
from tests.helpers import ENV, make_env

HOST = "127.0.0.1"
STARTUP_TIMEOUT = 30
SHUTDOWN_TIMEOUT = 30


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

    def __init__(self, *, finish: bool = False, fail: BaseException | None = None) -> None:
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

    code = await asyncio.wait_for(serve_both(public, monitor), timeout=5)

    assert monitor.should_exit is True
    assert code == EXIT_OK


async def test_when_the_monitor_listener_fails_the_public_one_is_asked_to_stop() -> None:
    public, monitor = FakeServer(), FakeServer(fail=OSError("bind failed"))

    with pytest.raises(OSError):
        await asyncio.wait_for(serve_both(public, monitor), timeout=5)

    assert public.should_exit is True


@pytest.mark.parametrize("failing", ["public", "monitor"])
async def test_a_listener_that_cannot_start_exits_4_and_stops_the_other(failing: str) -> None:
    """uvicorn reports a bind failure with SystemExit(3), not OSError. Either listener failing gives exit code 4."""
    failed = FakeServer(fail=SystemExit(3))
    running = FakeServer()
    public, monitor = (failed, running) if failing == "public" else (running, failed)

    code = await asyncio.wait_for(serve_both(public, monitor), timeout=5)

    assert code == EXIT_LISTENER_FAILED
    assert running.should_exit is True


def test_the_listeners_install_no_signal_handlers_of_their_own(monkeypatch) -> None:
    async def app(scope, receive, send) -> None:
        return None

    def refuse(*args):
        raise AssertionError("a signal handler was installed")

    monkeypatch.setattr(signal, "signal", refuse)
    for server in (MonitorServer(uvicorn.Config(app, port=8081, log_config=None)),
                   PublicServer(uvicorn.Config(app, port=8080, log_config=None))):
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


# Subprocess lifecycle: real listeners, real signals ----------------------------------------------------

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def is_listening(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_until_listening(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not is_listening(port):
        if process.poll() is not None:
            raise AssertionError(f"hallpass exited with {process.returncode} before listening")
        if time.monotonic() > deadline:
            raise AssertionError("hallpass did not start listening")
        time.sleep(0.1)


def launch(tmp_path: Path, public_port: int, monitor_port: int) -> tuple[subprocess.Popen, Path]:
    """Starts `python -m hallpass` in a clean environment with the fake settings. Output goes to a file in tmp_path."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        **ENV,
        "HALLPASS_DATA_DIR": str(data),
        "HALLPASS_BIND": f"{HOST}:{public_port}",
        "HALLPASS_MONITOR_BIND": f"{HOST}:{monitor_port}",
    }
    output = tmp_path / "output.log"
    with output.open("w") as handle:
        process = subprocess.Popen([sys.executable, "-m", "hallpass"], env=environment, stdout=handle,
                                   stderr=subprocess.STDOUT)
    return process, output


def finish(process: subprocess.Popen) -> int:
    """Waits for the process to exit, killing it if it does not, and returns its exit code."""
    try:
        return process.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise AssertionError("hallpass did not exit after the stop request") from None


def json_events(output: Path) -> list[dict]:
    events = []
    for line in output.read_text("utf-8").splitlines():
        if line.startswith("{"):
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(line))
    return events


def test_sigterm_stops_both_listeners_and_runs_the_cleanup(tmp_path: Path) -> None:
    public_port, monitor_port = free_port(), free_port()
    process, output = launch(tmp_path, public_port, monitor_port)
    try:
        wait_until_listening(public_port, process)
        wait_until_listening(monitor_port, process)

        process.send_signal(signal.SIGTERM)
        code = finish(process)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert code == EXIT_OK
    assert not is_listening(public_port)
    assert not is_listening(monitor_port)
    shutdown = [event for event in json_events(output) if event.get("event") == "shutdown_complete"]
    assert shutdown and shutdown[-1]["code"] == EXIT_OK


@pytest.mark.parametrize("occupied", ["monitor", "public"])
def test_a_port_already_in_use_exits_4_and_stops_the_other_listener(tmp_path: Path, occupied: str) -> None:
    """A real port conflict: a socket already listens on the port, so uvicorn cannot bind it."""
    taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        taken.bind((HOST, 0))
        taken.listen(1)
        taken_port = taken.getsockname()[1]
        other_port = free_port()
        public_port, monitor_port = (taken_port, other_port) if occupied == "public" else (other_port, taken_port)
        process, output = launch(tmp_path, public_port, monitor_port)
        try:
            code = finish(process)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    finally:
        taken.close()

    assert code == EXIT_LISTENER_FAILED
    assert not is_listening(other_port)
    shutdown = [event for event in json_events(output) if event.get("event") == "shutdown_complete"]
    assert shutdown and shutdown[-1]["code"] == EXIT_LISTENER_FAILED
