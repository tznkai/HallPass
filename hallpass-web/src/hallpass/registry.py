# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry store: people, parent links, groups, external identities and the decision log.

Plain functions over a sqlite3 connection. Callers run them inside `Database.run`, so each call is one transaction.
Nothing here logs; the web layer logs through `log_event` and writes the decision log for each admin mutation.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime

from hallpass import display_names, text

ACCOUNT_TYPES = ("adult", "parent", "child")
IDENTITY_KINDS = ("fluxer_user_id", "java_name", "bedrock_gamertag", "steam_id")
CASE_FOLDED_KINDS = ("java_name", "bedrock_gamertag")
_DIGITS = re.compile(r"[0-9]+")
# The SteamID64 base: universe public (1 << 56), account type individual (1 << 52), instance 1 (1 << 32). The individual
# account range is the base plus a 32-bit account number, so its limit is exclusive. Computed, never written out.
STEAM64_BASE = (1 << 56) | (1 << 52) | (1 << 32)
STEAM64_LIMIT = STEAM64_BASE + (1 << 32)
MAX_STEAM64_DIGITS = 20  # a 64-bit integer has at most 20 decimal digits
# A Minecraft name reaches a console command in hallmonitor, so it must match in full. A selector such as @a is refused.
# A Bedrock gamertag uses the Java class for now: spaces stay refused until the live check shows how Floodgate passes them.
JAVA_NAME = re.compile(r"[A-Za-z0-9_]{1,16}")
BEDROCK_GAMERTAG = re.compile(r"[A-Za-z0-9_]{1,16}")


class LinkRefused(Exception):
    """A refused link or type change. `str(exc)` is a text constant that is safe to show the admin."""


class NoSuchPerson(LookupError):
    """A person id that does not exist."""


class NoSuchGroup(LookupError):
    """A group id that does not exist."""


@dataclass(frozen=True)
class Person:
    id: int
    ref: str
    display_name: str
    account_type: str
    status: str
    prior_status: str | None
    authentik_uuid: str | None
    authentik_pk: int | None
    username: str | None
    email: str | None = field(repr=False)
    phone: str | None = field(repr=False)
    fluxer_email_override: str | None
    enrollment_ref: str | None = field(repr=False)
    invitation_pk: str | None
    invite_url: str | None = field(repr=False)
    conflict_mode: str
    agreement_note: str | None
    created_at: str


@dataclass(frozen=True)
class Group:
    id: int
    name: str


@dataclass(frozen=True)
class ExternalIdentity:
    id: int
    person_id: int
    service_slug: str
    kind: str
    value: str


_PERSON_COLUMNS = tuple(f.name for f in fields(Person))
_PERSON_SELECT = ", ".join(_PERSON_COLUMNS)
_PERSON_SELECT_P = ", ".join(f"p.{name}" for name in _PERSON_COLUMNS)


def _person(row: tuple) -> Person:
    return Person(**dict(zip(_PERSON_COLUMNS, row, strict=True)))


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require_type(account_type: str) -> None:
    if account_type not in ACCOUNT_TYPES:
        raise ValueError("unknown account type")


def _display_name(raw: str | None) -> str:
    """A display name is one line of plain text, because it is written into email subjects. Any control character (CR,
    LF, TAB and the rest of Unicode category Cc) or line or paragraph separator (Zl, Zp) is refused, as is a blank name."""
    value = raw or ""
    if any(display_names.is_unsafe(ch) for ch in value):
        raise ValueError("display name has a control character")
    name = value.strip()
    if not name:
        raise ValueError("display name is required")
    return name


# People ------------------------------------------------------------------------------------------------

def create_person(conn: sqlite3.Connection, *, display_name: str, account_type: str,
                  email: str | None = None, phone: str | None = None, now: datetime) -> Person:
    _require_type(account_type)
    name = _display_name(display_name)
    cursor = conn.execute(
        "INSERT INTO person (ref, display_name, account_type, email, phone, enrollment_ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (secrets.token_urlsafe(16), name, account_type, _clean(email), _clean(phone),
         secrets.token_urlsafe(24), _iso(now)),
    )
    person = get_person(conn, cursor.lastrowid)
    assert person is not None
    return person


def _has_children(conn: sqlite3.Connection, parent_id: int) -> bool:
    return conn.execute("SELECT 1 FROM parent_link WHERE parent_id = ? LIMIT 1", (parent_id,)).fetchone() is not None


