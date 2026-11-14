# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The adapter protocol: what an adapter can observe, apply and count."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from hallmonitor.contract import Subject

ObservedState = Literal["allowed", "suspended", "unsupported", "banned_external", "error"]


@dataclass(frozen=True)
class Capabilities:
    suspend: bool
    restore: bool
    read_state: bool
    removes_connected: bool


@dataclass(frozen=True)
class Observed:
    state: ObservedState
    detail: str | None = None


class AdapterError(Exception):
    """The service could not be asked. `reason` is a short token such as `unreachable`, `http` or `auth`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Adapter(Protocol):
    capabilities: Capabilities

    async def observe(self, subjects: Sequence[Subject]) -> Mapping[str, Observed]:
        """Observed state keyed by each subject's ref. A subject with no entry is reported as `error`."""
        ...

    async def apply(self, subject: Subject, desired: str) -> Observed: ...

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None: ...
