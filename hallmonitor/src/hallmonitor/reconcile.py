# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reconcile loop. Each cycle reads hallpass's desired state, asks each adapter what it observes, applies the
differences that may be applied, and posts one report per subject with the generation it acted on.

The decision table lives in `_decide`: an observation that is `unsupported`, `banned_external` or `error` is reported
as it is and never applied. A ban that hallmonitor did not place is never lifted.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from hallmonitor.adapters.base import Adapter, AdapterError, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.registry import build_adapter
from hallmonitor.client import MonitorClient, MonitorError
from hallmonitor.config import Config
from hallmonitor.contract import Report, ServiceDesired, Subject
from hallmonitor.db import Database
from hallmonitor.logs import log_event, setup_logging

log = logging.getLogger(__name__)
HTTP_TIMEOUT_SECONDS = 30.0
NEVER_APPLIED = frozenset({"unsupported", "banned_external", "error"})
TRANSPORT_FAILURES = (httpx.HTTPError, OSError)


@dataclass
class CycleResult:
    reports: list[Report] = field(default_factory=list)
    unmanaged: dict[str, int] = field(default_factory=dict)
    fetched: bool = True


AdapterFor = Callable[[ServiceDesired], Adapter | None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _report(slug: str, subject: Subject, observed: str, detail: str | None, now: datetime) -> Report:
    return Report(slug=slug, ref=subject.ref, observed=observed, detail=detail, generation=subject.generation, at=now)


async def _decide(adapter: Adapter, slug: str, subject: Subject, observed: Observed | None,
                  now: datetime) -> Report:
    """The decision table. Only a difference from the desired state is applied, and an apply failure is an error."""
    if observed is None:
        return _report(slug, subject, "error", "not_observed", now)
    if observed.state in NEVER_APPLIED or observed.state == subject.desired:
        return _report(slug, subject, observed.state, observed.detail, now)
    try:
        result = await adapter.apply(subject, subject.desired)
    except Exception:  # noqa: BLE001 - an adapter bug must not stop the other subjects; it is reported as its class
        log_event(log, "apply_failed", service=slug, level=logging.WARNING)
        return _report(slug, subject, "error", "apply_failed", now)
    return _report(slug, subject, result.state, result.detail, now)


async def _service_reports(service: ServiceDesired, adapter: Adapter | None, now: datetime,
                           unmanaged: dict[str, int]) -> list[Report]:
    subjects = service.subjects
    if adapter is None:
        return [_report(service.slug, subject, "error", "no_adapter", now) for subject in subjects]
    try:
        observed = await adapter.observe(subjects)
    except AdapterError as error:
        reason = error.reason
    except TRANSPORT_FAILURES:
        reason = "unreachable"
    except Exception:  # noqa: BLE001 - contain an adapter bug to its own service
        reason = "adapter_failed"
    else:
        reports = [await _decide(adapter, service.slug, subject, observed.get(subject.ref), now)
                   for subject in subjects]
        count = await _count(adapter, service.slug, subjects)
        if count is not None:
            unmanaged[service.slug] = count
        return reports
    log_event(log, "observe_failed", service=service.slug, reason=reason, level=logging.WARNING)
    return [_report(service.slug, subject, "error", reason, now) for subject in subjects]


async def _count(adapter: Adapter, slug: str, subjects: Sequence[Subject]) -> int | None:
    try:
        return await adapter.unmanaged_count(subjects)
    except Exception:  # noqa: BLE001 - a failed count leaves the count unreported; the reports still go
        log_event(log, "unmanaged_failed", service=slug, level=logging.WARNING)
        return None


def _log_summary(reports: Sequence[Report]) -> None:
    for state, count in sorted(Counter(report.observed for report in reports).items()):
        log_event(log, "cycle_subjects", observed=state, count=count)
    log_event(log, "cycle_done", count=len(reports))


async def cycle(*, client: MonitorClient, adapter_for: AdapterFor, now: datetime) -> CycleResult:
    """One pass: fetch, observe and apply per service, then post the reports. Nothing is queued across cycles."""
    try:
        services = await client.fetch_desired()
    except MonitorError as error:
        log_event(log, "desired_fetch_failed", reason=error.reason, status=error.status, level=logging.WARNING)
        return CycleResult(fetched=False)
    reports: list[Report] = []
    unmanaged: dict[str, int] = {}
    for service in services:
        reports.extend(await _service_reports(service, adapter_for(service), now, unmanaged))
    try:
        await client.post_reports(reports, unmanaged)
    except MonitorError as error:
        log_event(log, "reports_post_failed", reason=error.reason, status=error.status, level=logging.WARNING)
    _log_summary(reports)
    return CycleResult(reports=reports, unmanaged=unmanaged, fetched=True)


class AdapterCache:
    """Builds one adapter per service slug, on first sight, and rebuilds it when the adapter name changes."""

    def __init__(self, *, config: Config, db: Database, http: httpx.AsyncClient, clock: Callable[[], datetime]) -> None:
        self._config = config
        self._db = db
        self._http = http
        self._clock = clock
        self._built: dict[str, tuple[str, Adapter | None]] = {}

    def for_service(self, service: ServiceDesired) -> Adapter | None:
        cached = self._built.get(service.slug)
        if cached is not None and cached[0] == service.adapter:
            return cached[1]
        ctx = AdapterContext(
            slug=service.slug,
            settings=self._config.service_settings.get(service.slug, {}),
            config=self._config,
            db=self._db,
            http=self._http,
            clock=self._clock,
        )
        adapter = build_adapter(service.adapter, ctx)
        if adapter is None:
            log_event(log, "adapter_unavailable", service=service.slug, adapter=service.adapter)
        self._built[service.slug] = (service.adapter, adapter)
        return adapter


async def run(config: Config, *, clock: Callable[[], datetime] = _utc_now) -> None:
    """The loop: a cycle, then a wait of the configured interval. A cycle that raises is logged and the loop goes on."""
    setup_logging(config.log_level, config.secrets())
    db = Database.open(config.data_dir / "hallmonitor.db")
    http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
    client = MonitorClient(config.hallpass_url, config.hallpass_token)
    adapters = AdapterCache(config=config, db=db, http=http, clock=clock)
    try:
        while True:
            try:
                await cycle(client=client, adapter_for=adapters.for_service, now=clock())
            except Exception:  # noqa: BLE001 - the loop outlives any single cycle
                log_event(log, "cycle_failed", level=logging.ERROR)
            await asyncio.sleep(config.interval_seconds)
    finally:
        await client.aclose()
        await http.aclose()
        db.close()
