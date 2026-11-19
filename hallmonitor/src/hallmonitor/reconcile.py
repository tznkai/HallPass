# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reconcile loop. Each cycle reads the Console's desired state, asks each adapter what it observes, applies the
differences that may be applied, and posts one report per subject with the generation it acted on.

The decision table lives in `_decide`: an observation that is `unsupported`, `banned_external` or `error` is reported
as it is and never applied. A ban that HallMonitor did not place is never lifted.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from hallmonitor.adapters.base import SERVICE_WIDE_REASONS, Adapter, AdapterError, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.registry import build_adapter
from hallmonitor.client import MonitorClient, MonitorError
from hallmonitor.config import LOOPBACK_HOSTS, Config
from hallmonitor.contract import OBSERVED, Report, ServiceDesired, Subject
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


def _valid(observed: object) -> bool:
    """An Observed whose state the contract carries. Adapters are duck-typed, so anything else is refused here: a
    state outside OBSERVED would make the whole report body unbuildable and lose every service's reports."""
    return isinstance(observed, Observed) and isinstance(observed.state, str) and observed.state in OBSERVED


def _bad_state(slug: str, subject: Subject, now: datetime) -> Report:
    log_event(log, "bad_state", service=slug, level=logging.WARNING)   # the value itself is never logged
    return _report(slug, subject, "error", "bad_state", now)


def _needs_apply(observed: object, subject: Subject) -> bool:
    """True when `_decide` would call apply for this observation, the same test `_decide` applies."""
    return (isinstance(observed, Observed) and _valid(observed)
            and observed.state not in NEVER_APPLIED and observed.state != subject.desired)


async def _decide(adapter: Adapter, slug: str, subject: Subject, observed: Observed | None,
                  now: datetime) -> tuple[Report, str | None]:
    """The decision table. Only a difference from the desired state is applied, and an apply failure is an error.
    The second item is the reason when the apply raised a service-wide AdapterError, otherwise None."""
    if observed is None:
        return _report(slug, subject, "error", "not_observed", now), None
    if not _valid(observed):
        return _bad_state(slug, subject, now), None
    if observed.state in NEVER_APPLIED or observed.state == subject.desired:
        return _report(slug, subject, observed.state, observed.detail, now), None
    try:
        result = await adapter.apply(subject, subject.desired)
    except AdapterError as error:
        log_event(log, "apply_failed", service=slug, reason=error.reason, level=logging.WARNING)
        service_wide = error.reason if error.reason in SERVICE_WIDE_REASONS else None
        return _report(slug, subject, "error", error.reason, now), service_wide
    except Exception:  # noqa: BLE001 - an adapter bug must not stop the other subjects; it is reported as its class
        log_event(log, "apply_failed", service=slug, level=logging.WARNING)
        return _report(slug, subject, "error", "apply_failed", now), None
    if not _valid(result):
        return _bad_state(slug, subject, now), None
    return _report(slug, subject, result.state, result.detail, now), None


async def _apply_reports(adapter: Adapter, service: ServiceDesired, observed: Mapping[str, object],
                         now: datetime) -> list[Report]:
    """One report per subject. After an apply fails for a service-wide reason, the subjects of this service that
    would need an apply are reported with that reason and not applied, so a hung or refused service costs one wait."""
    reports: list[Report] = []
    blocked: str | None = None
    skip_logged = False
    for subject in service.subjects:
        one = observed.get(subject.ref)
        if blocked is not None and _needs_apply(one, subject):
            if not skip_logged:
                log_event(log, "apply_skipped", service=service.slug, reason=blocked, level=logging.WARNING)
                skip_logged = True
            reports.append(_report(service.slug, subject, "error", blocked, now))
            continue
        report, service_wide = await _decide(adapter, service.slug, subject, one, now)
        reports.append(report)
        blocked = blocked or service_wide
    return reports


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
        if not isinstance(observed, Mapping):   # adapters are duck-typed: a non-mapping fails the service, not the cycle
            log_event(log, "bad_state", service=service.slug, level=logging.WARNING)   # the value is never logged
            return [_report(service.slug, subject, "error", "bad_state", now) for subject in subjects]
        reports = await _apply_reports(adapter, service, observed, now)
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


def warn_if_insecure_transport(hallpass_url: str) -> None:
    """Warns once at startup when the bearer token travels over plain http to a host that is not loopback. The URL and
    the host are never logged; the event and its outcome say what the operator should check."""
    parts = urlsplit(hallpass_url)
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        log_event(log, "insecure_transport", outcome="plain_http_non_loopback", level=logging.WARNING)


async def run(config: Config, *, clock: Callable[[], datetime] = _utc_now) -> None:
    """The loop: a cycle, then a wait of the configured interval. A cycle that raises is logged and the loop goes on."""
    setup_logging(config.log_level, config.secrets())
    warn_if_insecure_transport(config.hallpass_url)
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