def _has_parents(conn: sqlite3.Connection, child_id: int) -> bool:
    return conn.execute("SELECT 1 FROM parent_link WHERE child_id = ? LIMIT 1", (child_id,)).fetchone() is not None


def update_person(conn: sqlite3.Connection, person_id: int, *, display_name: str, account_type: str,
                  email: str | None, phone: str | None, fluxer_email_override: str | None) -> Person:
    current = get_person(conn, person_id)
    if current is None:
        raise NoSuchPerson()
    _require_type(account_type)
    name = _display_name(display_name)
    if account_type == "child" and _has_children(conn, person_id):
        raise LinkRefused(text.TYPE_CHANGE_REFUSED_IS_PARENT)
    if current.account_type == "child" and account_type != "child" and _has_parents(conn, person_id):
        raise LinkRefused(text.TYPE_CHANGE_REFUSED_HAS_PARENTS)
    conn.execute(
        "UPDATE person SET display_name = ?, account_type = ?, email = ?, phone = ?, fluxer_email_override = ? "
        "WHERE id = ?",
        (name, account_type, _clean(email), _clean(phone), _clean(fluxer_email_override), person_id),
    )
    person = get_person(conn, person_id)
    assert person is not None
    return person


def _one(conn: sqlite3.Connection, where: str, params: tuple) -> Person | None:
    row = conn.execute(f"SELECT {_PERSON_SELECT} FROM person WHERE {where}", params).fetchone()
    return None if row is None else _person(row)


def get_person(conn: sqlite3.Connection, person_id: int) -> Person | None:
    return _one(conn, "id = ?", (person_id,))


def get_by_uuid(conn: sqlite3.Connection, uuid: str) -> Person | None:
    return _one(conn, "authentik_uuid = ?", (uuid,))


def get_by_ref(conn: sqlite3.Connection, ref: str) -> Person | None:
    """The person with this public ref, the only person id that leaves hallpass on the monitor API."""
    return _one(conn, "ref = ?", (ref,))


def get_by_enrollment_ref(conn: sqlite3.Connection, ref: str) -> Person | None:
    """The unbound person that holds this enrollment ref. A bound person never matches, even with a stale ref."""
    return _one(conn, "enrollment_ref = ? AND authentik_uuid IS NULL", (ref,))


def list_people(conn: sqlite3.Connection) -> list[Person]:
    rows = conn.execute(f"SELECT {_PERSON_SELECT} FROM person ORDER BY id").fetchall()
    return [_person(row) for row in rows]


def bind_identity(conn: sqlite3.Connection, person_id: int, *, uuid: str, authentik_pk: int, username: str) -> None:
    """Bind the person to the IdP user. Clears the enrollment ref, so a second account carrying it matches nothing."""
    cursor = conn.execute(
        "UPDATE person SET authentik_uuid = ?, authentik_pk = ?, username = ?, "
        "enrollment_ref = NULL, invite_url = NULL, invitation_pk = NULL WHERE id = ?",
        (uuid, authentik_pk, username, person_id),
    )
    if cursor.rowcount != 1:
        raise NoSuchPerson()


def set_invitation(conn: sqlite3.Connection, person_id: int, *, invitation_pk: str, invite_url: str,
                   enrollment_ref: str) -> bool:
    """Stores a live invitation on an unbound person. Returns False, storing nothing, when the person is bound or
    missing: an invitation made while the person was binding must not replace the bound identity's fields."""
    cursor = conn.execute(
        "UPDATE person SET invitation_pk = ?, invite_url = ?, enrollment_ref = ? "
        "WHERE id = ? AND authentik_uuid IS NULL",
        (invitation_pk, invite_url, enrollment_ref, person_id),
    )
    return cursor.rowcount == 1


def set_locked(conn: sqlite3.Connection, person_id: int, locked: bool) -> Person:
    """Lock stores the status it replaces in prior_status; unlock restores it. Locking twice keeps the first prior."""
    current = get_person(conn, person_id)
    if current is None:
        raise NoSuchPerson()
    if locked and current.status != "locked":
        conn.execute("UPDATE person SET prior_status = ?, status = 'locked' WHERE id = ?",
                     (current.status, person_id))
    elif not locked and current.status == "locked":
        conn.execute("UPDATE person SET status = ?, prior_status = NULL WHERE id = ?",
                     (current.prior_status or "invited", person_id))
    person = get_person(conn, person_id)
    assert person is not None
    return person


