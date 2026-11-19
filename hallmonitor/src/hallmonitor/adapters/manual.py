# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The manual adapter: a service HallMonitor cannot act on. Every subject reports `unsupported`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from hallmonitor.adapters.base import Capabilities, Observed
from hallmonitor.contract import Subject


class ManualAdapter:
    capabilities = Capabilities(suspend=False, restore=False, read_state=False, removes_connected=False)

    async def observe(self, subjects: Sequence[Subject]) -> Mapping[str, Observed]:
        return {subject.ref: Observed("unsupported") for subject in subjects}

    async def apply(self, subject: Subject, desired: str) -> Observed:
        return Observed("unsupported")

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
        return None


def create(ctx: object) -> ManualAdapter:
    return ManualAdapter()
