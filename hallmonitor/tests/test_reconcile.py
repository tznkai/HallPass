# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reconcile cycle: applying differences, drift, unreachable services, unmanaged entries and the decision table."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from hallmonitor.adapters.base import AdapterError, Capabilities, Observed
from hallmonitor.adapters.manual import ManualAdapter
from hallmonitor.client import MonitorClient
from hallmonitor.config import Config
from hallmonitor.contract import ServiceDesired, Subject
from hallmonitor.db import Database
from hallmonitor.logs import JsonFormatter
from hallmonitor.reconcile import AdapterCache, CycleResult, cycle, warn_if_insecure_transport
from tests.fake_adapter import ListAdapter
from tests.fake_hallpass import TOKEN, FakeHallpassState, make_transport

URL = "https://hallpass.invalid"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
AT = "2026-11-13T12:00:00Z"


def subject(ref: str, value: str, desired: str, generation: int) -> dict[str, Any]:
    return {"ref": ref, "identities": [{"kind": "java_name", "value": value}], "desired": desired,
            "generation": generation}


def service(slug: str, subjects: list[dict[str, Any]], adapter: str = "list") -> dict[str, Any]:
    return {"slug": slug, "adapter": adapter, "subjects": subjects}


async def run_cycle(state: FakeHallpassState, adapters: dict[str, Any], now: datetime = NOW) -> CycleResult:
    client = MonitorClient(URL, TOKEN, transport=make_transport(state))
    try:
        return await cycle(client=client, adapter_for=lambda s: adapters.get(s.slug), now=now)
    finally:
        await client.aclose()


def entry(slug: str, ref: str, observed: str, generation: int, detail: str | None = None) -> dict[str, Any]:
    return {"slug": slug, "ref": ref, "observed": observed, "detail": detail, "generation": generation, "at": AT}


# AC9.5: a difference is applied, and the report carries the generation acted on -----------------------

async def test_allowed_is_applied_and_reported_with_the_desired_generation() -> None:
    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "allowed", 3)])])
    adapter = ListAdapter()
    result = await run_cycle(state, {"a": adapter})
    assert adapter.allow == {"Steve"}
    assert state.reports == [entry("a", "r1", "allowed", 3)]
    assert result.fetched is True


async def test_suspended_is_applied_and_reported() -> None:
    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "suspended", 4)])])
    adapter = ListAdapter(allow={"Steve"})
    await run_cycle(state, {"a": adapter})
    assert adapter.allow == set()
    assert state.reports == [entry("a", "r1", "suspended", 4)]


# AC9.6: drift is applied again on the next cycle ---------------------------------------------------

async def test_a_suspended_player_re_added_by_hand_is_removed_again() -> None:
    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "suspended", 4)])])
    adapter = ListAdapter()
    await run_cycle(state, {"a": adapter})
    assert state.reports[-1] == entry("a", "r1", "suspended", 4)

    adapter.allow.add("Steve")   # someone adds the player back by hand
    await run_cycle(state, {"a": adapter})
    assert adapter.allow == set()
    assert state.reports[-1] == entry("a", "r1", "suspended", 4)


# AC9.7: an unreachable service is reported and does not stop the others ----------------------------

async def test_an_unreachable_service_is_reported_and_the_others_still_reconcile() -> None:
    state = FakeHallpassState(services=[
        service("a", [subject("ra1", "Ann", "allowed", 1), subject("ra2", "Bo", "allowed", 1)]),
        service("b", [subject("rb1", "Cy", "allowed", 2)]),
    ])
    broken = ListAdapter(unreachable=True)
    working = ListAdapter()
    await run_cycle(state, {"a": broken, "b": working})
    assert state.reports == [
        entry("a", "ra1", "error", 1, "unreachable"),
        entry("a", "ra2", "error", 1, "unreachable"),
        entry("b", "rb1", "allowed", 2),
    ]
    assert working.allow == {"Cy"}
    assert broken.allow == set()


async def test_the_unreachable_service_reconciles_on_the_next_cycle() -> None:
    state = FakeHallpassState(services=[service("a", [subject("ra1", "Ann", "allowed", 1)])])
    adapter = ListAdapter(unreachable=True)
    await run_cycle(state, {"a": adapter})
    assert state.reports[-1] == entry("a", "ra1", "error", 1, "unreachable")

    adapter.unreachable = False
    await run_cycle(state, {"a": adapter})
    assert adapter.allow == {"Ann"}
    assert state.reports[-1] == entry("a", "ra1", "allowed", 1)


