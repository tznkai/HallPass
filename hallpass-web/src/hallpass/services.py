# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The service catalogue and its entitlement rows: which services the Console gates, and who may use each.

Plain functions over a sqlite3 connection. Callers run them inside `Database.run`. Each mutation writes its own
`decision_log` row, so it takes the acting admin's person id as `actor_id`. Nothing here calls log_event.
A service's slug is fixed at creation: forward-auth config, external identities and HallMonitor settings all key on it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from hallpass import registry, text
from hallpass.registry import NoSuchGroup, NoSuchPerson, _iso, log_decision

SERVICE_KINDS = ("oidc", "forward_auth", "game", "feed")
SUBJECT_KINDS = ("type", "group", "person")
EFFECTS = ("grant", "deny")
DEFAULT_ADAPTER = "manual"
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SLUG_MAX_LENGTH = 64
_DIGITS = re.compile(r"[0-9]+")


class NoSuchService(LookupError):
    """A service id that does not exist."""


class EntitlementRefused(Exception):
    """A refused entitlement. `str(exc)` is a text constant that is safe to show the admin."""


class ServiceRefused(ValueError):
    """A refused service value. `str(exc)` is a text constant that is safe to show the admin."""


@dataclass(frozen=True)
class Service:
    id: int
    slug: str
    name: str
    kind: str
    adapter: str
    auto_suspendable: bool
    url: str | None
    retired: bool
    created_at: str


@dataclass(frozen=True)
class Entitlement:
    id: int
    service_id: int
    subject_kind: str
    subject: str
    effect: str


_SERVICE_COLUMNS = "id, slug, name, kind, adapter, auto_suspendable, url, retired, created_at"
_ENTITLEMENT_COLUMNS = "id, service_id, subject_kind, subject, effect"


def _service(row: tuple) -> Service:
    return Service(id=row[0], slug=row[1], name=row[2], kind=row[3], adapter=row[4],
                   auto_suspendable=bool(row[5]), url=row[6], retired=bool(row[7]), created_at=row[8])


def is_valid_slug(slug: str) -> bool:
    """A slug is hyphen-separated runs of lower-case letters and digits, up to SLUG_MAX_LENGTH characters. Single
    hyphens only: HallMonitor reads `HM_SERVICE_<SLUG>__<KEY>` at its first `__`, so a doubled or trailing hyphen
    would give a variable that splits to a different slug."""
    return len(slug) <= SLUG_MAX_LENGTH and _SLUG.fullmatch(slug) is not None


def _checked_slug(slug: str) -> str:
    clean = (slug or "").strip()
    if not is_valid_slug(clean):
        raise ServiceRefused(text.SERVICE_SLUG_INVALID)
    return clean


def _checked_adapter(adapter: str, auto_suspendable: bool) -> str:
    """The stored adapter. A service that the Console can suspend automatically needs an adapter that HallMonitor runs,
    and the `manual` adapter is the one that never does, so that pair is refused."""
    clean = _clean_adapter(adapter)
    if auto_suspendable and clean == "manual":
        raise ServiceRefused(text.SERVICE_SUSPEND_NEEDS_ADAPTER)
    return clean


def _clean_url(url: str | None) -> str | None:
    clean = (url or "").strip()
    return clean or None


def _clean_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        raise ValueError("service name is required")
    return clean


def _clean_adapter(adapter: str) -> str:
    return (adapter or "").strip() or DEFAULT_ADAPTER


# Catalogue --------------------------------------------------------------------------------------------

def create_service(conn: sqlite3.Connection, *, slug: str, name: str, kind: str, adapter: str,
                   auto_suspendable: bool, url: str | None, actor_id: int, now: datetime) -> Service:
    clean_slug = _checked_slug(slug)
    clean_name = _clean_name(name)
    if kind not in SERVICE_KINDS:
        raise ValueError("unknown service kind")
    cursor = conn.execute(
        "INSERT INTO service (slug, name, kind, adapter, auto_suspendable, url, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (clean_slug, clean_name, kind, _checked_adapter(adapter, auto_suspendable), int(auto_suspendable),
         _clean_url(url), _iso(now)),
    )
    log_decision(conn, at=now, actor_kind="admin", actor_person_id=actor_id, target_person_id=None,
                 action="service_created", services=[clean_slug], detail={"kind": kind})
    return _required(conn, cursor.lastrowid)


def update_service(conn: sqlite3.Connection, service_id: int, *, name: str, adapter: str, auto_suspendable: bool,
                   url: str | None, actor_id: int, now: datetime) -> Service:
    """Edits the name, adapter, suspend flag and URL. The slug and kind are not editable."""
    current = _required(conn, service_id)
    conn.execute(
        "UPDATE service SET name = ?, adapter = ?, auto_suspendable = ?, url = ? WHERE id = ?",
        (_clean_name(name), _checked_adapter(adapter, auto_suspendable), int(auto_suspendable), _clean_url(url),
         service_id),
    )
    log_decision(conn, at=now, actor_kind="admin", actor_person_id=actor_id, target_person_id=None,
                 action="service_updated", services=[current.slug])
    return _required(conn, service_id)


def retire_service(conn: sqlite3.Connection, service_id: int, *, actor_id: int, now: datetime) -> Service:
    return _set_retired(conn, service_id, True, actor_id=actor_id, now=now)


def unretire_service(conn: sqlite3.Connection, service_id: int, *, actor_id: int, now: datetime) -> Service:
    return _set_retired(conn, service_id, False, actor_id=actor_id, now=now)


