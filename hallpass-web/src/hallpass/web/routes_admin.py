# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin pages: people, invitations, locking, parent links, external identities, groups, terms, and services.

Every route needs an admin (require_admin), and every POST also needs a CSRF token (require_csrf). Each POST writes
a decision_log row and ends in a 303, so a refresh never repeats it.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from hallpass import authentik_sync, desired, grants, holds, registry, services, terms, text, tokens
from hallpass.auth import require_admin, require_admin_any_terms, require_csrf
from hallpass.authentik import AuthentikError
from hallpass.holds import HoldRefused
from hallpass.logs import log_event
from hallpass.registry import LinkRefused, NoSuchGroup, Person
from hallpass.web.pages import message_page, page
from hallpass.web.routes_me import service_rows

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
Admin = Annotated[Person, Depends(require_admin)]
# The terms pages are the one exemption from the page gate: the first version must be publishable before anyone has accepted.
AdminAnyTerms = Annotated[Person, Depends(require_admin_any_terms)]
WRITE = [Depends(require_csrf)]
WarningParam = Annotated[str | None, Query(alias="warning")]
WARNINGS = {"authentik": text.AUTHENTIK_NOT_UPDATED}


def _deps(request: Request):
    return request.app.state.deps


def _to(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _log(conn, *, now: datetime, admin: Person, action: str, target: int | None,
         detail: dict | None = None) -> None:
    registry.log_decision(conn, at=now, actor_kind="admin", actor_person_id=admin.id, target_person_id=target,
                          action=action, detail=detail)


async def _sync_affected(deps, person_id: int) -> bool:
    """After a change to one person: their Authentik state, and their children's, which carry the parent's address.
    Returns False when Authentik could not be brought in line."""
    affected = await deps.db.run(lambda c: authentik_sync.affected_by(c, person_id))
    return await authentik_sync.sync_people(deps, affected)


async def _sync_every_bound(deps) -> bool:
    """After a change that can reach anyone, such as a service, an entitlement, the terms or a group."""
    return await authentik_sync.sync_people(deps, await deps.db.run(authentik_sync.bound_person_ids))


# People ----------------------------------------------------------------------------------------------

async def _people_page(request: Request, admin: Person, *, status: int = 200, error: str | None = None) -> Response:
    people, missing = await _deps(request).db.run(
        lambda c: (registry.list_people(c), registry.people_missing_fluxer_address(c)))
    return page(request, "admin/people.html", viewer=admin, status_code=status, error=error, people=people,
                missing=missing, account_types=registry.ACCOUNT_TYPES, no_address_flag=text.NO_FLUXER_ADDRESS_FLAG)


@router.get("/people")
async def people_page(request: Request, admin: Admin) -> Response:
    return await _people_page(request, admin)


def _create(conn, *, admin: Person, now: datetime, name: str, account_type: str, email: str,
            phone: str) -> Person:
    person = registry.create_person(conn, display_name=name, account_type=account_type, email=email, phone=phone,
                                    now=now)
    _log(conn, now=now, admin=admin, action="person_created", target=person.id)
    return person


@router.post("/people", dependencies=WRITE)
async def create_person(request: Request, admin: Admin,
                        display_name: Annotated[str, Form()] = "",
                        account_type: Annotated[str, Form()] = "adult",
                        email: Annotated[str, Form()] = "",
                        phone: Annotated[str, Form()] = "") -> Response:
    deps = _deps(request)
    now = deps.clock()
    try:
        person = await deps.db.run(lambda c: _create(c, admin=admin, now=now, name=display_name,
                                                     account_type=account_type, email=email, phone=phone))
    except ValueError:
        return await _people_page(request, admin, status=400, error=text.INVALID_INPUT)
    await _issue_invitation(deps, person=person, enrollment_ref=person.enrollment_ref, admin=admin, now=now)
    return _to(f"/admin/people/{person.id}")


# Invitations -----------------------------------------------------------------------------------------

async def _issue_invitation(deps, *, person: Person, enrollment_ref: str, admin: Person, now: datetime) -> bool:
    """Creates an Authentik invitation for an unbound person and stores it. False when Authentik refuses, or when the
    person bound while the invitation was being made; the person is kept either way. In the second case the new
    invitation is deleted, because its ref can never match anyone."""
    try:
        invitation = await deps.authentik.create_invitation(
            name=f"hallpass-{person.ref}", flow_slug=deps.config.authentik_invite_flow,
            enrollment_ref=enrollment_ref, expires=now + timedelta(hours=deps.config.invite_expiry_hours))
    except AuthentikError as error:
        log_event(logger, "invitation_created", outcome="authentik_failed", reason=error.reason, person_id=person.id)
        return False

    def store(conn) -> bool:
        if not registry.set_invitation(conn, person.id, invitation_pk=invitation.pk, invite_url=invitation.url,
                                       enrollment_ref=enrollment_ref):
            return False
        _log(conn, now=now, admin=admin, action="invitation_created", target=person.id)
        return True

    if await deps.db.run(store):
        return True
    log_event(logger, "invitation_created", outcome="person_bound", person_id=person.id)
    await _retire_invitation(deps, invitation.pk)
    return False


async def _retire_invitation(deps, invitation_pk: str | None) -> None:
    """Best effort: a live old link carries a ref that no longer matches any person, so it cannot bind."""
    if invitation_pk is None:
        return
    try:
        await deps.authentik.delete_invitation(invitation_pk)
    except AuthentikError as error:
        log_event(logger, "invitation_retired", outcome="authentik_failed", reason=error.reason)


@router.post("/people/{person_id}/invite", dependencies=WRITE)
async def regenerate_invitation(request: Request, person_id: int, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()
    person = await deps.db.run(lambda c: _unbound_person(c, person_id))
    if person is None:
        return _to(f"/admin/people/{person_id}")
    new_ref = secrets.token_urlsafe(24)
    if await _issue_invitation(deps, person=person, enrollment_ref=new_ref, admin=admin, now=now):
        await _retire_invitation(deps, person.invitation_pk)
    return _to(f"/admin/people/{person_id}")


def _unbound_person(conn, person_id: int) -> Person | None:
    person = registry.get_person(conn, person_id)
    if person is None:
        raise registry.NoSuchPerson()
    return None if person.authentik_uuid is not None else person


# Person page -----------------------------------------------------------------------------------------

def _person_context(conn, person: Person) -> dict:
    parents = registry.parents_of(conn, person.id)
    children = registry.children_of(conn, person.id)
    linked = {p.id for p in parents + children}
    others = [p for p in registry.list_people(conn) if p.id != person.id and p.id not in linked]
    is_child = person.account_type == "child"
    return {
        "person": person,
        "fluxer_address": registry.fluxer_address(conn, person),
        "parents": parents,
        "children": children,
        "parent_choices": [p for p in others if p.account_type != "child"] if is_child else [],
        "child_choices": [] if is_child else [p for p in others if p.account_type == "child"],
        "identities": [(identity, [holder.display_name for holder in registry.other_holders(conn, identity)])
                      for identity in registry.identities_of(conn, person.id)],
        "identity_also_held": text.IDENTITY_ALSO_HELD,
        "identity_kinds": registry.IDENTITY_KINDS,
        "identity_note": text.IDENTITY_REMOVE_NOTE,
        "hold_rows": _hold_rows(conn, person.id),
        "modes": holds.MODES,
        "services": services.list_services(conn),
        "groups": registry.groups_of(conn, person.id),
        "account_types": registry.ACCOUNT_TYPES,
        "no_address_flag": text.NO_FLUXER_ADDRESS_FLAG,
        "invitation_not_created": text.INVITATION_NOT_CREATED,
        "invite_hint": text.INVITE_SHOWN_ONCE_HINT,
    }


def _hold_rows(conn, person_id: int) -> list[dict]:
    """One row per service the person is held on: who placed the holds, and whether the service can be suspended
    automatically, or is marked as suspended by hand."""
    held = holds.services_with_holds(conn, person_id)
    marked = holds.marked_service_ids(conn, person_id)
    rows = []
    for service in services.list_services(conn, include_retired=True):
        if service.id not in held:
            continue
        if service.auto_suspendable:
            wording = text.CAN_AUTO_SUSPEND
        elif service.id in marked:
            wording = text.MANUALLY_SUSPENDED
        else:
            wording = text.CANNOT_AUTO_SUSPEND
        rows.append({"service": service, "labels": holds.hold_labels(conn, person_id, service.id),
                     "suspendable": wording, "marked": service.id in marked})
    return rows


def _load_person(conn, person_id: int, now: datetime) -> dict:
    person = registry.get_person(conn, person_id)
    if person is None:
        raise registry.NoSuchPerson()
    _, pauses = desired.refresh_with_pauses(conn, now)
    rows = service_rows(conn, person, pauses)
    for row in rows:
        pause = row["shared_pause"]
        if pause is not None:
            causes = [registry.get_person(conn, cause) for cause in pause.causes]
            holders = [holder.display_name for holder in causes if holder is not None]
            row["shared_pause_note"] = f"{text.SHARED_PAUSE_CAUSES} {', '.join(holders)}"
    return {**_person_context(conn, person), "service_rows": rows}


async def _person_page(request: Request, admin: Person, person_id: int, *, status: int = 200,
                       error: str | None = None, warning: str | None = None) -> Response:
    deps = _deps(request)
    now = deps.clock()
    context = await deps.db.run(lambda c: _load_person(c, person_id, now))
    return page(request, "admin/person.html", viewer=admin, status_code=status, error=error,
                warning=WARNINGS.get(warning or ""), **context)


@router.get("/people/{person_id}")
async def person_page(request: Request, person_id: int, admin: Admin, warning: WarningParam = None) -> Response:
    return await _person_page(request, admin, person_id, warning=warning)


@router.post("/people/{person_id}", dependencies=WRITE)
async def update_person(request: Request, person_id: int, admin: Admin,
                        display_name: Annotated[str, Form()] = "",
                        account_type: Annotated[str, Form()] = "adult",
                        email: Annotated[str, Form()] = "",
                        phone: Annotated[str, Form()] = "",
                        fluxer_email_override: Annotated[str, Form()] = "") -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        before = registry.get_person(conn, person_id)
        registry.update_person(conn, person_id, display_name=display_name, account_type=account_type, email=email,
                               phone=phone, fluxer_email_override=fluxer_email_override)
        _log(conn, now=now, admin=admin, action="person_updated", target=person_id)
        if before is not None and before.account_type != account_type:
            grants.change_account_type(conn, person_id, old_type=before.account_type, new_type=account_type,
                                       actor_id=admin.id, now=now)

    try:
        await deps.db.run(change)
    except LinkRefused as error:
        return await _person_page(request, admin, person_id, status=400, error=str(error))
    except ValueError:
        return await _person_page(request, admin, person_id, status=400, error=text.INVALID_INPUT)
    await _sync_affected(deps, person_id)
    return _to(f"/admin/people/{person_id}")


# Locking ---------------------------------------------------------------------------------------------

async def _set_locked(request: Request, admin: Person, person_id: int, *, locked: bool) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> Person:
        person = registry.set_locked(conn, person_id, locked)
        _log(conn, now=now, admin=admin, action="locked" if locked else "unlocked", target=person_id)
        return person

    person = await deps.db.run(change)
    # The sync sets the active flag. The lock is kept in hallpass either way; the warning tells the admin when
    # Authentik still needs the change, and the resync loop retries it. Sessions end on a lock even when the sync
    # failed: a session must not outlive the lock because one of the sync's calls was refused.
    synced = await _sync_affected(deps, person_id)
    if locked and person.authentik_pk is not None:
        try:
            await deps.authentik.end_sessions(person.authentik_pk)
        except AuthentikError as error:
            log_event(logger, "account_sync", outcome="authentik_failed", reason=error.reason, person_id=person_id)
            synced = False
    return _to(f"/admin/people/{person_id}" if synced else f"/admin/people/{person_id}?warning=authentik")


@router.post("/people/{person_id}/lock", dependencies=WRITE)
async def lock_person(request: Request, person_id: int, admin: Admin) -> Response:
    return await _set_locked(request, admin, person_id, locked=True)


@router.post("/people/{person_id}/unlock", dependencies=WRITE)
async def unlock_person(request: Request, person_id: int, admin: Admin) -> Response:
    return await _set_locked(request, admin, person_id, locked=False)


# Parent links ----------------------------------------------------------------------------------------

async def _link(request: Request, admin: Person, *, child_id: int, parent_id: int, page_id: int) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        registry.link_parent(conn, child_id, parent_id, now)
        _log(conn, now=now, admin=admin, action="parent_linked", target=child_id, detail={"parent_id": parent_id})

    try:
        await deps.db.run(change)
    except LinkRefused as error:
        return await _person_page(request, admin, page_id, status=400, error=str(error))
    await _sync_affected(deps, child_id)
    return _to(f"/admin/people/{page_id}")


@router.post("/people/{person_id}/parents", dependencies=WRITE)
async def add_parent(request: Request, person_id: int, admin: Admin,
                     parent_id: Annotated[int, Form()]) -> Response:
    return await _link(request, admin, child_id=person_id, parent_id=parent_id, page_id=person_id)


@router.post("/people/{person_id}/children", dependencies=WRITE)
async def add_child(request: Request, person_id: int, admin: Admin,
                    child_id: Annotated[int, Form()]) -> Response:
    return await _link(request, admin, child_id=child_id, parent_id=person_id, page_id=person_id)


def _ensure_people(conn, *person_ids: int) -> None:
    for person_id in person_ids:
        if registry.get_person(conn, person_id) is None:
            raise registry.NoSuchPerson()


@router.post("/people/{child_id}/parents/{parent_id}/remove", dependencies=WRITE)
async def remove_parent(request: Request, child_id: int, parent_id: int, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        _ensure_people(conn, child_id, parent_id)
        # Log only a link that existed, so a repeated or stale form records no change that did not happen.
        if registry.unlink_parent(conn, child_id, parent_id) == 1:
            _log(conn, now=now, admin=admin, action="parent_unlinked", target=child_id,
                 detail={"parent_id": parent_id})

    await deps.db.run(change)
    await _sync_affected(deps, child_id)
    return _to(f"/admin/people/{child_id}")


# External identities ---------------------------------------------------------------------------------

@router.post("/people/{person_id}/identities", dependencies=WRITE)
async def add_identity(request: Request, person_id: int, admin: Admin,
                       service_slug: Annotated[str, Form()] = "",
                       kind: Annotated[str, Form()] = "",
                       value: Annotated[str, Form()] = "") -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        if registry.get_person(conn, person_id) is None:
            raise registry.NoSuchPerson()
        identity_id = registry.add_identity(conn, person_id, service_slug, kind, value)
        _log(conn, now=now, admin=admin, action="identity_added", target=person_id,
             detail={"identity_id": identity_id, "kind": kind})

    try:
        await deps.db.run(change)
    except ValueError:
        return await _person_page(request, admin, person_id, status=400, error=text.INVALID_INPUT)
    except sqlite3.IntegrityError:
        return await _person_page(request, admin, person_id, status=400, error=text.IDENTITY_TAKEN)
    await _sync_affected(deps, person_id)
    return _to(f"/admin/people/{person_id}")


@router.post("/identities/{identity_id}/remove", dependencies=WRITE)
async def remove_identity(request: Request, identity_id: int, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> int | None:
        identity = registry.get_identity(conn, identity_id)
        if identity is None:
            return None
        registry.remove_identity(conn, identity_id)
        _log(conn, now=now, admin=admin, action="identity_removed", target=identity.person_id,
             detail={"identity_id": identity_id})
        return identity.person_id

    owner = await deps.db.run(change)
    if owner is not None:
        await _sync_affected(deps, owner)
    return _to("/admin/people" if owner is None else f"/admin/people/{owner}")


# Groups ----------------------------------------------------------------------------------------------

async def _groups_page(request: Request, admin: Person, *, status: int = 200, error: str | None = None) -> Response:
    groups = await _deps(request).db.run(registry.list_groups)
    return page(request, "admin/groups.html", viewer=admin, status_code=status, error=error, groups=groups)


@router.get("/groups")
async def groups_page(request: Request, admin: Admin) -> Response:
    return await _groups_page(request, admin)


@router.post("/groups", dependencies=WRITE)
async def create_group(request: Request, admin: Admin, name: Annotated[str, Form()] = "") -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> int:
        group_id = registry.create_group(conn, name)
        _log(conn, now=now, admin=admin, action="group_created", target=None, detail={"group_id": group_id})
        return group_id

    try:
        group_id = await deps.db.run(change)
    except ValueError:
        return await _groups_page(request, admin, status=400, error=text.INVALID_INPUT)
    except sqlite3.IntegrityError:
        return await _groups_page(request, admin, status=400, error=text.GROUP_TAKEN)
    return _to(f"/admin/groups/{group_id}")


def _ensure_group(conn, group_id: int) -> None:
    if not any(group.id == group_id for group in registry.list_groups(conn)):
        raise NoSuchGroup()


def _group_context(conn, group_id: int) -> dict:
    _ensure_group(conn, group_id)
    group = next(g for g in registry.list_groups(conn) if g.id == group_id)
    members = registry.members(conn, group_id)
    member_ids = {m.id for m in members}
    return {"group": group, "members": members,
            "candidates": [p for p in registry.list_people(conn) if p.id not in member_ids]}


@router.get("/groups/{group_id}")
async def group_page(request: Request, group_id: int, admin: Admin) -> Response:
    context = await _deps(request).db.run(lambda c: _group_context(c, group_id))
    return page(request, "admin/group.html", viewer=admin, **context)


@router.post("/groups/{group_id}/members", dependencies=WRITE)
async def add_member(request: Request, group_id: int, admin: Admin,
                     person_id: Annotated[int, Form()]) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        _ensure_group(conn, group_id)
        if registry.get_person(conn, person_id) is None:
            raise registry.NoSuchPerson()
        registry.add_member(conn, group_id, person_id)
        _log(conn, now=now, admin=admin, action="member_added", target=person_id, detail={"group_id": group_id})

    await deps.db.run(change)
    await authentik_sync.sync_people(deps, [person_id])
    return _to(f"/admin/groups/{group_id}")


@router.post("/groups/{group_id}/members/{person_id}/remove", dependencies=WRITE)
async def remove_member(request: Request, group_id: int, person_id: int, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        _ensure_group(conn, group_id)
        _ensure_people(conn, person_id)
        if registry.remove_member(conn, group_id, person_id) == 1:
            _log(conn, now=now, admin=admin, action="member_removed", target=person_id,
                 detail={"group_id": group_id})

    await deps.db.run(change)
    await authentik_sync.sync_people(deps, [person_id])
    return _to(f"/admin/groups/{group_id}")


@router.post("/groups/{group_id}/delete", dependencies=WRITE)
async def delete_group(request: Request, group_id: int, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        _ensure_group(conn, group_id)
        registry.delete_group(conn, group_id)
        _log(conn, now=now, admin=admin, action="group_deleted", target=None, detail={"group_id": group_id})

    await deps.db.run(change)
    await _sync_every_bound(deps)
    return _to("/admin/groups")


# Terms -----------------------------------------------------------------------------------------------

def _versions_rows(conn) -> list[dict]:
    rows = []
    for version in terms.list_versions(conn):
        accepted, missing = terms.acceptance_report(conn, version.id)
        rows.append({"version": version, "accepted": len(accepted), "not_accepted": len(missing)})
    return rows


async def _terms_page(request: Request, admin: Person, *, status: int = 200, error: str | None = None) -> Response:
    rows = await _deps(request).db.run(_versions_rows)
    return page(request, "admin/terms.html", viewer=admin, status_code=status, error=error, rows=rows)


@router.get("/terms")
async def terms_page(request: Request, admin: AdminAnyTerms) -> Response:
    return await _terms_page(request, admin)


@router.post("/terms", dependencies=WRITE)
async def publish_terms(request: Request, admin: AdminAnyTerms,
                        tos_text: Annotated[str, Form()] = "",
                        privacy_text: Annotated[str, Form()] = "",
                        blocking: Annotated[str | None, Form()] = None) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        terms.publish(conn, tos_text=tos_text, privacy_text=privacy_text, blocking=blocking is not None,
                      actor_id=admin.id, now=now)

    try:
        await deps.db.run(change)
    except ValueError:
        return await _terms_page(request, admin, status=400, error=text.INVALID_INPUT)
    await _sync_every_bound(deps)
    return _to("/admin/terms")


def _version_report(conn, version_id: int) -> dict | None:
    version = terms.get(conn, version_id)
    if version is None:
        return None
    accepted, missing = terms.acceptance_report(conn, version_id)
    return {"version": version, "accepted": accepted, "missing": missing}


@router.get("/terms/{version_id}")
async def terms_version_page(request: Request, version_id: int, admin: AdminAnyTerms) -> Response:
    report = await _deps(request).db.run(lambda c: _version_report(c, version_id))
    if report is None:
        return message_page(request, text.NOT_FOUND, 404)
    return page(request, "admin/terms_version.html", viewer=admin, **report)


# Services ----------------------------------------------------------------------------------------------

def _optional_id(raw: str) -> int | None:
    clean = raw.strip()
    if not clean:
        return None
    if not clean.isdigit():
        raise ValueError("person id must be a number")
    return int(clean)


async def _services_page(request: Request, admin: Person, *, status: int = 200, error: str | None = None) -> Response:
    rows = await _deps(request).db.run(lambda c: services.list_services(c, include_retired=True))
    return page(request, "admin/services.html", viewer=admin, status_code=status, error=error, services=rows,
                service_kinds=services.SERVICE_KINDS)


@router.get("/services")
async def services_page(request: Request, admin: Admin) -> Response:
    return await _services_page(request, admin)


@router.post("/services", dependencies=WRITE)
async def create_service(request: Request, admin: Admin,
                         slug: Annotated[str, Form()] = "",
                         name: Annotated[str, Form()] = "",
                         kind: Annotated[str, Form()] = "",
                         adapter: Annotated[str, Form()] = "",
                         url: Annotated[str, Form()] = "",
                         auto_suspendable: Annotated[str | None, Form()] = None) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> int:
        service = services.create_service(conn, slug=slug, name=name, kind=kind, adapter=adapter,
                                          auto_suspendable=auto_suspendable is not None, url=url or None,
                                          actor_id=admin.id, now=now)
        return service.id

    try:
        service_id = await deps.db.run(change)
    except services.ServiceRefused as refused:
        return await _services_page(request, admin, status=400, error=str(refused))
    except ValueError:
        return await _services_page(request, admin, status=400, error=text.INVALID_INPUT)
    except sqlite3.IntegrityError:
        return await _services_page(request, admin, status=400, error=text.SERVICE_TAKEN)
    return _to(f"/admin/services/{service_id}")


def _service_context(conn, service_id: int) -> dict:
    service = services.get(conn, service_id)
    if service is None:
        raise services.NoSuchService()
    group_names = {str(group.id): group.name for group in registry.list_groups(conn)}
    people = registry.list_people(conn)
    person_names = {str(person.id): person.display_name for person in people}
    def label(subject_kind: str, subject: str) -> str:
        if subject_kind == "type":
            return subject
        names = group_names if subject_kind == "group" else person_names
        return names.get(subject, subject)

    entitlements = [{"id": e.id, "subject_kind": e.subject_kind, "effect": e.effect,
                     "label": label(e.subject_kind, e.subject)}
                    for e in services.entitlements_for(conn, service_id)]
    attached = [{"token": t, "person": person_names.get(str(t.person_id), "") if t.person_id else "everyone entitled"}
                for t in tokens.attached_for(conn, service_id)]
    subject_choices = ([{"value": f"type:{kind}", "label": f"Type: {kind}"} for kind in registry.ACCOUNT_TYPES]
                       + [{"value": f"group:{gid}", "label": f"Group: {name}"} for gid, name in group_names.items()]
                       + [{"value": f"person:{p.id}", "label": f"Person: {p.display_name}"} for p in people])
    return {"service": service, "entitlements": entitlements, "attached": attached,
            "subject_choices": subject_choices, "people": people, "attached_kinds": tokens.ATTACHED_KINDS,
            "retire_note": text.SERVICE_RETIRE_NOTE}


async def _service_page(request: Request, admin: Person, service_id: int, *, status: int = 200,
                        error: str | None = None) -> Response:
    context = await _deps(request).db.run(lambda c: _service_context(c, service_id))
    return page(request, "admin/service.html", viewer=admin, status_code=status, error=error, **context)


@router.get("/services/{service_id}")
async def service_page(request: Request, service_id: int, admin: Admin) -> Response:
    return await _service_page(request, admin, service_id)


@router.post("/services/{service_id}", dependencies=WRITE)
async def update_service(request: Request, service_id: int, admin: Admin,
                         name: Annotated[str, Form()] = "",
                         adapter: Annotated[str, Form()] = "",
                         url: Annotated[str, Form()] = "",
                         auto_suspendable: Annotated[str | None, Form()] = None) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        services.update_service(conn, service_id, name=name, adapter=adapter,
                                auto_suspendable=auto_suspendable is not None, url=url or None,
                                actor_id=admin.id, now=now)

    try:
        await deps.db.run(change)
    except services.ServiceRefused as refused:
        return await _service_page(request, admin, service_id, status=400, error=str(refused))
    except ValueError:
        return await _service_page(request, admin, service_id, status=400, error=text.INVALID_INPUT)
    await _sync_every_bound(deps)
    return _to(f"/admin/services/{service_id}")


async def _set_retired(request: Request, admin: Person, service_id: int, *, retired: bool) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        if retired:
            services.retire_service(conn, service_id, actor_id=admin.id, now=now)
        else:
            services.unretire_service(conn, service_id, actor_id=admin.id, now=now)

    await deps.db.run(change)
    await _sync_every_bound(deps)
    return _to(f"/admin/services/{service_id}")


@router.post("/services/{service_id}/retire", dependencies=WRITE)
async def retire_service(request: Request, service_id: int, admin: Admin) -> Response:
    return await _set_retired(request, admin, service_id, retired=True)


@router.post("/services/{service_id}/unretire", dependencies=WRITE)
async def unretire_service(request: Request, service_id: int, admin: Admin) -> Response:
    return await _set_retired(request, admin, service_id, retired=False)


@router.post("/services/{service_id}/entitlements", dependencies=WRITE)
async def add_service_entitlement(request: Request, service_id: int, admin: Admin,
                                  subject: Annotated[str, Form()] = "",
                                  effect: Annotated[str, Form()] = "grant") -> Response:
    """`subject` is `kind:value` as the form's select offers it: `type:adult`, `group:<id>` or `person:<id>`."""
    deps = _deps(request)
    now = deps.clock()
    kind, _, value = subject.partition(":")

    def change(conn) -> None:
        if kind not in services.SUBJECT_KINDS or not value:
            raise ValueError("unknown subject")
        services.add_entitlement(conn, service_id, kind, value, effect, actor_id=admin.id, now=now)

    try:
        await deps.db.run(change)
    except services.EntitlementRefused as refused:
        return await _service_page(request, admin, service_id, status=400, error=str(refused))
    except ValueError:
        return await _service_page(request, admin, service_id, status=400, error=text.INVALID_INPUT)
    except sqlite3.IntegrityError:
        return await _service_page(request, admin, service_id, status=400, error=text.ENTITLEMENT_TAKEN)
    await _sync_every_bound(deps)
    return _to(f"/admin/services/{service_id}")


@router.post("/entitlements/{entitlement_id}/remove", dependencies=WRITE)
async def remove_service_entitlement(request: Request, entitlement_id: int, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> int | None:
        service_id = services.entitlement_service_id(conn, entitlement_id)
        if service_id is not None:
            services.remove_entitlement(conn, entitlement_id, actor_id=admin.id, now=now)
        return service_id

    service_id = await deps.db.run(change)
    if service_id is not None:
        await _sync_every_bound(deps)
    return _to("/admin/services" if service_id is None else f"/admin/services/{service_id}")


@router.post("/services/{service_id}/tokens", dependencies=WRITE)
async def attach_token(request: Request, service_id: int, admin: Admin,
                       kind: Annotated[str, Form()] = "",
                       label: Annotated[str, Form()] = "",
                       value: Annotated[str, Form()] = "",
                       person: Annotated[str, Form()] = "") -> Response:
    """Attaches a token to the service: for everyone entitled when `person` is blank, otherwise for that person.
    The log row names the token id and kind only, never the value."""
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        service = services.get(conn, service_id)
        if service is None:
            raise services.NoSuchService()
        person_id = _optional_id(person)
        if person_id is not None and registry.get_person(conn, person_id) is None:
            raise registry.NoSuchPerson()
        token_id = tokens.attach(conn, service_id=service_id, person_id=person_id, kind=kind, label=label.strip(),
                                 value=value, now=now)
        registry.log_decision(conn, at=now, actor_kind="admin", actor_person_id=admin.id,
                              target_person_id=person_id, action="token_attached", services=[service.slug],
                              detail={"token_id": token_id, "kind": kind})

    try:
        await deps.db.run(change)
    except ValueError:
        return await _service_page(request, admin, service_id, status=400, error=text.INVALID_INPUT)
    return _to(f"/admin/services/{service_id}")


@router.post("/tokens/{token_id}/revoke", dependencies=WRITE)
async def revoke_token(request: Request, token_id: int, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> int | None:
        token = tokens.get_attached(conn, token_id)
        if token is None or token.revoked:
            return None
        service = services.get(conn, token.service_id)
        tokens.revoke(conn, token_id, now)
        registry.log_decision(conn, at=now, actor_kind="admin", actor_person_id=admin.id,
                              target_person_id=token.person_id, action="token_revoked", services=[service.slug],
                              detail={"token_id": token_id, "kind": token.kind})
        return token.service_id

    service_id = await deps.db.run(change)
    return _to("/admin/services" if service_id is None else f"/admin/services/{service_id}")


# Holds, manual marks and conflict modes ---------------------------------------------------------------

def _window(deps) -> timedelta:
    return timedelta(hours=deps.config.conflict_window_hours)


@router.post("/people/{person_id}/holds", dependencies=WRITE)
async def admin_place_hold(request: Request, person_id: int, admin: Admin,
                           service_id: Annotated[str, Form()] = "") -> Response:
    """Places an admin hold on one service, or on every service the person has (`all`)."""
    deps = _deps(request)
    now = deps.clock()
    try:
        scope = holds.service_scope(service_id)
    except ValueError:
        return await _person_page(request, admin, person_id, status=400, error=text.INVALID_INPUT)

    def change(conn) -> None:
        holds.place(conn, actor=holds.Actor("admin", admin.id), target_id=person_id, service_ids=scope, now=now,
                    window=_window(deps), admin_email=deps.config.admin_email)

    await deps.db.run(change)
    await _sync_affected(deps, person_id)
    return _to(f"/admin/people/{person_id}")


@router.post("/people/{person_id}/holds/lift", dependencies=WRITE)
async def admin_lift_holds(request: Request, person_id: int, admin: Admin,
                           service_id: Annotated[str, Form()] = "",
                           include_parent: Annotated[str | None, Form()] = None) -> Response:
    """Lifts the admin holds on one service or on all of them. Parent holds go only when `include_parent` is ticked.
    Lockdown holds stay: they are lifted by resolving their review."""
    deps = _deps(request)
    now = deps.clock()
    try:
        scope = holds.service_scope(service_id)
    except ValueError:
        return await _person_page(request, admin, person_id, status=400, error=text.INVALID_INPUT)

    def change(conn) -> None:
        ids = sorted(holds.services_with_holds(conn, person_id)) if scope is None else scope
        holds.lift(conn, actor=holds.Actor("admin", admin.id), target_id=person_id, service_ids=ids, now=now,
                   window=_window(deps), admin_email=deps.config.admin_email,
                   include_parent=include_parent is not None)

    await deps.db.run(change)
    await _sync_affected(deps, person_id)
    return _to(f"/admin/people/{person_id}")


@router.post("/people/{person_id}/manual/{service_id}", dependencies=WRITE)
async def mark_manual(request: Request, person_id: int, service_id: int, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        holds.mark_manual(conn, admin_id=admin.id, person_id=person_id, service_id=service_id, now=now)

    try:
        await deps.db.run(change)
    except HoldRefused as refused:
        return await _person_page(request, admin, person_id, status=400, error=refused.message)
    await _sync_affected(deps, person_id)
    return _to(f"/admin/people/{person_id}")


@router.post("/people/{person_id}/manual/{service_id}/unmark", dependencies=WRITE)
async def unmark_manual(request: Request, person_id: int, service_id: int, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> None:
        holds.unmark_manual(conn, admin_id=admin.id, person_id=person_id, service_id=service_id, now=now)

    await deps.db.run(change)
    await _sync_affected(deps, person_id)
    return _to(f"/admin/people/{person_id}")


@router.post("/people/{person_id}/mode", dependencies=WRITE)
async def set_conflict_mode(request: Request, person_id: int, admin: Admin,
                            mode: Annotated[str, Form()] = "",
                            agreement_note: Annotated[str, Form()] = "",
                            apply_to_siblings: Annotated[str | None, Form()] = None) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> list[int]:
        return holds.set_mode(conn, child_id=person_id, mode=mode, agreement_note=agreement_note,
                              apply_to_siblings=apply_to_siblings is not None, admin_id=admin.id, now=now)

    try:
        changed = await deps.db.run(change)
    except ValueError:
        return await _person_page(request, admin, person_id, status=400, error=text.INVALID_INPUT)
    await authentik_sync.sync_people(deps, changed)
    return _to(f"/admin/people/{person_id}")


# Monitor health ----------------------------------------------------------------------------------------

@router.get("/monitor")
async def monitor_page(request: Request, admin: Admin) -> Response:
    deps = _deps(request)
    now = deps.clock()
    health = await deps.db.run(lambda c: desired.monitor_health(c, now))
    return page(request, "admin/monitor.html", viewer=admin, **health)


# Reviews ---------------------------------------------------------------------------------------------

async def _reviews_page(request: Request, admin: Person) -> Response:
    open_items, resolved_items = await _deps(request).db.run(holds.list_reviews)
    return page(request, "admin/reviews.html", viewer=admin, open_items=open_items, resolved_items=resolved_items)


@router.get("/reviews")
async def reviews_page(request: Request, admin: Admin) -> Response:
    return await _reviews_page(request, admin)


async def _review_page(request: Request, admin: Person, review_id: int, *, status: int = 200,
                       error: str | None = None) -> Response:
    detail = await _deps(request).db.run(lambda c: holds.review_detail(c, review_id))
    if detail is None:
        return message_page(request, text.NOT_FOUND, 404)
    return page(request, "admin/review.html", viewer=admin, status_code=status, error=error, review=detail)


@router.get("/reviews/{review_id}")
async def review_page(request: Request, review_id: int, admin: Admin) -> Response:
    return await _review_page(request, admin, review_id)


@router.post("/reviews/{review_id}/resolve", dependencies=WRITE)
async def resolve_review(request: Request, review_id: int, admin: Admin,
                         outcome: Annotated[str, Form()] = "",
                         lift_lockdown: Annotated[str | None, Form()] = None) -> Response:
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> int:
        child_id = holds.review_child_id(conn, review_id)
        if child_id is None:
            raise holds.NoSuchReview()
        holds.resolve_review(conn, admin_id=admin.id, review_id=review_id, outcome=outcome,
                             lift_lockdown=lift_lockdown is not None, now=now)
        return child_id

    try:
        child_id = await deps.db.run(change)
    except holds.NoSuchReview:
        return message_page(request, text.NOT_FOUND, 404)
    except ValueError:
        return await _review_page(request, admin, review_id, status=400, error=text.INVALID_INPUT)
    await authentik_sync.sync_people(deps, [child_id])
    return _to(f"/admin/reviews/{review_id}")


@router.post("/reviews/{review_id}/lift", dependencies=WRITE)
async def lift_review_lockdown(request: Request, review_id: int, admin: Admin) -> Response:
    """Lifts the lockdown a resolved review left in force. Only the administrator may lift a lockdown."""
    deps = _deps(request)
    now = deps.clock()

    def change(conn) -> int:
        child_id = holds.review_child_id(conn, review_id)
        if child_id is None:
            raise holds.NoSuchReview()
        holds.lift_lockdown(conn, admin_id=admin.id, review_id=review_id, now=now)
        return child_id

    try:
        child_id = await deps.db.run(change)
    except holds.NoSuchReview:
        return message_page(request, text.NOT_FOUND, 404)
    except ValueError:
        return await _review_page(request, admin, review_id, status=400, error=text.INVALID_INPUT)
    await authentik_sync.sync_people(deps, [child_id])
    return _to(f"/admin/reviews/{review_id}")