# AC9.10: entries on the service that hallpass does not list are left untouched and counted ----------

async def test_an_unmanaged_entry_is_left_alone_and_counted() -> None:
    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "allowed", 3)])])
    adapter = ListAdapter(allow={"Stranger"})
    await run_cycle(state, {"a": adapter})
    assert "Stranger" in adapter.allow
    assert state.unmanaged == [{"slug": "a", "count": 1}]


# The decision table ---------------------------------------------------------------------------------

class StubAdapter:
    """An adapter whose observation is fixed per ref, so each row of the decision table can be set up."""

    capabilities = Capabilities(suspend=True, restore=True, read_state=True, removes_connected=True)

    def __init__(self, observed: dict[str, Observed], apply_error: bool = False) -> None:
        self.observed = observed
        self.apply_error = apply_error
        self.applied: list[str] = []

    async def observe(self, subjects: Sequence[Subject]) -> dict[str, Observed]:
        return {ref: obs for ref, obs in self.observed.items() if any(s.ref == ref for s in subjects)}

    async def apply(self, subject: Subject, desired: str) -> Observed:
        self.applied.append(subject.ref)
        if self.apply_error:
            raise RuntimeError("boom")
        return Observed(desired)  # type: ignore[arg-type]

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
        return None


@pytest.mark.parametrize("observed_state", ["unsupported", "banned_external", "error"])
async def test_a_ban_or_an_unsupported_or_error_observation_is_reported_and_never_applied(
        observed_state: str) -> None:
    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "allowed", 2)])])
    stub = StubAdapter({"r1": Observed(observed_state, "why")})  # type: ignore[arg-type]
    await run_cycle(state, {"a": stub})
    assert stub.applied == []
    assert state.reports == [entry("a", "r1", observed_state, 2, "why")]


async def test_a_matching_observation_is_reported_without_an_apply() -> None:
    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "suspended", 5)])])
    stub = StubAdapter({"r1": Observed("suspended", "no_account")})
    await run_cycle(state, {"a": stub})
    assert stub.applied == []
    assert state.reports == [entry("a", "r1", "suspended", 5, "no_account")]


async def test_a_subject_missing_from_the_observation_is_an_error_not_observed() -> None:
    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "allowed", 1)])])
    await run_cycle(state, {"a": StubAdapter({})})
    assert state.reports == [entry("a", "r1", "error", 1, "not_observed")]


async def test_an_exception_during_apply_is_reported_as_apply_failed() -> None:
    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "allowed", 1)])])
    await run_cycle(state, {"a": StubAdapter({"r1": Observed("suspended")}, apply_error=True)})
    assert state.reports == [entry("a", "r1", "error", 1, "apply_failed")]


async def test_an_adapter_error_from_apply_keeps_its_reason_in_the_report_and_the_log(
        caplog: pytest.LogCaptureFixture) -> None:
    class Unreachable(StubAdapter):
        async def apply(self, subject: Subject, desired: str) -> Observed:
            raise AdapterError("unreachable")

    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "allowed", 1)])])
    with caplog.at_level(logging.WARNING):
        await run_cycle(state, {"a": Unreachable({"r1": Observed("suspended")})})
    assert state.reports == [entry("a", "r1", "error", 1, "unreachable")]
    logged = [json.loads(JsonFormatter().format(record)) for record in caplog.records
              if record.getMessage() == "apply_failed"]
    assert [(event["service"], event["reason"], event["level"]) for event in logged] == [
        ("a", "unreachable", "WARNING")]


# A service-wide failure on apply stops the service's remaining applies --------------------------------

class RaisingAdapter(StubAdapter):
    """Every apply is recorded first; an apply for a ref in `failing` raises AdapterError(reason)."""

    def __init__(self, observed: dict[str, Observed], *, reason: str, failing: set[str]) -> None:
        super().__init__(observed)
        self.reason = reason
        self.failing = failing

    async def apply(self, subject: Subject, desired: str) -> Observed:
        self.applied.append(subject.ref)
        if subject.ref in self.failing:
            raise AdapterError(self.reason)
        return Observed(desired)


