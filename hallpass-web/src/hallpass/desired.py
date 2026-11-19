# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Desired state for HallMonitor: each monitored (service, subject) pair with its desired value and generation.

`refresh` recomputes `access.evaluate` for every subject of every monitored service and stores the result. A
shared identity (several people hold it on one service) is paused while any holder is suspended, and is then
sent only on the suspended holders' subjects (`_sent_identities`). A pair gets generation 1 the first time it is
seen, and goes up by one each time its desired value changes. A pair that stops being sent keeps its row (sent = 0),
and goes up by one when it is sent again, so a generation never goes down. A report carrying an older generation
therefore shows that the current value is not yet confirmed. `report_status` maps a report onto the text a row shows.
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
    bound = conn.execute("SELECT id, authentik_uuid FROM person WHERE authentik_uuid IS NOT NULL "
                         "ORDER BY id").fetchall()
    return [(person_id, ((SSO_SUBJECT_KIND, uuid),) + tuple(by_person.get(person_id, [])))
            for person_id, uuid in bound]


def _store_generation(conn: sqlite3.Connection, *, person_id: int, service_id: int, value: str, now: datetime) -> int:
    """Stores the pair as sent and returns its generation. A new pair starts at 1. A pair that was not sent (sent = 0,
    kept by `_prune`) goes up by one, even when its value is the same, so it starts above every generation it had
    before. A sent pair goes up by one only when its value changes."""
    row = conn.execute("SELECT desired, generation, sent FROM desired_state WHERE person_id = ? AND service_id = ?",
                       (person_id, service_id)).fetchone()
    stamp = _iso(now)
    if row is None:
        conn.execute("INSERT INTO desired_state (person_id, service_id, desired, generation, changed_at, sent) "
                     "VALUES (?, ?, ?, 1, ?, 1)", (person_id, service_id, value, stamp))
        return 1
    stored_value, generation, sent = row
    if sent and stored_value == value:
        return generation
    conn.execute("UPDATE desired_state SET desired = ?, generation = ?, changed_at = ?, sent = 1 "
                 "WHERE person_id = ? AND service_id = ?", (value, generation + 1, stamp, person_id, service_id))
    return generation + 1


def monitored_services(conn: sqlite3.Connection) -> list[Service]:
    """The non-retired services HallMonitor reconciles, ordered by slug."""
    return sorted((service for service in services.list_services(conn) if service.kind in MONITORED_KINDS),
                  key=lambda service: service.slug)


def _prune(conn: sqlite3.Connection, subjects: set[tuple[int, int]], monitored_ids: set[int]) -> None:
    """Keeps the rows that no longer belong, and deletes the reports that no longer belong. A desired row for a pair
    that is not a subject any more is marked not sent (sent = 0) and kept, so its generation stays on record for a
    return (see `_store_generation`). Its report is deleted, and `desired_for` hides the row, so a late report for it is
    refused. Unmanaged counts for services that are not monitored are deleted."""
    sent_pairs = conn.execute("SELECT person_id, service_id FROM desired_state WHERE sent = 1").fetchall()
    for person_id, service_id in sent_pairs:
        if (person_id, service_id) not in subjects:
            conn.execute("UPDATE desired_state SET sent = 0 WHERE person_id = ? AND service_id = ?",
                         (person_id, service_id))
    for person_id, service_id in conn.execute("SELECT person_id, service_id FROM service_report").fetchall():
        if (person_id, service_id) not in subjects:
            conn.execute("DELETE FROM service_report WHERE person_id = ? AND service_id = ?", (person_id, service_id))
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
    """A shared identity of an allowed person that is paused on a service. `causes` are the ids of the suspended holders
    who pause it, never empty. Ids only: the pages look up the names, and only the administrator's page shows them."""
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
    the HallMonitor record of one person's account, never shared, so it is left out."""
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
    """The identities HallMonitor is sent for a subject. A paused identity is kept only on a suspended subject, so that
    HallMonitor removes it there. An allowed subject never gets it, so HallMonitor never adds it back."""
    return tuple((kind, value) for kind, value in subject.identities
                 if kind == SSO_SUBJECT_KIND or subject.desired == "suspended"
                 or (kind, registry.identity_key(kind, value)) not in paused)


@dataclass(frozen=True)
class _ServiceState:
    """One service's subjects as the rules see them: the shared identities that are paused, and the pause of each
    allowed person who has one."""
    subjects: list[_Subject]
    paused: set[tuple[str, str]]
    pauses: dict[int, SharedPause]


def _pauses(shared: dict[tuple[str, str], list[_Subject]], paused: set[tuple[str, str]]) -> dict[int, SharedPause]:
    """The pause of each allowed holder of a paused shared identity, with the suspended holders that cause it. A
    suspended holder has no pause: its identity is kept on its own subject, so its own suspension is the reason."""
    causes: dict[int, set[int]] = {}
    for key in paused:
        holders = shared[key]
        suspended = {holder.person.id for holder in holders if holder.desired == "suspended"}
        for holder in holders:
            if holder.desired != "suspended":
                causes.setdefault(holder.person.id, set()).update(suspended)
    return {person_id: SharedPause(causes=tuple(sorted(ids))) for person_id, ids in causes.items()}


