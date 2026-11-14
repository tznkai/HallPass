# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static adapter-name-to-plugin mapping. Adding an adapter is one module plus one line here.

Mirrors fluxer-activity-panel's sources/registry.py. A plugin returns None when its service is not configured.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

import httpx

from hallmonitor.adapters import manual
from hallmonitor.adapters.base import Adapter
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


@dataclass(frozen=True)
class AdapterPlugin:
    create: Callable[[AdapterContext], Adapter | None]  # None means the adapter is not configured


ADAPTERS: dict[str, AdapterPlugin] = {
    "manual": AdapterPlugin(manual.create),
}


def build_adapter(name: str, ctx: AdapterContext) -> Adapter | None:
    plugin = ADAPTERS.get(name)
    if plugin is None:
        return None
    return plugin.create(ctx)