SKIP_ROWS = [
    subject("r1", "Steve", "allowed", 1),     # fails on apply
    subject("r2", "Alex", "allowed", 2),      # needs an apply: skipped
    subject("r3", "Sam", "allowed", 3),       # already allowed: reported without an apply
    subject("r4", "Kai", "suspended", 4),     # needs an apply: skipped
]
SKIP_OBSERVED = {"r1": Observed("suspended"), "r2": Observed("suspended"), "r3": Observed("allowed"),
                 "r4": Observed("allowed")}


@pytest.mark.parametrize("reason", ["auth", "timeout", "unreachable"])
async def test_a_service_wide_apply_failure_reports_the_rest_of_its_applies_without_calling_them(
        reason: str, caplog: pytest.LogCaptureFixture) -> None:
    state = FakeHallpassState(services=[service("a", SKIP_ROWS)])
    adapter = RaisingAdapter(dict(SKIP_OBSERVED), reason=reason, failing={"r1"})
    with caplog.at_level(logging.WARNING):
        await run_cycle(state, {"a": adapter})
    assert adapter.applied == ["r1"]
    assert state.reports == [
        entry("a", "r1", "error", 1, reason),
        entry("a", "r2", "error", 2, reason),
        entry("a", "r3", "allowed", 3),
        entry("a", "r4", "error", 4, reason),
    ]
    skipped = [json.loads(JsonFormatter().format(record)) for record in caplog.records
               if record.getMessage() == "apply_skipped"]
    assert [(event["service"], event["reason"]) for event in skipped] == [("a", reason)]


async def test_a_subject_that_needs_no_apply_keeps_its_own_observation_after_a_service_wide_failure() -> None:
    rows = [subject("r1", "Steve", "allowed", 1), subject("r2", "Alex", "allowed", 2),
            subject("r3", "Sam", "suspended", 3)]
    observed = {"r1": Observed("suspended"), "r2": Observed("error", "bad_identity"),
                "r3": Observed("suspended")}
    state = FakeHallpassState(services=[service("a", rows)])
    adapter = RaisingAdapter(observed, reason="unreachable", failing={"r1"})
    await run_cycle(state, {"a": adapter})
    assert adapter.applied == ["r1"]
    assert state.reports == [
        entry("a", "r1", "error", 1, "unreachable"),
        entry("a", "r2", "error", 2, "bad_identity"),
        entry("a", "r3", "suspended", 3),
    ]


async def test_a_failure_that_is_not_service_wide_does_not_stop_the_other_applies() -> None:
    state = FakeHallpassState(services=[service("a", SKIP_ROWS)])
    adapter = RaisingAdapter(dict(SKIP_OBSERVED), reason="protocol", failing={"r1"})
    await run_cycle(state, {"a": adapter})
    assert adapter.applied == ["r1", "r2", "r4"]
    assert state.reports == [
        entry("a", "r1", "error", 1, "protocol"),
        entry("a", "r2", "allowed", 2),
        entry("a", "r3", "allowed", 3),
        entry("a", "r4", "suspended", 4),
    ]


async def test_a_service_wide_failure_does_not_stop_the_next_service() -> None:
    first = RaisingAdapter({"r1": Observed("suspended"), "r2": Observed("suspended")},
                           reason="auth", failing={"r1"})
    second = RaisingAdapter({"r3": Observed("suspended")}, reason="auth", failing=set())
    state = FakeHallpassState(services=[
        service("a", [subject("r1", "Steve", "allowed", 1), subject("r2", "Alex", "allowed", 2)]),
        service("b", [subject("r3", "Sam", "allowed", 3)]),
    ])
    await run_cycle(state, {"a": first, "b": second})
    assert first.applied == ["r1"]
    assert second.applied == ["r3"]
    assert state.reports == [
        entry("a", "r1", "error", 1, "auth"),
        entry("a", "r2", "error", 2, "auth"),
        entry("b", "r3", "allowed", 3),
    ]


async def test_a_service_with_no_adapter_reports_every_subject_as_error() -> None:
    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "allowed", 1)])])
    await run_cycle(state, {})
    assert state.reports == [entry("a", "r1", "error", 1, "no_adapter")]


