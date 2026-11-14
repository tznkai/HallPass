# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure conflict engine: reversal and lockdown verdicts for each conflict mode, with no database.

Covers hallpass.AC7.1 (cooperative), AC7.2 and AC7.3 (verdict), and the rule part of AC7.4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hallpass import conflict
from hallpass.conflict import Entry, Proposed, Verdict

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=24)
PARENT_A = 1
PARENT_B = 2
ONE = frozenset({1})
TWO = frozenset({2})
BOTH = frozenset({1, 2})
SERVICE_SETS = [ONE, TWO, BOTH]
MODES = ["cooperative", "default", "monitored", "strict"]
NO_VERDICT = Verdict(reversal=False, lockdown=False)


def play(mode: str, steps: list[tuple[datetime, int, str, frozenset[int]]]) -> list[Verdict]:
    """Runs each step through evaluate, appending it to the history as the caller would: a logged action carries
    the reversal flag its verdict gave."""
    history: list[Entry] = []
    verdicts: list[Verdict] = []
    for at, actor, action, services in steps:
        verdict = conflict.evaluate(mode, history, Proposed(actor, action, services), at, WINDOW)
        verdicts.append(verdict)
        history.append(Entry(at=at, actor_id=actor, action=action, services=services, reversal=verdict.reversal))
    return verdicts


# AC7.1: cooperative and strict never reverse or lock -----------------------------------------------

@pytest.mark.parametrize("services", SERVICE_SETS)
@pytest.mark.parametrize("mode", ["cooperative", "strict"])
def test_cooperative_and_strict_never_reverse_or_lock(mode: str, services: frozenset[int]) -> None:
    steps = [
        (NOW - timedelta(hours=3), PARENT_A, "place", services),
        (NOW - timedelta(hours=2), PARENT_B, "lift", services),
        (NOW - timedelta(hours=1), PARENT_A, "place", services),
        (NOW, PARENT_B, "lift", services),
    ]
    assert play(mode, steps) == [NO_VERDICT] * 4


# AC7.2: a first cross-parent reversal is allowed with no lockdown -----------------------------------

@pytest.mark.parametrize("mode", ["default", "monitored"])
def test_first_cross_parent_reversal_is_allowed_without_lockdown(mode: str) -> None:
    steps = [
        (NOW - timedelta(hours=1), PARENT_A, "place", ONE),
        (NOW, PARENT_B, "lift", ONE),
    ]
    assert play(mode, steps)[-1] == Verdict(reversal=True, lockdown=False)


# AC7.3: a second reversal within the window locks down ----------------------------------------------

@pytest.mark.parametrize("mode", ["default", "monitored"])
def test_second_reversal_within_the_window_locks_down(mode: str) -> None:
    steps = [
        (NOW - timedelta(hours=2), PARENT_A, "place", ONE),
        (NOW - timedelta(hours=1), PARENT_B, "lift", ONE),
        (NOW, PARENT_A, "place", ONE),
    ]
    assert play(mode, steps)[-1] == Verdict(reversal=True, lockdown=True)


# AC7.4: the window edge, the own-decision rule and the overlap rule --------------------------------

@pytest.mark.parametrize("mode", ["default", "monitored"])
def test_first_reversal_outside_the_window_causes_no_lockdown(mode: str) -> None:
    steps = [
        (NOW - timedelta(hours=26), PARENT_A, "place", ONE),
        (NOW - timedelta(hours=25), PARENT_B, "lift", ONE),
        (NOW, PARENT_A, "place", ONE),
    ]
    assert play(mode, steps)[-1] == Verdict(reversal=True, lockdown=False)


@pytest.mark.parametrize("mode", ["default", "monitored"])
def test_first_reversal_exactly_at_the_window_edge_is_outside(mode: str) -> None:
    steps = [
        (NOW - timedelta(hours=25), PARENT_A, "place", ONE),
        (NOW - WINDOW, PARENT_B, "lift", ONE),
        (NOW, PARENT_A, "place", ONE),
    ]
    assert play(mode, steps)[-1] == Verdict(reversal=True, lockdown=False)


@pytest.mark.parametrize("mode", ["default", "monitored"])
def test_a_parent_reversing_their_own_decision_is_not_a_reversal(mode: str) -> None:
    steps = [
        (NOW - timedelta(hours=1), PARENT_A, "place", ONE),
        (NOW, PARENT_A, "lift", ONE),
    ]
    assert play(mode, steps)[-1] == NO_VERDICT


@pytest.mark.parametrize("mode", ["default", "monitored"])
def test_a_lift_on_a_service_the_last_action_did_not_touch_is_not_a_reversal(mode: str) -> None:
    steps = [
        (NOW - timedelta(hours=1), PARENT_A, "place", ONE),
        (NOW, PARENT_B, "lift", TWO),
    ]
    assert play(mode, steps)[-1] == NO_VERDICT


@pytest.mark.parametrize("mode", ["default", "monitored"])
def test_a_reversal_on_partly_overlapping_services_counts(mode: str) -> None:
    steps = [
        (NOW - timedelta(hours=1), PARENT_A, "place", BOTH),
        (NOW, PARENT_B, "lift", ONE),
    ]
    assert play(mode, steps)[-1] == Verdict(reversal=True, lockdown=False)


@pytest.mark.parametrize("mode", ["default", "monitored"])
def test_the_most_recent_overlapping_action_is_the_one_compared(mode: str) -> None:
    # The older lift by B is the same actor as the proposed lift, so only the newer place by A can reverse it.
    history = [
        Entry(at=NOW - timedelta(hours=3), actor_id=PARENT_B, action="lift", services=ONE, reversal=False),
        Entry(at=NOW - timedelta(hours=1), actor_id=PARENT_A, action="place", services=ONE, reversal=False),
    ]
    verdict = conflict.evaluate(mode, list(reversed(history)), Proposed(PARENT_B, "lift", ONE), NOW, WINDOW)
    assert verdict == Verdict(reversal=True, lockdown=False)


# Mode dispatch and purity ------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["cooperative", "strict"])
def test_a_cross_parent_lift_is_not_reported_as_a_reversal_outside_default_and_monitored(mode: str) -> None:
    history = [Entry(at=NOW - timedelta(hours=1), actor_id=PARENT_A, action="place", services=ONE, reversal=False)]
    assert conflict.evaluate(mode, history, Proposed(PARENT_B, "lift", ONE), NOW, WINDOW) == NO_VERDICT


def test_the_engine_is_pure_and_imports_no_storage_or_web_code() -> None:
    source = Path(conflict.__file__).read_text("utf-8")
    for forbidden in ("hallpass.db", "hallpass.holds", "hallpass.web", "sqlite3"):
        assert forbidden not in source
