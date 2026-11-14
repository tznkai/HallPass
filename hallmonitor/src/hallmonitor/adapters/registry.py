# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static adapter-name-to-plugin mapping. Adding an adapter is one module plus one line here.

Mirrors fluxer-activity-panel's sources/registry.py. A plugin returns None when its service is not configured.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hallmonitor.adapters import fluxer, manual
from hallmonitor.adapters.base import Adapter
from hallmonitor.adapters.context import AdapterContext


@dataclass(frozen=True)
class AdapterPlugin:
    create: Callable[[AdapterContext], Adapter | None]  # None means the adapter is not configured


ADAPTERS: dict[str, AdapterPlugin] = {
    "manual": AdapterPlugin(manual.create),
    "fluxer": AdapterPlugin(fluxer.create),
}


def build_adapter(name: str, ctx: AdapterContext) -> Adapter | None:
    plugin = ADAPTERS.get(name)
    if plugin is None:
        return None
    return plugin.create(ctx)
