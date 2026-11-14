# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The monitor client against the fake hallpass listener."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hallmonitor.client import MonitorClient, MonitorError
from hallmonitor.contract import Report
from tests.fake_hallpass import TOKEN, FakeHallpassState, make_transport

URL = "https://hallpass.invalid"
EXAMPLE = json.loads((Path(__file__).parent / "contract" / "monitor_v1.json").read_text("utf-8"))


def client_for(state: FakeHallpassState, token: str = TOKEN) -> MonitorClient:
    return MonitorClient(URL, token, transport=make_transport(state))


async def test_fetch_desired_parses_the_served_body() -> None:
    state = FakeHallpassState(services=copy.deepcopy(EXAMPLE["desired"]["services"]))
    client = client_for(state)
    services = await client.fetch_desired()
    assert [s.slug for s in services] == ["minecraft", "fluxer"]
    assert services[1].subjects[0].generation == 2


async def test_post_reports_is_recorded_by_the_fake() -> None:
    state = FakeHallpassState()
    client = client_for(state)
    at = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    await client.post_reports([Report("minecraft", "ref-a", "allowed", None, 3, at)], {"minecraft": 1})
    assert state.reports == [{
        "slug": "minecraft", "ref": "ref-a", "observed": "allowed", "detail": None, "generation": 3,
        "at": "2026-11-13T12:00:00Z",
    }]
    assert state.unmanaged == [{"slug": "minecraft", "count": 1}]


async def test_a_wrong_token_is_refused_with_its_status() -> None:
    client = client_for(FakeHallpassState(), token="w" * 40)
    with pytest.raises(MonitorError) as info:
        await client.fetch_desired()
    assert info.value.reason == "http"
    assert info.value.status == 401


async def test_a_wrong_token_on_post_is_refused_with_its_status() -> None:
    client = client_for(FakeHallpassState(), token="w" * 40)
    with pytest.raises(MonitorError) as info:
        await client.post_reports([], {})
    assert info.value.status == 401


async def test_an_unreachable_hallpass_is_reported_as_unreachable() -> None:
    client = client_for(FakeHallpassState(fail_desired=True))
    with pytest.raises(MonitorError) as info:
        await client.fetch_desired()
    assert info.value.reason == "unreachable"
    assert info.value.status is None


async def test_a_body_that_does_not_match_the_contract_is_a_contract_error_reason() -> None:
    state = FakeHallpassState(services=[{"slug": "minecraft", "adapter": "manual"}])
    client = client_for(state)
    with pytest.raises(MonitorError) as info:
        await client.fetch_desired()
    assert info.value.reason == "contract"
