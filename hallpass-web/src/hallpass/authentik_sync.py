# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keeps Authentik in step with hallpass's decisions: each bound person's Fluxer group membership, their fluxer_email
attribute and their active flag. The web routes call it after every change, and resync_loop calls it for everyone.

A failed Authentik call is logged with its reason and left for the next resync. It never raises into the request that
made the change, because the decision in hallpass is already committed."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol

from hallpass import access, registry, services
from hallpass.authentik import AuthentikClient, AuthentikError
from hallpass.config import Config
from hallpass.db import Database
from hallpass.logs import log_event
from hallpass.registry import Person

log = logging.getLogger(__name__)
ATTRIBUTE = "fluxer_email"


class SyncDeps(Protocol):
    config: Config
    db: Database
    authentik: AuthentikClient


def wanted(conn: sqlite3.Connection, person: Person,
           fluxer_service: services.Service | None) -> tuple[bool, str | None, bool]:
    """(member, fluxer_email, active) as Authentik should hold them. A missing or retired Fluxer service
    (passed as None) means nobody is a member; the address and the active flag are still computed."""
    address = registry.fluxer_address(conn, person)
    member = (
        fluxer_service is not None
        and person.authentik_uuid is not None
        and address is not None
        and access.evaluate(conn, person, fluxer_service).desired == "allowed"
    )
    return member, address, person.status != "locked"


def _snapshot(conn: sqlite3.Connection, deps: SyncDeps, person_id: int) -> tuple[int, bool, str | None, bool] | None:
    person = registry.get_person(conn, person_id)
    if person is None or person.authentik_uuid is None or person.authentik_pk is None:
        return None
    fluxer = services.get_by_slug(conn, deps.config.fluxer_service_slug)
    if fluxer is not None and fluxer.retired:
        fluxer = None
    member, address, active = wanted(conn, person, fluxer)
    return person.authentik_pk, member, address, active


async def _remove_from_group(authentik: AuthentikClient, group: str, pk: int) -> None:
    """Not a member already (404) counts as removed."""
    try:
        await authentik.remove_from_group(group, pk)
    except AuthentikError as error:
        if error.status != 404:
            raise


async def _attempt(person_id: int, step: str, call: Callable[[], Awaitable[None]]) -> bool:
    """Runs one Authentik call. A failure is logged with the step's name and the status, and reported as False, so
    the other steps still run."""
    try:
        await call()
    except AuthentikError as error:
        log_event(log, "authentik_sync_failed", level=logging.WARNING, person_id=person_id, action=step,
                  reason=error.reason, http_status=error.status)
        return False
    return True


async def sync_person(deps: SyncDeps, person_id: int) -> bool:
    """Brings the person's Authentik state in line. Returns True when every step succeeded (or there is nothing to keep
    in step), and False after logging a failed call. Never raises for an Authentik failure.

    Each step runs even when an earlier one fails. The restrictive steps (leaving the Fluxer group, deactivating) come
    first, so a failed attribute call can never leave a hold or a lock unenforced. The permissive steps come last."""
    snapshot = await deps.db.run(lambda c: _snapshot(c, deps, person_id))
    if snapshot is None:
        return True                 # unbound or missing: nothing in Authentik to keep in step
    pk, member, address, active = snapshot
    group = deps.config.authentik_fluxer_group
    authentik = deps.authentik
    steps: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    if not member:
        steps.append(("remove_from_group", lambda: _remove_from_group(authentik, group, pk)))
    if not active:
        steps.append(("set_active", lambda: authentik.set_active(pk, False)))
    steps.append(("update_attributes", lambda: authentik.update_attributes(pk, {ATTRIBUTE: address})))
    if member:
        steps.append(("add_to_group", lambda: authentik.add_to_group(group, pk)))
    if active:
        steps.append(("set_active", lambda: authentik.set_active(pk, True)))
    results = [await _attempt(person_id, step, call) for step, call in steps]
    return all(results)


async def sync_people(deps: SyncDeps, person_ids: Iterable[int]) -> bool:
    """Syncs each person, in id order, even after a failure. Returns True only when every one of them succeeded."""
    results = [await sync_person(deps, person_id) for person_id in sorted(set(person_ids))]
    return all(results)


def bound_person_ids(conn: sqlite3.Connection) -> list[int]:
    return [row[0] for row in conn.execute(
        "SELECT id FROM person WHERE authentik_uuid IS NOT NULL ORDER BY id").fetchall()]


def affected_by(conn: sqlite3.Connection, person_id: int) -> set[int]:
    """The person and their linked children: a parent's email changes the children's plus-addresses."""
    return {person_id} | {child.id for child in registry.children_of(conn, person_id)}


async def resync_all(deps: SyncDeps) -> bool:
    return await sync_people(deps, await deps.db.run(bound_person_ids))


async def resync_loop(deps: SyncDeps) -> None:
    """Syncs every bound person each `resync_seconds`. A pass that fails is logged and retried on the next pass."""
    while True:
        try:
            await resync_all(deps)
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any failure; the next pass retries
            log_event(log, "authentik_resync_failed", level=logging.ERROR, code=type(exc).__name__)
        await asyncio.sleep(deps.config.resync_seconds)
