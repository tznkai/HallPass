# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The monitor API: hallpass's desired state for hallmonitor, and the reports hallmonitor sends back.

It is a separate FastAPI app on its own listener and is never mounted on the public app. Every request is refused with
403 when it carries a proxy header, and with 401 unless it presents the monitor bearer token. The response models below
are the only shapes that leave the process, so no display name, contact, account type or parent link can appear in one.
"""

from __future__ import annotations

import hmac
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from hallpass import desired, registry, services
from hallpass.logs import log_event
from hallpass.registry import _iso
from hallpass.web import app as web_app

logger = logging.getLogger(__name__)
# A request carrying any of these came through a proxy. The monitor listener is never reached through one.
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "via")


class Identity(BaseModel):
    kind: str
    value: str


class Subject(BaseModel):
    ref: str
    identities: list[Identity]
    desired: Literal["allowed", "suspended"]
    generation: int


class ServiceDesired(BaseModel):
    slug: str
    adapter: str
    subjects: list[Subject]


class DesiredResponse(BaseModel):
    services: list[ServiceDesired]


class ReportIn(BaseModel):
    slug: str
    ref: str
    observed: Literal["allowed", "suspended", "unsupported", "banned_external", "error"]
    detail: str | None = None
    generation: int
    at: datetime

    @field_validator("at")
    @classmethod
    def _has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("at must carry a timezone")
        return value


class Unmanaged(BaseModel):
    slug: str
    count: int = Field(ge=0)


class ReportsIn(BaseModel):
    reports: list[ReportIn]
    unmanaged: list[Unmanaged] = Field(default_factory=list)


_UPSERT_REPORT = (
    "INSERT INTO service_report (person_id, service_id, observed, detail, generation, reported_at) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (person_id, service_id) DO UPDATE SET observed = excluded.observed, detail = excluded.detail, "
    "generation = excluded.generation, reported_at = excluded.reported_at "
    "WHERE excluded.reported_at >= service_report.reported_at"
)
_UPSERT_UNMANAGED = (
    "INSERT INTO service_unmanaged (service_id, count, reported_at) VALUES (?, ?, ?) "
    "ON CONFLICT (service_id) DO UPDATE SET count = excluded.count, reported_at = excluded.reported_at"
)
_UPSERT_CONTACT = (
    "INSERT INTO monitor_contact (id, last_contact_at) VALUES (1, ?) "
    "ON CONFLICT (id) DO UPDATE SET last_contact_at = excluded.last_contact_at"
)


def _touch_contact(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(_UPSERT_CONTACT, (_iso(now),))


def _desired_response(services_list: list[services.Service], rows: list[desired.DesiredRow]) -> DesiredResponse:
    """One entry per monitored service, including those with no subjects, so hallmonitor can count their unmanaged
    entries. Subjects keep the row order: person id within each service."""
    grouped = {service.slug: ServiceDesired(slug=service.slug, adapter=service.adapter, subjects=[])
               for service in services_list}
    for row in rows:
        grouped[row.service.slug].subjects.append(Subject(
            ref=row.person.ref,
            identities=[Identity(kind=kind, value=value) for kind, value in row.identities],
            desired=row.desired,
            generation=row.generation,
        ))
    return DesiredResponse(services=list(grouped.values()))


@dataclass
class _Outcome:
    """What one reports request stored, and what it skipped by reason."""
    stored: int = 0
    unknown_ref: int = 0
    not_sent: int = 0
    unknown_slug: int = 0


def _is_sent(conn: sqlite3.Connection, person_id: int, service_id: int, generation: int) -> bool:
    """True when the pair is sent now (`desired.desired_for` reads a row) and the report's generation is not above the
    stored one. A pair that is not sent was not in the last desired read (it is not a subject, or it was dropped), so a
    late report for it is refused. A report above the stored generation answers a value that was never sent. A report
    at or below it is stored, and it shows as confirmed only when it carries the stored generation (see
    `desired.report_status`)."""
    stored = desired.desired_for(conn, person_id, service_id)
    return stored is not None and generation <= stored[1]


def _store_reports(conn: sqlite3.Connection, body: ReportsIn, now: datetime) -> _Outcome:
    """Stores each report and unmanaged count. A report or count naming an unknown slug is skipped as unknown_slug. A
    report is skipped as unknown_ref when its ref is unknown, and as not_sent when its pair is not sent or its
    generation is above the stored one (see `_is_sent`). A report's time is clamped to now, so a clock running ahead
    cannot block later reports, since the newest time wins. A report older than the stored one for its pair does not
    replace it."""
    outcome = _Outcome()
    for report in body.reports:
        service = services.get_by_slug(conn, report.slug)
        if service is None:
            outcome.unknown_slug += 1
            continue
        person = registry.get_by_ref(conn, report.ref)
        if person is None:
            outcome.unknown_ref += 1
            continue
        if not _is_sent(conn, person.id, service.id, report.generation):
            outcome.not_sent += 1
            continue
        conn.execute(_UPSERT_REPORT, (person.id, service.id, report.observed, report.detail, report.generation,
                                      _iso(min(report.at, now))))
        outcome.stored += 1
    for item in body.unmanaged:
        service = services.get_by_slug(conn, item.slug)
        if service is None:
            outcome.unknown_slug += 1
            continue
        conn.execute(_UPSERT_UNMANAGED, (service.id, item.count, _iso(now)))
    _touch_contact(conn, now)
    return outcome


def create_monitor_app(deps: web_app.AppDeps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.deps = deps

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if any(name in request.headers for name in PROXY_HEADERS):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        presented = request.headers.get("authorization", "").encode()
        expected = f"Bearer {deps.config.monitor_token}".encode()
        if not hmac.compare_digest(presented, expected):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/api/monitor/v1/desired", response_model=DesiredResponse)
    async def get_desired() -> DesiredResponse:
        now = deps.clock()

        def read(conn: sqlite3.Connection) -> tuple[list[services.Service], list[desired.DesiredRow]]:
            rows = desired.refresh(conn, now)
            _touch_contact(conn, now)
            return desired.monitored_services(conn), rows

        monitored, rows = await deps.db.run(read)
        log_event(logger, "monitor_desired", count=len(rows))
        return _desired_response(monitored, rows)

    @app.post("/api/monitor/v1/reports", status_code=204)
    async def post_reports(body: ReportsIn) -> Response:
        now = deps.clock()
        outcome = await deps.db.run(lambda conn: _store_reports(conn, body, now))
        log_event(logger, "monitor_reports", count=outcome.stored)
        if outcome.unknown_ref:
            log_event(logger, "monitor_reports_skipped", count=outcome.unknown_ref, reason="unknown_ref")
        if outcome.not_sent:
            log_event(logger, "monitor_reports_skipped", count=outcome.not_sent, reason="not_sent")
        if outcome.unknown_slug:
            log_event(logger, "monitor_reports_skipped", count=outcome.unknown_slug, reason="unknown_slug")
        return Response(status_code=204)

    return app