# Parent links -----------------------------------------------------------------------------------------

def link_parent(conn: sqlite3.Connection, child_id: int, parent_id: int, now: datetime) -> None:
    child = get_person(conn, child_id)
    parent = get_person(conn, parent_id)
    if child is None or parent is None:
        raise NoSuchPerson()
    if child.account_type != "child":
        raise LinkRefused(text.LINK_REFUSED_CHILD_TYPE)
    if parent.account_type == "child":
        raise LinkRefused(text.LINK_REFUSED_PARENT_TYPE)
    # OR IGNORE keeps an existing link and its place in the order
    conn.execute("INSERT OR IGNORE INTO parent_link (child_id, parent_id, created_at) VALUES (?, ?, ?)",
                 (child_id, parent_id, _iso(now)))


def unlink_parent(conn: sqlite3.Connection, child_id: int, parent_id: int) -> int:
    """Removes the link. Returns the rows removed: 0 when there was no such link."""
    return conn.execute("DELETE FROM parent_link WHERE child_id = ? AND parent_id = ?",
                        (child_id, parent_id)).rowcount


def parents_of(conn: sqlite3.Connection, child_id: int) -> list[Person]:
    rows = conn.execute(
        f"SELECT {_PERSON_SELECT_P} FROM parent_link l JOIN person p ON p.id = l.parent_id "
        "WHERE l.child_id = ? ORDER BY l.rowid",
        (child_id,),
    ).fetchall()
    return [_person(row) for row in rows]


def children_of(conn: sqlite3.Connection, parent_id: int) -> list[Person]:
    rows = conn.execute(
        f"SELECT {_PERSON_SELECT_P} FROM parent_link l JOIN person p ON p.id = l.child_id "
        "WHERE l.parent_id = ? ORDER BY l.rowid",
        (parent_id,),
    ).fetchall()
    return [_person(row) for row in rows]


# Groups -----------------------------------------------------------------------------------------------

def create_group(conn: sqlite3.Connection, name: str) -> int:
    clean = (name or "").strip()
    if not clean:
        raise ValueError("group name is required")
    return conn.execute("INSERT INTO grp (name) VALUES (?)", (clean,)).lastrowid


def delete_group(conn: sqlite3.Connection, grp_id: int) -> None:
    conn.execute("DELETE FROM grp WHERE id = ?", (grp_id,))


def list_groups(conn: sqlite3.Connection) -> list[Group]:
    rows = conn.execute("SELECT id, name FROM grp ORDER BY name").fetchall()
    return [Group(id=row[0], name=row[1]) for row in rows]