async def test_an_auth_failure_from_observe_is_reported_with_its_reason_and_the_next_service_runs() -> None:
    class Denied(StubAdapter):
        async def observe(self, subjects: Sequence[Subject]) -> dict[str, Observed]:
            raise AdapterError("auth")

    state = FakeHallpassState(services=[
        service("a", [subject("r1", "Steve", "allowed", 1)]),
        service("b", [subject("r2", "Ann", "allowed", 1)]),
    ])
    working = ListAdapter()
    await run_cycle(state, {"a": Denied({}), "b": working})
    assert state.reports == [entry("a", "r1", "error", 1, "auth"), entry("b", "r2", "allowed", 1)]


async def test_an_unexpected_exception_from_observe_is_contained_to_its_service() -> None:
    class Broken(StubAdapter):
        async def observe(self, subjects: Sequence[Subject]) -> dict[str, Observed]:
            raise RuntimeError("bug")

    state = FakeHallpassState(services=[
        service("a", [subject("r1", "Steve", "allowed", 1)]),
        service("b", [subject("r2", "Ann", "allowed", 1)]),
    ])
    await run_cycle(state, {"a": Broken({}), "b": ListAdapter()})
    assert state.reports == [entry("a", "r1", "error", 1, "adapter_failed"), entry("b", "r2", "allowed", 1)]


class BadApply(StubAdapter):
    """Observes a difference, then answers the apply with a state the contract does not carry."""

    async def apply(self, subject: Subject, desired: str) -> Observed:
        self.applied.append(subject.ref)
        return Observed("bogus")  # type: ignore[arg-type]


async def test_an_unknown_observed_state_is_bad_state_and_the_other_services_still_post(
        caplog: pytest.LogCaptureFixture) -> None:
    state = FakeHallpassState(services=[
        service("a", [subject("r1", "Steve", "allowed", 2)]),
        service("b", [subject("r2", "Ann", "allowed", 3)]),
    ])
    stub = StubAdapter({"r1": Observed("bogus")})  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        await run_cycle(state, {"a": stub, "b": ListAdapter()})
    assert stub.applied == []
    assert state.reports == [entry("a", "r1", "error", 2, "bad_state"), entry("b", "r2", "allowed", 3)]
    logged = [json.loads(JsonFormatter().format(record)) for record in caplog.records
              if record.getMessage() == "bad_state"]
    assert [(event["service"], event["level"]) for event in logged] == [("a", "WARNING")]
    assert all("bogus" not in JsonFormatter().format(record) for record in caplog.records)


async def test_an_unknown_state_from_apply_is_bad_state_and_the_other_services_still_post() -> None:
    state = FakeHallpassState(services=[
        service("a", [subject("r1", "Steve", "allowed", 2)]),
        service("b", [subject("r2", "Ann", "allowed", 3)]),
    ])
    await run_cycle(state, {"a": BadApply({"r1": Observed("suspended")}), "b": ListAdapter()})
    assert state.reports == [entry("a", "r1", "error", 2, "bad_state"), entry("b", "r2", "allowed", 3)]


class NotAMapping(StubAdapter):
    """Observes with whatever it was given, so an observe that returns something other than a mapping can be set up."""

    def __init__(self, returned: object) -> None:
        super().__init__({})
        self.returned = returned

    async def observe(self, subjects: Sequence[Subject]) -> Any:
        return self.returned


@pytest.mark.parametrize("returned", [["r1"], "allowed", None])
async def test_an_observe_that_is_not_a_mapping_is_bad_state_for_its_service_only(
        returned: object, caplog: pytest.LogCaptureFixture) -> None:
    state = FakeHallpassState(services=[
        service("a", [subject("r1", "Steve", "allowed", 2), subject("r2", "Ann", "allowed", 2)]),
        service("b", [subject("r3", "Cy", "allowed", 3)]),
    ])
    stub = NotAMapping(returned)
    with caplog.at_level(logging.WARNING):
        await run_cycle(state, {"a": stub, "b": ListAdapter()})
    assert stub.applied == []
    assert state.reports == [
        entry("a", "r1", "error", 2, "bad_state"),
        entry("a", "r2", "error", 2, "bad_state"),
        entry("b", "r3", "allowed", 3),
    ]
    logged = [json.loads(JsonFormatter().format(record)) for record in caplog.records
              if record.getMessage() == "bad_state"]
    assert len(logged) == 1
    assert logged[0]["service"] == "a"
    assert logged[0]["level"] == "WARNING"
    assert {key: value for key, value in logged[0].items() if key not in ("ts", "level", "logger", "event")} == {
        "service": "a"}


