# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The access decision: one answer per person and service, from retirement, entitlement, person status and terms.

Forward auth, the personal page and the desired-state computation for the monitor API all use `evaluate`, so they
cannot disagree. A service is suspended whenever any reason applies. The reasons come in a fixed order:
retired, not_entitled, not_active, terms, held. A hold of any kind on (person, service) gives `held`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from hallpass import entitlements, registry, services, terms
from hallpass.registry import Person
from hallpass.services import Service


@dataclass(frozen=True)
class Access:
    desired: Literal["allowed", "suspended"]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Hold:
    id: int
    person_id: int
    service_id: int
    kind: Literal["parent", "admin", "lockdown"]
    placed_by: int | None
    placed_at: str
    reason: str
    review_id: int | None


def holds_for(conn: sqlite3.Connection, person_id: int, service_id: int) -> list[Hold]:
    rows = conn.execute(
        "SELECT id, person_id, service_id, kind, placed_by, placed_at, reason, review_id FROM hold "
        "WHERE person_id = ? AND service_id = ? ORDER BY id",
        (person_id, service_id),
    ).fetchall()
    return [Hold(*row) for row in rows]


def evaluate(conn: sqlite3.Connection, person: Person, service: Service) -> Access:
    reasons: list[str] = []
    if service.retired:
        reasons.append("retired")
    if not entitlements.is_entitled(conn, person, service):
        reasons.append("not_entitled")
    if person.status != "active":
        reasons.append("not_active")
    if terms.services_blocked(terms.state_for(conn, person.id)):
        reasons.append("terms")
    if holds_for(conn, person.id, service.id):
        reasons.append("held")
    return Access(desired="suspended" if reasons else "allowed", reasons=tuple(reasons))


def services_for(conn: sqlite3.Connection, person: Person) -> list[tuple[Service, Access]]:
    """Every non-retired service the person is entitled to or has an external identity for, by name."""
    identity_slugs = {identity.service_slug for identity in registry.identities_of(conn, person.id)}
    found: list[tuple[Service, Access]] = []
    for service in services.list_services(conn):
        if service.slug in identity_slugs or entitlements.is_entitled(conn, person, service):
            found.append((service, evaluate(conn, person, service)))
    return found
