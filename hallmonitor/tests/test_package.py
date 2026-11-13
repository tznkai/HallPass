# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The package exposes its version and matches the version in pyproject.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

import hallmonitor

PROJECT = Path(__file__).resolve().parents[1]


def test_version_is_exposed():
    assert hallmonitor.__version__ == "0.1.0"


def test_version_matches_pyproject():
    data = tomllib.loads((PROJECT / "pyproject.toml").read_text("utf-8"))
    assert hallmonitor.__version__ == data["project"]["version"]
