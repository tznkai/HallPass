# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The conflict engine: decides whether a parent's action reverses another parent's, and whether the reversal locks
the child down. Pure: it reads the history it is given and touches no storage, so the caller builds that history."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

REVERSAL_MODES = ("default", "monitored")


@dataclass(frozen=True)
class Entry:
    at: datetime
    actor_id: int
    action: Literal["place", "lift"]
    services: frozenset[int]
    reversal: bool


@dataclass(frozen=True)
class Proposed:
    actor_id: int
    action: Literal["place", "lift"]
    services: frozenset[int]


@dataclass(frozen=True)
class Verdict:
    reversal: bool
    lockdown: bool


def evaluate(mode: str, history: Sequence[Entry], proposed: Proposed, now: datetime, window: timedelta) -> Verdict:
    if mode not in REVERSAL_MODES:
        return Verdict(False, False)
    previous = next(
        (e for e in sorted(history, key=lambda e: e.at, reverse=True) if e.services & proposed.services),
        None,
    )
    reversal = (
        previous is not None
        and previous.actor_id != proposed.actor_id
        and previous.action != proposed.action
    )
    if not reversal:
        return Verdict(False, False)
    recent = any(e.reversal and now - window < e.at <= now for e in history)
    return Verdict(True, recent)
