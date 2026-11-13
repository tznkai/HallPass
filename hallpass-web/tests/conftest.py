# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures for the web tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.helpers import Env, make_env


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    environment = await make_env(tmp_path)
    try:
        yield environment
    finally:
        await environment.close()
