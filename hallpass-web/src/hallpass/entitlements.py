# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entitlement resolution: whether a person may use a service, from the service's entitlement rows.

`resolve` is pure. The rule, in order: a person-level row decides when present; otherwise a group grant for a group
the person belongs to entitles them; otherwise a type grant for their account type does; otherwise they are not
entitled. Only person rows can deny, and the store refuses a deny on any other row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from hallpass import registry, services
from hallpass.registry import Person
from hallpass.services import Service


@dataclass(frozen=True)
class Row:
    subject_kind: str
    subject: str
    effect: str


def resolve(rows: Iterable[Row], *, account_type: str, group_ids: frozenset[int], person_id: int) -> bool:
    rows = tuple(rows)
    person_key = str(person_id)
    for row in rows:
        if row.subject_kind == "person" and row.subject == person_key:
            return row.effect == "grant"
    group_keys = {str(group_id) for group_id in group_ids}
    for row in rows:
        if row.subject_kind == "group" and row.effect == "grant" and row.subject in group_keys:
            return True
    return any(row.subject_kind == "type" and row.effect == "grant" and row.subject == account_type
               for row in rows)


def is_entitled(conn: sqlite3.Connection, person: Person, service: Service) -> bool:
    rows = [Row(e.subject_kind, e.subject, e.effect) for e in services.entitlements_for(conn, service.id)]
    group_ids = frozenset(group.id for group in registry.groups_of(conn, person.id))
    return resolve(rows, account_type=person.account_type, group_ids=group_ids, person_id=person.id)
