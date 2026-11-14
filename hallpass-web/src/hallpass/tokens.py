# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feed tokens the person generates, and the invite links, per-person game credentials and API keys the admin attaches.

Plain functions over a sqlite3 connection, run inside `Database.run`. A feed token is 192 random bits, shown once and
stored only as its SHA-256 hex, so the database holds nothing that works as a feed URL. An attached token is stored as
given and is listed only to people entitled to its service (`visible_to`). `issue_feed` writes its own decision row;
`attach` and `revoke` do not, since their callers record the admin's action.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from hallpass import entitlements, services
from hallpass.registry import Person, _iso, log_decision
from hallpass.services import NoSuchService, Service

FEED_BYTES = 24   # token_urlsafe(24): 192 random bits, 32 characters
ATTACHED_KINDS = ("invite_link", "game_credential", "api_key")


@dataclass(frozen=True)
class VisibleToken:
    id: int
    service_id: int
    service_name: str
    kind: str
    label: str
    value: str = field(repr=False)


@dataclass(frozen=True)
class AttachedToken:
    """An attached token as the admin sees it. `value` stays out of repr, as in VisibleToken."""
    id: int
    service_id: int
    person_id: int | None
    kind: str
    label: str
    value: str = field(repr=False)
    revoked: bool


_ATTACHED_COLUMNS = "id, service_id, person_id, kind, label, value, revoked_at IS NOT NULL"


def _attached(row: tuple) -> AttachedToken:
    return AttachedToken(id=row[0], service_id=row[1], person_id=row[2], kind=row[3], label=row[4], value=row[5],
                         revoked=bool(row[6]))


def get_attached(conn: sqlite3.Connection, token_id: int) -> AttachedToken | None:
    """An attached token by id. Feed tokens are not attached tokens, so they are never returned here."""
    row = conn.execute(
        f"SELECT {_ATTACHED_COLUMNS} FROM token WHERE id = ? AND kind IN ('invite_link', 'game_credential', 'api_key')",
        (token_id,),
    ).fetchone()
    return None if row is None else _attached(row)


def attached_for(conn: sqlite3.Connection, service_id: int) -> list[AttachedToken]:
    """The unrevoked attached tokens of one service, for the admin's service page."""
    rows = conn.execute(
        f"SELECT {_ATTACHED_COLUMNS} FROM token WHERE service_id = ? AND revoked_at IS NULL "
        "AND kind IN ('invite_link', 'game_credential', 'api_key') ORDER BY id",
        (service_id,),
    ).fetchall()
    return [_attached(row) for row in rows]


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_feed(conn: sqlite3.Connection, *, person_id: int, service_id: int, now: datetime) -> str:
    """Generates a feed token for the person and service, revoking any they already had. Returns the raw value once."""
    service = services.get(conn, service_id)
    if service is None:
        raise NoSuchService()
    revoked = conn.execute(
        "UPDATE token SET revoked_at = ? WHERE person_id = ? AND service_id = ? AND kind = 'feed' AND revoked_at IS NULL",
        (_iso(now), person_id, service_id),
    ).rowcount
    raw = secrets.token_urlsafe(FEED_BYTES)
    conn.execute(
        "INSERT INTO token (service_id, person_id, kind, label, value, hash, created_at) "
        "VALUES (?, ?, 'feed', '', NULL, ?, ?)",
        (service_id, person_id, hash_token(raw), _iso(now)),
    )
    log_decision(conn, at=now, actor_kind="person", actor_person_id=person_id, target_person_id=person_id,
                 action="feed_token_issued", services=[service.slug], detail={"revoked": revoked})
    return raw


def feed_owner(conn: sqlite3.Connection, *, service_id: int, raw: str) -> int | None:
    """The person id an unrevoked feed token for this service belongs to, or None."""
    row = conn.execute(
        "SELECT person_id FROM token WHERE hash = ? AND service_id = ? AND kind = 'feed' AND revoked_at IS NULL",
        (hash_token(raw), service_id),
    ).fetchone()
    return None if row is None else row[0]


def attach(conn: sqlite3.Connection, *, service_id: int, person_id: int | None, kind: str, label: str, value: str,
           now: datetime) -> int:
    if kind not in ATTACHED_KINDS:
        raise ValueError("unknown token kind")
    if not (value or "").strip():
        raise ValueError("token value is required")
    cursor = conn.execute(
        "INSERT INTO token (service_id, person_id, kind, label, value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (service_id, person_id, kind, label, value, _iso(now)),
    )
    return cursor.lastrowid


def revoke(conn: sqlite3.Connection, token_id: int, now: datetime) -> None:
    conn.execute("UPDATE token SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_iso(now), token_id))


def visible_to(conn: sqlite3.Connection, person: Person) -> list[VisibleToken]:
    """Unrevoked attached tokens on live services where the person is entitled, and that are theirs or service-wide.
    Feed tokens are never listed here; the person sees their feed URL only once, when it is generated."""
    rows = conn.execute(
        "SELECT t.id, t.service_id, s.name, t.kind, t.label, t.value FROM token t "
        "JOIN service s ON s.id = t.service_id "
        "WHERE t.revoked_at IS NULL AND t.kind <> 'feed' AND s.retired = 0 "
        "AND (t.person_id = ? OR t.person_id IS NULL) ORDER BY t.id",
        (person.id,),
    ).fetchall()
    visible: list[VisibleToken] = []
    for token_id, service_id, name, kind, label, value in rows:
        service = services.get(conn, service_id)
        if service is not None and entitlements.is_entitled(conn, person, service):
            visible.append(VisibleToken(id=token_id, service_id=service_id, service_name=name, kind=kind,
                                        label=label, value=value))
    return visible


def feed_services_for(conn: sqlite3.Connection, person: Person) -> list[Service]:
    """The live feed services the person is entitled to, by name."""
    return [service for service in services.list_services(conn)
            if service.kind == "feed" and entitlements.is_entitled(conn, person, service)]