async def test_a_failing_unmanaged_count_does_not_stop_the_reports() -> None:
    class Counting(StubAdapter):
        async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
            raise AdapterError("http")

    state = FakeHallpassState(services=[service("a", [subject("r1", "Steve", "suspended", 1)])])
    await run_cycle(state, {"a": Counting({"r1": Observed("suspended")})})
    assert state.reports == [entry("a", "r1", "suspended", 1)]
    assert state.unmanaged == []


# HallPass Console down, and the report post ---------------------------------------------------------

async def test_when_hallpass_is_down_nothing_is_applied_and_the_cycle_reports_not_fetched() -> None:
    state = FakeHallpassState(fail_desired=True, services=[service("a", [subject("r1", "Steve", "allowed", 1)])])
    adapter = ListAdapter()
    result = await run_cycle(state, {"a": adapter})
    assert result.fetched is False
    assert result.reports == []
    assert adapter.calls == []
    assert state.reports == []


async def test_a_failed_report_post_is_logged_and_the_next_cycle_recomputes(caplog: pytest.LogCaptureFixture) -> None:
    state = FakeHallpassState(fail_reports=True, services=[service("a", [subject("r1", "Steve", "allowed", 1)])])
    adapter = ListAdapter()
    with caplog.at_level(logging.INFO):
        result = await run_cycle(state, {"a": adapter})
    assert result.fetched is True
    assert any(r.getMessage() == "reports_post_failed" for r in caplog.records)

    state.fail_reports = False
    await run_cycle(state, {"a": adapter})
    assert state.reports == [entry("a", "r1", "allowed", 1)]


# The adapter cache used by the entry point ----------------------------------------------------------

@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database.open(tmp_path / "hallmonitor.db")
    yield db
    db.close()


async def test_adapter_cache_builds_once_and_rebuilds_when_the_name_changes(database: Database) -> None:
    async with httpx.AsyncClient() as http:
        cache = AdapterCache(
            config=Config(hallpass_url=URL, hallpass_token=TOKEN),
            db=database,
            http=http,
            clock=lambda: NOW,
        )
        first = cache.for_service(ServiceDesired(slug="abiotic", adapter="manual", subjects=()))
        assert isinstance(first, ManualAdapter)
        assert cache.for_service(ServiceDesired(slug="abiotic", adapter="manual", subjects=())) is first

        assert cache.for_service(ServiceDesired(slug="abiotic", adapter="no-such", subjects=())) is None


# Plain http to a remote host is logged once; https and loopback are not --------------------------------

@pytest.mark.parametrize("url", ["http://hallpass.invalid", "http://hallpass.invalid:8080/base"])
def test_plain_http_to_a_remote_host_logs_one_warning_and_no_host(url: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        warn_if_insecure_transport(url)
    events = [json.loads(JsonFormatter().format(record)) for record in caplog.records]
    assert [event["event"] for event in events] == ["insecure_transport"]
    assert events[0]["level"] == "WARNING"
    assert events[0]["outcome"] == "plain_http_non_loopback"
    assert "hallpass.invalid" not in json.dumps(events)


@pytest.mark.parametrize("url", ["https://hallpass.invalid", "http://127.0.0.1:8080", "http://[::1]:8080"])
def test_https_or_loopback_logs_nothing(url: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        warn_if_insecure_transport(url)
    assert caplog.records == []


async def test_adapter_cache_gives_each_slug_its_own_adapter(database: Database) -> None:
    async with httpx.AsyncClient() as http:
        cache = AdapterCache(config=Config(hallpass_url=URL, hallpass_token=TOKEN), db=database, http=http,
                             clock=lambda: NOW)
        one = cache.for_service(ServiceDesired(slug="one", adapter="manual", subjects=()))
        two = cache.for_service(ServiceDesired(slug="two", adapter="manual", subjects=()))
        assert one is not two