def add_member(conn: sqlite3.Connection, grp_id: int, person_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO grp_member (grp_id, person_id) VALUES (?, ?)", (grp_id, person_id))


def remove_member(conn: sqlite3.Connection, grp_id: int, person_id: int) -> int:
    """Removes the membership. Returns the rows removed: 0 when the person was not a member."""
    return conn.execute("DELETE FROM grp_member WHERE grp_id = ? AND person_id = ?",
                        (grp_id, person_id)).rowcount


def members(conn: sqlite3.Connection, grp_id: int) -> list[Person]:
    rows = conn.execute(
        f"SELECT {_PERSON_SELECT_P} FROM grp_member m JOIN person p ON p.id = m.person_id "
        "WHERE m.grp_id = ? ORDER BY p.id",
        (grp_id,),
    ).fetchall()
    return [_person(row) for row in rows]


def groups_of(conn: sqlite3.Connection, person_id: int) -> list[Group]:
    rows = conn.execute(
        "SELECT g.id, g.name FROM grp_member m JOIN grp g ON g.id = m.grp_id WHERE m.person_id = ? ORDER BY g.name",
        (person_id,),
    ).fetchall()
    return [Group(id=row[0], name=row[1]) for row in rows]


# External identities ----------------------------------------------------------------------------------

def _canonical_steam64(value: str) -> bool:
    """True for a SteamID64 in the individual range, written without a leading zero. Anything else, including a value too
    long to be an id, is False and never raises."""
    if len(value) > MAX_STEAM64_DIGITS or _DIGITS.fullmatch(value) is None:
        return False
    number = int(value)
    return str(number) == value and STEAM64_BASE <= number < STEAM64_LIMIT


def identity_key(kind: str, value: str) -> str:
    """The form in which two identities of one service and kind count as the same. Minecraft names fold to lower case;
    every other kind is compared as stored. The unique index external_identity_person applies the same rule in SQL
    (migration 0007), so change both together."""
    return value.lower() if kind in CASE_FOLDED_KINDS else value


def add_identity(conn: sqlite3.Connection, person_id: int, service_slug: str, kind: str, value: str) -> int:
    if kind not in IDENTITY_KINDS:
        raise ValueError("unknown identity kind")
    slug = (service_slug or "").strip()
    clean = (value or "").strip()
    if not slug or not clean:
        raise ValueError("service and value are required")
    if conn.execute("SELECT 1 FROM service WHERE slug = ?", (slug,)).fetchone() is None:
        raise ValueError("unknown service")
    if kind == "steam_id":
        if not _DIGITS.fullmatch(clean):
            raise ValueError("steam_id must be digits")
        if not _canonical_steam64(clean):
            raise ValueError("steam_id must be a SteamID64 in the individual range, without a leading zero")
    if kind == "java_name" and not JAVA_NAME.fullmatch(clean):
        raise ValueError("java_name must be 1 to 16 letters, digits or underscores")
    if kind == "bedrock_gamertag" and not BEDROCK_GAMERTAG.fullmatch(clean):
        raise ValueError("bedrock_gamertag must be 1 to 16 letters, digits or underscores")
    return conn.execute(
        "INSERT INTO external_identity (person_id, service_slug, kind, value) VALUES (?, ?, ?, ?)",
        (person_id, slug, kind, clean),
    ).lastrowid


def remove_identity(conn: sqlite3.Connection, identity_id: int) -> None:
    conn.execute("DELETE FROM external_identity WHERE id = ?", (identity_id,))


def get_identity(conn: sqlite3.Connection, identity_id: int) -> ExternalIdentity | None:
    row = conn.execute("SELECT id, person_id, service_slug, kind, value FROM external_identity WHERE id = ?",
                       (identity_id,)).fetchone()
    return None if row is None else ExternalIdentity(*row)


def identities_of(conn: sqlite3.Connection, person_id: int) -> list[ExternalIdentity]:
    rows = conn.execute(
        "SELECT id, person_id, service_slug, kind, value FROM external_identity WHERE person_id = ? ORDER BY id",
        (person_id,),
    ).fetchall()
    return [ExternalIdentity(*row) for row in rows]


def other_holders(conn: sqlite3.Connection, identity: ExternalIdentity) -> list[Person]:
    """The other people who hold the same identity on the same service, by identity_key, in person id order."""
    key = identity_key(identity.kind, identity.value)
    rows = conn.execute(
        "SELECT person_id, value FROM external_identity WHERE service_slug = ? AND kind = ? AND person_id <> ? "
        "ORDER BY person_id, id",
        (identity.service_slug, identity.kind, identity.person_id),
    ).fetchall()
    found: list[Person] = []
    for person_id, value in rows:
        if identity_key(identity.kind, value) == key:
            person = get_person(conn, person_id)
            if person is not None and person.id not in {p.id for p in found}:
                found.append(person)
    return found


# Fluxer address ---------------------------------------------------------------------------------------

def fluxer_address(conn: sqlite3.Connection, person: Person) -> str | None:
    """The address Fluxer uses for this person: the override, then their own email, then a parent's plus-address."""
    if person.fluxer_email_override:
        return person.fluxer_email_override
    if person.email:
        return person.email
    if person.account_type == "child" and person.username:
        for parent in parents_of(conn, person.id):
            if parent.email and "@" in parent.email:
                local, _, domain = parent.email.rpartition("@")
                local = local.split("+", 1)[0]
                return f"{local}+{person.username}@{domain}"
    return None


def people_missing_fluxer_address(conn: sqlite3.Connection) -> list[Person]:
    return [p for p in list_people(conn) if p.account_type == "child" and fluxer_address(conn, p) is None]


# Decision log -----------------------------------------------------------------------------------------

def log_decision(conn: sqlite3.Connection, *, at: datetime, actor_kind: str, actor_person_id: int | None,
                 target_person_id: int | None, action: str, services: Iterable[str | int] = (),
                 detail: dict | None = None, reversal: bool = False) -> int:
    return conn.execute(
        "INSERT INTO decision_log (at, actor_kind, actor_person_id, target_person_id, action, services, detail, "
        "reversal) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_iso(at), actor_kind, actor_person_id, target_person_id, action, json.dumps(list(services)),
         json.dumps(detail or {}, sort_keys=True), int(reversal)),
    ).lastrowid
