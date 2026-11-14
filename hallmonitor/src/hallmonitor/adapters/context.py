# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an adapter plugin receives: its service slug, its settings, the config, the database, the shared HTTP client and
the clock. A separate module so an adapter can import it without importing the registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

import httpx

from hallmonitor.config import Config
from hallmonitor.db import Database


@dataclass(frozen=True)
class AdapterContext:
    slug: str
    settings: Mapping[str, str]
    config: Config
    db: Database
    http: httpx.AsyncClient
    clock: Callable[[], datetime]
