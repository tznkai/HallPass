# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Desired state for hallmonitor: each monitored (service, subject) pair with its desired value and generation.

`refresh` recomputes `access.evaluate` for every subject of every monitored service and stores the result. A
shared identity (several people hold it on one service) is paused while any holder is suspended, and is then
sent only on the suspended holders' subjects (`_sent_identities`). A pair gets generation 1 the first time it is
seen, and goes up by one each time its desired value changes. A report carrying an older generation therefore
shows that the current value is not yet confirmed. `report_status` maps a report onto the text a row shows.
Plain functions over a sqlite3 connection; callers run them inside `Database.run`. Nothing here logs.
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


_PRUNED_TABLES = ("desired_state", "service_report")


def _prune(conn: sqlite3.Connection, subjects: set[tuple[int, int]], monitored_ids: set[int]) -> None:
    """Deletes the rows that no longer belong: desired and report rows for (person, service) pairs that are not
    subjects any more, and unmanaged counts for services that are not monitored. A pair that returns starts again at
    generation 1 with no report, because its old report was deleted with it."""
    for table in _PRUNED_TABLES:
        for person_id, service_id in conn.execute(f"SELECT person_id, service_id FROM {table}").fetchall():
            if (person_id, service_id) not in subjects:
                conn.execute(f"DELETE FROM {table} WHERE person_id = ? AND service_id = ?", (person_id, service_id))
    for (service_id,) in conn.execute("SELECT service_id FROM service_unmanaged").fetchall():
        if service_id not in monitored_ids:
            conn.execute("DELETE FROM service_unmanaged WHERE service_id = ?", (service_id,))


@dataclass(frozen=True)
class _Subject:
    person: Person
    identities: tuple[tuple[str, str], ...]
    desired: str


@dataclass(frozen=True)
class SharedPause:
    """A paused shared identity of one person on one service. `causes` are the ids of the other holders who are
    suspended, and so pause it. It is empty when this person's own suspension is the only cause. Ids only: the pages
    look up the names, and only the administrator's page shows them."""
    causes: tuple[int, ...]


def _evaluated_subjects(conn: sqlite3.Connection, service: Service) -> list[_Subject]:
    found: list[_Subject] = []
    for person_id, identities in _subjects(conn, service):
        person = registry.get_person(conn, person_id)
        assert person is not None
        found.append(_Subject(person=person, identities=identities,
                              desired=access.evaluate(conn, person, service).desired))
    return found


def _shared_holders(subjects: list[_Subject]) -> dict[tuple[str, str], list[_Subject]]:
    """Each identity that two or more subjects hold on the service, keyed by (kind, identity_key). The SSO subject is
    the hallmonitor record of one person's account, never shared, so it is left out."""
    holders: dict[tuple[str, str], list[_Subject]] = {}
    for subject in subjects:
        for kind, value in subject.identities:
            if kind != SSO_SUBJECT_KIND:
                holders.setdefault((kind, registry.identity_key(kind, value)), []).append(subject)
    return {key: found for key, found in holders.items() if len(found) > 1}


def _paused_keys(shared: dict[tuple[str, str], list[_Subject]]) -> set[tuple[str, str]]:
    """A shared identity is paused while any of its holders is suspended, for any reason."""
    return {key for key, found in shared.items() if any(s.desired == "suspended" for s in found)}


