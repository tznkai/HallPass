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


def _store_reports(conn: sqlite3.Connection, body: ReportsIn, now: datetime) -> tuple[int, int]:
    """Stores each report and unmanaged count. Returns (stored, skipped). A report for an unknown slug or ref is skipped.
    A report older than the stored one for its pair does not replace it."""
    stored = skipped = 0
    for report in body.reports:
        service = services.get_by_slug(conn, report.slug)
        person = registry.get_by_ref(conn, report.ref)
        if service is None or person is None:
            skipped += 1
            continue
        conn.execute(_UPSERT_REPORT, (person.id, service.id, report.observed, report.detail, report.generation,
                                      _iso(report.at)))
        stored += 1
    for item in body.unmanaged:
        service = services.get_by_slug(conn, item.slug)
        if service is None:
            skipped += 1
            continue
        conn.execute(_UPSERT_UNMANAGED, (service.id, item.count, _iso(now)))
    _touch_contact(conn, now)
    return stored, skipped


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
        stored, skipped = await deps.db.run(lambda conn: _store_reports(conn, body, now))
        log_event(logger, "monitor_reports", count=stored)
        if skipped:
            log_event(logger, "monitor_reports_skipped", count=skipped, reason="unknown_ref")
        return Response(status_code=204)

    return app
