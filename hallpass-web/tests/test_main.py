# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The entry point: a bad environment exits 2, a refused database exits 3, and nothing else runs first."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from hallpass.__main__ import main
from tests.helpers import ENV


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