def _service_state(conn: sqlite3.Connection, service: Service) -> _ServiceState:
    """Evaluates every subject of the service once. `refresh_with_pauses` is the only caller: it reads the sent
    identities and the pauses from this, so no page evaluates a service on its own."""
    subjects = _evaluated_subjects(conn, service)
    shared = _shared_holders(subjects)
    paused = _paused_keys(shared)
    return _ServiceState(subjects=subjects, paused=paused, pauses=_pauses(shared, paused))


PairPauses = dict[tuple[int, int], SharedPause]


def refresh_with_pauses(conn: sqlite3.Connection, now: datetime) -> tuple[list[DesiredRow], PairPauses]:
    """Recomputes and stores the desired state of every subject of every monitored service, then prunes the rows that
    no longer belong (see `_prune`). Returns the rows ordered by service slug, then person id, and the shared pauses
    keyed by (service id, person id), so a page can show them without evaluating the services again.

    A subject whose identities are all paused (see `_sent_identities`) is not a subject for this round: HallMonitor is
    not sent it, so it gets no report, and its desired row is marked not sent. A later restore sends it again above its
    old generation.

    Cost: every subject of every monitored service is evaluated once on each call, so the work grows with services times
    people. That is fine at neighbourhood scale. It could later be limited to the person being viewed. Callers run it
    inside one transaction, so the stored state and the pruning always agree.
    """
    rows: list[DesiredRow] = []
    subjects: set[tuple[int, int]] = set()
    pauses: PairPauses = {}
    monitored = monitored_services(conn)
    for service in monitored:
        state = _service_state(conn, service)
        for subject in state.subjects:
            if subject.person.id in state.pauses:
                pauses[(service.id, subject.person.id)] = state.pauses[subject.person.id]
            identities = _sent_identities(subject, state.paused)
            if not identities:
                continue
            generation = _store_generation(conn, person_id=subject.person.id, service_id=service.id,
                                           value=subject.desired, now=now)
            subjects.add((subject.person.id, service.id))
            rows.append(DesiredRow(person=subject.person, service=service, desired=subject.desired,
                                   generation=generation, identities=identities))
    _prune(conn, subjects, {service.id for service in monitored})
    return rows, pauses


def refresh(conn: sqlite3.Connection, now: datetime) -> list[DesiredRow]:
    """`refresh_with_pauses` for callers that need only the rows."""
    rows, _ = refresh_with_pauses(conn, now)
    return rows


def desired_for(conn: sqlite3.Connection, person_id: int, service_id: int) -> tuple[str, int] | None:
    """The stored desired value and generation of a pair that is sent now, or None. A pair that is not sent (sent = 0)
    reads as None, so no page shows it as monitored and no report is accepted for it."""
    row = conn.execute("SELECT desired, generation FROM desired_state "
                       "WHERE person_id = ? AND service_id = ? AND sent = 1", (person_id, service_id)).fetchone()
    return None if row is None else (row[0], row[1])


def report_for(conn: sqlite3.Connection, person_id: int, service_id: int) -> Report | None:
    row = conn.execute("SELECT observed, detail, generation, reported_at FROM service_report "
                       "WHERE person_id = ? AND service_id = ?", (person_id, service_id)).fetchone()
    return None if row is None else Report(observed=row[0], detail=row[1], generation=row[2], reported_at=row[3])


def report_status(desired_row: tuple[str, int] | None, found: Report | None, manual_mark: bool) -> str | None:
    """The status text a row shows next to its access status. The first rule that applies wins. None for a service that
    HallMonitor does not monitor, which is one with no desired row."""
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
        # no_adapter means HallMonitor has no settings for the service, not that a call to it failed.
        return text.NOT_CONFIGURED if found.detail == "no_adapter" else text.SERVICE_ERROR
    if found.generation < desired_generation or found.observed != desired_value:
        return text.NOT_YET_CONFIRMED
    if found.detail == "pending_restart":
        return text.PENDING_RESTART
    return text.STATUS_ALLOWED if desired_value == "allowed" else text.STATUS_SUSPENDED


def monitor_health(conn: sqlite3.Connection, now: datetime) -> dict:
    """What the administrator's monitor page shows: the last contact from HallMonitor, the services with a report in
    error (with how many people), and the unmanaged entries per service. It refreshes first, so the page never counts
    a pair that is no longer a subject. The counts also join on the live pairs and on the monitored services, so a
    stale report cannot be counted even if a refresh did not prune it."""
    refresh(conn, now)
    kinds = ",".join("?" * len(MONITORED_KINDS))
    contact = conn.execute("SELECT last_contact_at FROM monitor_contact WHERE id = 1").fetchone()
    errors = conn.execute(
        "SELECT s.name, COUNT(*) FROM service_report r JOIN service s ON s.id = r.service_id "
        "JOIN desired_state d ON d.person_id = r.person_id AND d.service_id = r.service_id AND d.sent = 1 "
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