def _sent_identities(subject: _Subject, paused: set[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """The identities hallmonitor is sent for a subject. A paused identity is kept only on a suspended subject, so that
    hallmonitor removes it there. An allowed subject never gets it, so hallmonitor never adds it back."""
    return tuple((kind, value) for kind, value in subject.identities
                 if kind == SSO_SUBJECT_KIND or subject.desired == "suspended"
                 or (kind, registry.identity_key(kind, value)) not in paused)


def refresh(conn: sqlite3.Connection, now: datetime) -> list[DesiredRow]:
    """Recomputes and stores the desired state of every subject of every monitored service, then prunes the rows that
    no longer belong (see `_prune`). Returns the rows ordered by service slug, then person id.

    A subject whose identities are all paused (see `_sent_identities`) is not a subject for this round: hallmonitor is
    not sent it, so it gets no desired row and no report, and a later restore starts it again at generation 1.

    Cost: every subject of every monitored service is recomputed on each call, so the work grows with services times
    people. That is fine at neighbourhood scale. It could later be limited to the person being viewed. Callers run it
    inside one transaction, so the stored state and the pruning always agree.
    """
    rows: list[DesiredRow] = []
    subjects: set[tuple[int, int]] = set()
    monitored = monitored_services(conn)
    for service in monitored:
        candidates = _evaluated_subjects(conn, service)
        paused = _paused_keys(_shared_holders(candidates))
        for subject in candidates:
            identities = _sent_identities(subject, paused)
            if not identities:
                continue
            generation = _store_generation(conn, person_id=subject.person.id, service_id=service.id,
                                           value=subject.desired, now=now)
            subjects.add((subject.person.id, service.id))
            rows.append(DesiredRow(person=subject.person, service=service, desired=subject.desired,
                                   generation=generation, identities=identities))
    _prune(conn, subjects, {service.id for service in monitored})
    return rows


def shared_pause(conn: sqlite3.Connection, person: Person, service: Service) -> SharedPause | None:
    """The shared identities of this person that are paused on the service, or None when none is. Only monitored
    services can pause, because hallmonitor is the one that removes an identity. Callers may run it without
    `refresh`."""
    if service.retired or service.kind not in MONITORED_KINDS:
        return None
    shared = _shared_holders(_evaluated_subjects(conn, service))
    paused = _paused_keys(shared)
    mine = [key for key in paused if any(s.person.id == person.id for s in shared[key])]
    if not mine:
        return None
    causes = {s.person.id for key in mine for s in shared[key]
              if s.desired == "suspended" and s.person.id != person.id}
    return SharedPause(causes=tuple(sorted(causes)))


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
        # no_adapter means hallmonitor has no settings for the service, not that a call to it failed.
        return text.NOT_CONFIGURED if found.detail == "no_adapter" else text.SERVICE_ERROR
    if found.generation < desired_generation or found.observed != desired_value:
        return text.NOT_YET_CONFIRMED
    if found.detail == "pending_restart":
        return text.PENDING_RESTART
    return text.STATUS_ALLOWED if desired_value == "allowed" else text.STATUS_SUSPENDED


def monitor_health(conn: sqlite3.Connection, now: datetime) -> dict:
    """What the administrator's monitor page shows: the last contact from hallmonitor, the services with a report in
    error (with how many people), and the unmanaged entries per service. It refreshes first, so the page never counts
    a pair that is no longer a subject. The counts also join on the live pairs and on the monitored services, so a
    stale report cannot be counted even if a refresh did not prune it."""
    refresh(conn, now)
    kinds = ",".join("?" * len(MONITORED_KINDS))
    contact = conn.execute("SELECT last_contact_at FROM monitor_contact WHERE id = 1").fetchone()
    errors = conn.execute(
        "SELECT s.name, COUNT(*) FROM service_report r JOIN service s ON s.id = r.service_id "
        "JOIN desired_state d ON d.person_id = r.person_id AND d.service_id = r.service_id "
        f"WHERE r.observed = 'error' AND s.retired = 0 AND s.kind IN ({kinds}) "
        "GROUP BY s.id ORDER BY s.name, s.id", MONITORED_KINDS).fetchall()
    unmanaged = conn.execute(
        "SELECT s.name, u.count FROM service_unmanaged u JOIN service s ON s.id = u.service_id "
        f"WHERE s.retired = 0 AND s.kind IN ({kinds}) ORDER BY s.name, s.id", MONITORED_KINDS).fetchall()
    return {
        "last_contact_at": None if contact is None else contact[0],
        "errors": [{"name": name, "count": count} for name, count in errors],
        "unmanaged": [{"name": name, "count": count} for name, count in unmanaged],
    }
