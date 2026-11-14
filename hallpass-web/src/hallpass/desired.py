# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Desired state for hallmonitor: each monitored (service, subject) pair with its desired value and generation.

`refresh` recomputes `access.evaluate` for every subject of every monitored service and stores the result. A pair gets
generation 1 the first time it is seen, and goes up by one each time its desired value changes. A report carrying an
older generation therefore shows that the current value is not yet confirmed. `report_status` maps a report onto the
text a row shows. Plain functions over a sqlite3 connection; callers run them inside `Database.run`. Nothing here logs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from hallpass import access, registry, services, text
from hallpass.registry import Person, _iso
from hallpass.services import Service

MONITORED_KINDS = ("oidc", "game")
# Services on these adapters find their accounts by the person's IdP subject, so every bound person is a subject.
SSO_SUBJECT_ADAPTERS = ("fluxer",)
SSO_SUBJECT_KIND = "sso_subject"


@dataclass(frozen=True)
class DesiredRow:
    person: Person
    service: Service
    desired: str
    generation: int
    identities: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Report:
    observed: str
    detail: str | None
    generation: int
    reported_at: str


def _subjects(conn: sqlite3.Connection, service: Service) -> list[tuple[int, tuple[tuple[str, str], ...]]]:
    """(person id, identities) for each subject of the service, in person id order."""
    identity_rows = conn.execute(
        "SELECT person_id, kind, value FROM external_identity WHERE service_slug = ? ORDER BY id",
        (service.slug,),
    ).fetchall()
    by_person: dict[int, list[tuple[str, str]]] = {}
    for person_id, kind, value in identity_rows:
        by_person.setdefault(person_id, []).append((kind, value))
    if service.adapter not in SSO_SUBJECT_ADAPTERS:
        return [(person_id, tuple(found)) for person_id, found in sorted(by_person.items())]
    bound = conn.execute("SELECT id, authentik_uuid FROM person WHERE authentik_uuid IS NOT NULL ORDER BY id").fetchall()
    return [(person_id, ((SSO_SUBJECT_KIND, uuid),) + tuple(by_person.get(person_id, [])))
            for person_id, uuid in bound]


def _store_generation(conn: sqlite3.Connection, *, person_id: int, service_id: int, value: str, now: datetime) -> int:
    row = conn.execute("SELECT desired, generation FROM desired_state WHERE person_id = ? AND service_id = ?",
                       (person_id, service_id)).fetchone()
    stamp = _iso(now)
    if row is None:
        conn.execute("INSERT INTO desired_state (person_id, service_id, desired, generation, changed_at) "
                     "VALUES (?, ?, ?, 1, ?)", (person_id, service_id, value, stamp))
        return 1
    stored_value, generation = row
    if stored_value == value:
        return generation
    conn.execute("UPDATE desired_state SET desired = ?, generation = ?, changed_at = ? "
                 "WHERE person_id = ? AND service_id = ?", (value, generation + 1, stamp, person_id, service_id))
    return generation + 1


def monitored_services(conn: sqlite3.Connection) -> list[Service]:
    """The non-retired services hallmonitor reconciles, ordered by slug."""
    return sorted((service for service in services.list_services(conn) if service.kind in MONITORED_KINDS),
                  key=lambda service: service.slug)


def refresh(conn: sqlite3.Connection, now: datetime) -> list[DesiredRow]:
    """Recomputes and stores the desired state of every subject of every monitored service. Returns the rows ordered by
    service slug, then person id."""
    rows: list[DesiredRow] = []
    for service in monitored_services(conn):
        for person_id, identities in _subjects(conn, service):
            person = registry.get_person(conn, person_id)
            assert person is not None
            value = access.evaluate(conn, person, service).desired
            generation = _store_generation(conn, person_id=person_id, service_id=service.id, value=value, now=now)
            rows.append(DesiredRow(person=person, service=service, desired=value, generation=generation,
                                   identities=identities))
    return rows


def desired_for(conn: sqlite3.Connection, person_id: int, service_id: int) -> tuple[str, int] | None:
    row = conn.execute("SELECT desired, generation FROM desired_state WHERE person_id = ? AND service_id = ?",
                       (person_id, service_id)).fetchone()
    return None if row is None else (row[0], row[1])


def report_for(conn: sqlite3.Connection, person_id: int, service_id: int) -> Report | None:
    row = conn.execute("SELECT observed, detail, generation, reported_at FROM service_report "
                       "WHERE person_id = ? AND service_id = ?", (person_id, service_id)).fetchone()
    return None if row is None else Report(observed=row[0], detail=row[1], generation=row[2], reported_at=row[3])


def report_status(desired_row: tuple[str, int] | None, found: Report | None, manual_mark: bool) -> str | None:
    """The status text a row shows next to its access status. The first rule that applies wins. None for a service that
    hallmonitor does not monitor, which is one with no desired row."""
    if desired_row is None:
        return None
    desired_value, desired_generation = desired_row
    if found is None:
        return text.NOT_YET_CONFIRMED
    if found.observed == "banned_external":
        return text.BANNED_IN_GAME
    if found.observed == "unsupported":
        return text.MANUALLY_SUSPENDED if manual_mark else text.CANNOT_AUTO_SUSPEND
    if found.observed == "error":
        return text.SERVICE_ERROR
    if found.generation < desired_generation or found.observed != desired_value:
        return text.NOT_YET_CONFIRMED
    if found.detail == "pending_restart":
        return text.PENDING_RESTART
    return text.STATUS_ALLOWED if desired_value == "allowed" else text.STATUS_SUSPENDED
