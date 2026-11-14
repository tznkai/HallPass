# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The entry point: exit 2 for a bad environment, 3 for a refused database."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import hallmonitor.__main__ as entry
from tests.fake_hallpass import TOKEN


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """setup_logging replaces the root handlers and captures warnings; put both back after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(False)


def test_a_missing_variable_exits_2_and_names_it(monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(os, "environ", {})
    assert entry.main() == 2
    assert "HALLPASS_MONITOR_URL is required" in capsys.readouterr().err


def test_a_refused_database_exits_3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                    capsys: pytest.CaptureFixture[str], restore_logging: None) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "hallmonitor.db").write_bytes(b"not a database" * 100)   # quarantined on open: refused
    monkeypatch.setattr(os, "environ", {
        "HALLPASS_MONITOR_URL": "https://hallpass.invalid",
        "HALLPASS_MONITOR_TOKEN": TOKEN,
        "HALLMONITOR_DATA_DIR": str(data),
    })
    assert entry.main() == 3
    assert "database refused: quarantined" in capsys.readouterr().err