def _set_retired(conn: sqlite3.Connection, service_id: int, retired: bool, *, actor_id: int,
                 now: datetime) -> Service:
    current = _required(conn, service_id)
    if current.retired == retired:
        return current
    conn.execute("UPDATE service SET retired = ? WHERE id = ?", (int(retired), service_id))
    log_decision(conn, at=now, actor_kind="admin", actor_person_id=actor_id, target_person_id=None,
                 action="service_retired" if retired else "service_unretired", services=[current.slug])
    return _required(conn, service_id)


def _one(conn: sqlite3.Connection, where: str, params: tuple) -> Service | None:
    row = conn.execute(f"SELECT {_SERVICE_COLUMNS} FROM service WHERE {where}", params).fetchone()
    return None if row is None else _service(row)


def _required(conn: sqlite3.Connection, service_id: int) -> Service:
    service = get(conn, service_id)
    if service is None:
        raise NoSuchService()
    return service


def get(conn: sqlite3.Connection, service_id: int) -> Service | None:
    return _one(conn, "id = ?", (service_id,))


def get_by_slug(conn: sqlite3.Connection, slug: str) -> Service | None:
    return _one(conn, "slug = ?", (slug,))


def list_services(conn: sqlite3.Connection, include_retired: bool = False) -> list[Service]:
    where = "" if include_retired else " WHERE retired = 0"
    rows = conn.execute(f"SELECT {_SERVICE_COLUMNS} FROM service{where} ORDER BY name, id").fetchall()
    return [_service(row) for row in rows]


# Entitlements -----------------------------------------------------------------------------------------

def _subject_for(conn: sqlite3.Connection, subject_kind: str, subject: str) -> str:
    """Checks the subject exists and returns the stored form: the account type, or the numeric id as text."""
    clean = str(subject).strip()
    if subject_kind == "type":
        if clean not in registry.ACCOUNT_TYPES:
            raise ValueError("unknown account type")
        return clean
    if not _DIGITS.fullmatch(clean):
        raise NoSuchPerson() if subject_kind == "person" else NoSuchGroup()
    subject_id = int(clean)
    if subject_kind == "person":
        if registry.get_person(conn, subject_id) is None:
            raise NoSuchPerson()
    elif conn.execute("SELECT 1 FROM grp WHERE id = ?", (subject_id,)).fetchone() is None:
        raise NoSuchGroup()
    return str(subject_id)


def add_entitlement(conn: sqlite3.Connection, service_id: int, subject_kind: str, subject: str, effect: str, *,
                    actor_id: int, now: datetime) -> Entitlement:
    service = _required(conn, service_id)
    if subject_kind not in SUBJECT_KINDS:
        raise ValueError("unknown subject kind")
    if effect not in EFFECTS:
        raise ValueError("unknown effect")
    if effect == "deny" and subject_kind != "person":
        raise EntitlementRefused(text.ENTITLEMENT_DENY_NOT_PERSON)
    stored = _subject_for(conn, subject_kind, subject)
    cursor = conn.execute(
        "INSERT INTO entitlement (service_id, subject_kind, subject, effect) VALUES (?, ?, ?, ?)",
        (service_id, subject_kind, stored, effect),
    )
    log_decision(conn, at=now, actor_kind="admin", actor_person_id=actor_id,
                 target_person_id=_target(subject_kind, stored), action="entitlement_added",
                 services=[service.slug], detail={"subject_kind": subject_kind, "effect": effect})
    return _entitlement(conn, cursor.lastrowid)


def remove_entitlement(conn: sqlite3.Connection, entitlement_id: int, *, actor_id: int, now: datetime) -> bool:
    """Removes the entitlement. Returns False, logging nothing, when there is no such entitlement."""
    row = conn.execute(
        "SELECT e.id, e.service_id, e.subject_kind, e.subject, e.effect, s.slug FROM entitlement e "
        "JOIN service s ON s.id = e.service_id WHERE e.id = ?",
        (entitlement_id,),
    ).fetchone()
    if row is None:
        return False
    entitlement = Entitlement(*row[:5])
    conn.execute("DELETE FROM entitlement WHERE id = ?", (entitlement_id,))
    log_decision(conn, at=now, actor_kind="admin", actor_person_id=actor_id,
                 target_person_id=_target(entitlement.subject_kind, entitlement.subject),
                 action="entitlement_removed", services=[row[5]],
                 detail={"subject_kind": entitlement.subject_kind, "effect": entitlement.effect})
    return True


def entitlement_service_id(conn: sqlite3.Connection, entitlement_id: int) -> int | None:
    """The service an entitlement belongs to, or None when there is no such entitlement."""
    row = conn.execute("SELECT service_id FROM entitlement WHERE id = ?", (entitlement_id,)).fetchone()
    return None if row is None else row[0]


def entitlements_for(conn: sqlite3.Connection, service_id: int) -> list[Entitlement]:
    rows = conn.execute(
        f"SELECT {_ENTITLEMENT_COLUMNS} FROM entitlement WHERE service_id = ? ORDER BY id", (service_id,),
    ).fetchall()
    return [Entitlement(*row) for row in rows]


def _entitlement(conn: sqlite3.Connection, entitlement_id: int) -> Entitlement:
    row = conn.execute(f"SELECT {_ENTITLEMENT_COLUMNS} FROM entitlement WHERE id = ?", (entitlement_id,)).fetchone()
    assert row is not None
    return Entitlement(*row)


def _target(subject_kind: str, subject: str) -> int | None:
    """A person-level row is about that person, so it appears in their decision history."""
    return int(subject) if subject_kind == "person" else None
