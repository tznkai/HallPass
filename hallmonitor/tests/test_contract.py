# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wire types against the recorded example that both projects share."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hallmonitor.contract import (
    OBSERVED,
    ContractError,
    Identity,
    Report,
    ServiceDesired,
    Subject,
    parse_desired,
    reports_body,
)

EXAMPLE = json.loads((Path(__file__).parent / "contract" / "monitor_v1.json").read_text("utf-8"))


def test_parse_desired_reads_the_example() -> None:
    services = parse_desired(EXAMPLE["desired"])
    assert services == [
        ServiceDesired(
            slug="minecraft",
            adapter="manual",
            subjects=(Subject(ref="ref-a", identities=(Identity("java_name", "Steve"),),
                              desired="allowed", generation=3),),
        ),
        ServiceDesired(
            slug="fluxer",
            adapter="fluxer",
            subjects=(Subject(ref="ref-a",
                              identities=(Identity("sso_subject", "00000000-0000-4000-8000-000000000001"),),
                              desired="suspended", generation=2),),
        ),
    ]


def test_missing_generation_raises() -> None:
    body = copy.deepcopy(EXAMPLE["desired"])
    del body["services"][0]["subjects"][0]["generation"]
    with pytest.raises(ContractError):
        parse_desired(body)


@pytest.mark.parametrize("bad", ["banned", "", None])
def test_desired_value_must_be_allowed_or_suspended(bad: object) -> None:
    body = copy.deepcopy(EXAMPLE["desired"])
    body["services"][0]["subjects"][0]["desired"] = bad
    with pytest.raises(ContractError):
        parse_desired(body)


def test_extra_fields_in_the_desired_body_are_not_kept() -> None:
    body = copy.deepcopy(EXAMPLE["desired"])
    body["services"][0]["subjects"][0]["display_name"] = "Zebulon Quux"
    services = parse_desired(body)
    subject = services[0].subjects[0]
    assert not hasattr(subject, "display_name")
    assert "Zebulon Quux" not in repr(services)


def test_reports_body_matches_the_example() -> None:
    entry = EXAMPLE["reports"]["reports"][0]
    report = Report(
        slug=entry["slug"],
        ref=entry["ref"],
        observed=entry["observed"],
        detail=entry["detail"],
        generation=entry["generation"],
        at=datetime(2026, 11, 13, 12, 0, tzinfo=UTC),
    )
    body = reports_body([report], {"minecraft": 1})
    assert body == EXAMPLE["reports"]


def test_reports_body_formats_time_as_utc_with_z() -> None:
    at = datetime(2026, 11, 13, 14, 30, 5, tzinfo=UTC)
    body = reports_body([Report("s", "r", "error", "unreachable", 1, at)], {})
    assert body["reports"][0]["at"] == "2026-11-13T14:30:05Z"
    assert body["unmanaged"] == []


def test_reports_body_refuses_an_unknown_observed_value() -> None:
    report = Report("s", "r", "banned", None, 1, datetime(2026, 11, 13, tzinfo=UTC))
    with pytest.raises(ContractError):
        reports_body([report], {})


def test_reports_body_refuses_a_naive_time() -> None:
    report = Report("s", "r", "allowed", None, 1, datetime(2026, 11, 13))
    with pytest.raises(ContractError):
        reports_body([report], {})


def test_observed_values_are_the_wire_literals() -> None:
    assert OBSERVED == frozenset({"allowed", "suspended", "unsupported", "banned_external", "error"})
