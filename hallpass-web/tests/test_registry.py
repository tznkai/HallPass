# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registry store tests against a real SQLite database under tmp_path."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hallpass import registry, text
from hallpass.db import Database
from hallpass.registry import LinkRefused, NoSuchPerson

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
# Built from parts so no tracked file holds a 15+ digit run. A SteamID64 shape, not a real account.
STEAM_ID = "".join(["7656", "1198", "0000", "0001"])
CHILD_UUID = "00000000-0000-4000-8000-0000000000c1"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hp.db")
    try:
        yield database
    finally:
        database.close()


async def make(db: Database, **fields) -> registry.Person:
    kwargs = {"display_name": "Someone", "account_type": "adult", "now": NOW, **fields}
    return await db.run(lambda c: registry.create_person(c, **kwargs))


async def bind_child(db: Database, person_id: int, username: str = "kid") -> None:
    await db.run(lambda c: registry.bind_identity(c, person_id, uuid=CHILD_UUID, authentik_pk=1, username=username))


# AC2.1 ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("account_type", ["adult", "parent", "child"])
@pytest.mark.parametrize("contact", [{}, {"email": "pat@example.com", "phone": "555-0100"}],
                         ids=["no-contact", "with-contact"])
async def test_create_person_of_each_type_with_and_without_contact(db: Database, account_type: str,
                                                                   contact: dict) -> None:
    person = await make(db, display_name="Pat", account_type=account_type, **contact)

    stored = await db.run(lambda c: registry.get_person(c, person.id))
    assert stored is not None
    assert stored.display_name == "Pat"
    assert stored.account_type == account_type
    assert stored.status == "invited"
    assert stored.email == contact.get("email")
    assert stored.phone == contact.get("phone")


async def test_update_person_changes_fields_and_keeps_ref(db: Database) -> None:
    person = await make(db, display_name="Ann", account_type="adult", email="ann@example.com")

    updated = await db.run(lambda c: registry.update_person(
        c, person.id, display_name="Ann B", account_type="parent", email="ann.b@example.com",
        phone="555-0199", fluxer_email_override=None))

    assert updated.display_name == "Ann B"
    assert updated.account_type == "parent"
    assert updated.email == "ann.b@example.com"
    assert updated.phone == "555-0199"
    assert updated.ref == person.ref


async def test_person_ref_is_22_url_safe_characters_and_unique(db: Database) -> None:
    first = await make(db, display_name="One")
    second = await make(db, display_name="Two")

    pattern = re.compile(r"[A-Za-z0-9_-]{22}")
    assert pattern.fullmatch(first.ref)
    assert pattern.fullmatch(second.ref)
    assert first.ref != second.ref


async def test_blank_contact_fields_are_stored_as_none(db: Database) -> None:
    person = await make(db, email="   ", phone="")
    assert person.email is None
    assert person.phone is None


async def test_list_people_returns_every_person_in_id_order(db: Database) -> None:
    first = await make(db, display_name="One")
    second = await make(db, display_name="Two")
    people = await db.run(lambda c: registry.list_people(c))
    assert [p.id for p in people] == [first.id, second.id]


async def test_person_repr_does_not_show_contacts_or_enrollment(db: Database) -> None:
    person = await make(db, email="pat@example.com", phone="555-0100")
    text_repr = repr(person)
    assert "pat@example.com" not in text_repr
    assert "555-0100" not in text_repr
    assert person.enrollment_ref not in text_repr


# AC2.2 ----------------------------------------------------------------------------------------------

async def test_child_linked_to_three_parents_lists_them_in_link_order(db: Database) -> None:
    child = await make(db, display_name="Kid", account_type="child")
    ann = await make(db, display_name="Ann", account_type="parent")
    bob = await make(db, display_name="Bob", account_type="parent")
    step = await make(db, display_name="Step", account_type="adult")

    def link_all(c: sqlite3.Connection) -> None:
        for parent in (ann, bob, step):
            registry.link_parent(c, child.id, parent.id, NOW)

    await db.run(link_all)

    parents = await db.run(lambda c: registry.parents_of(c, child.id))
    assert [p.display_name for p in parents] == ["Ann", "Bob", "Step"]


async def test_unlink_one_parent_leaves_two(db: Database) -> None:
    child = await make(db, account_type="child")
    ann = await make(db, display_name="Ann", account_type="parent")
    bob = await make(db, display_name="Bob", account_type="parent")
    step = await make(db, display_name="Step", account_type="adult")

    def link_then_unlink(c: sqlite3.Connection) -> None:
        for parent in (ann, bob, step):
            registry.link_parent(c, child.id, parent.id, NOW)
        registry.unlink_parent(c, child.id, bob.id)

    await db.run(link_then_unlink)

    parents = await db.run(lambda c: registry.parents_of(c, child.id))
    assert [p.display_name for p in parents] == ["Ann", "Step"]


async def test_same_parent_can_parent_two_children(db: Database) -> None:
    parent = await make(db, display_name="Ann", account_type="parent")
    first = await make(db, display_name="First", account_type="child")
    second = await make(db, display_name="Second", account_type="child")

    def link_both(c: sqlite3.Connection) -> None:
        registry.link_parent(c, first.id, parent.id, NOW)
        registry.link_parent(c, second.id, parent.id, NOW)

    await db.run(link_both)

    children = await db.run(lambda c: registry.children_of(c, parent.id))
    assert [p.display_name for p in children] == ["First", "Second"]


async def test_relinking_keeps_the_original_place_in_the_order(db: Database) -> None:
    child = await make(db, account_type="child")
    ann = await make(db, display_name="Ann", account_type="parent")
    bob = await make(db, display_name="Bob", account_type="parent")

    def link_again(c: sqlite3.Connection) -> None:
        registry.link_parent(c, child.id, ann.id, NOW)
        registry.link_parent(c, child.id, bob.id, NOW)
        registry.link_parent(c, child.id, ann.id, NOW)

    await db.run(link_again)
    parents = await db.run(lambda c: registry.parents_of(c, child.id))
    assert [p.display_name for p in parents] == ["Ann", "Bob"]


# AC2.3 ----------------------------------------------------------------------------------------------

async def test_group_add_two_members_then_remove_one(db: Database) -> None:
    group_id = await db.run(lambda c: registry.create_group(c, "Movie night"))
    first = await make(db, display_name="First")
    second = await make(db, display_name="Second")

    def add_both(c: sqlite3.Connection) -> None:
        registry.add_member(c, group_id, first.id)
        registry.add_member(c, group_id, second.id)
        registry.remove_member(c, group_id, first.id)

    await db.run(add_both)

    members = await db.run(lambda c: registry.members(c, group_id))
    assert [p.display_name for p in members] == ["Second"]
    groups = await db.run(lambda c: registry.groups_of(c, second.id))
    assert [g.name for g in groups] == ["Movie night"]
    assert await db.run(lambda c: registry.groups_of(c, first.id)) == []


async def test_duplicate_group_name_is_refused(db: Database) -> None:
    await db.run(lambda c: registry.create_group(c, "Same"))
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: registry.create_group(c, "Same"))


async def test_delete_group_removes_it_from_the_list(db: Database) -> None:
    group_id = await db.run(lambda c: registry.create_group(c, "Temp"))
    await db.run(lambda c: registry.delete_group(c, group_id))
    assert await db.run(lambda c: registry.list_groups(c)) == []


async def test_identities_of_all_four_kinds_then_remove_one(db: Database) -> None:
    person = await make(db, display_name="Player", account_type="child")

    def add_all(c: sqlite3.Connection) -> list[int]:
        return [
            registry.add_identity(c, person.id, "fluxer", "fluxer_user_id", "1001"),
            registry.add_identity(c, person.id, "minecraft", "java_name", "kidplayer"),
            registry.add_identity(c, person.id, "minecraft-bedrock", "bedrock_gamertag", "kidplayer2"),
            registry.add_identity(c, person.id, "steam", "steam_id", STEAM_ID),
        ]

    ids = await db.run(add_all)
    await db.run(lambda c: registry.remove_identity(c, ids[1]))

    identities = await db.run(lambda c: registry.identities_of(c, person.id))
    assert [(i.kind, i.value) for i in identities] == [
        ("fluxer_user_id", "1001"), ("bedrock_gamertag", "kidplayer2"), ("steam_id", STEAM_ID),
    ]


async def test_duplicate_identity_for_same_service_kind_value_raises_integrity_error(db: Database) -> None:
    first = await make(db, display_name="First", account_type="child")
    second = await make(db, display_name="Second", account_type="child")
    await db.run(lambda c: registry.add_identity(c, first.id, "minecraft", "java_name", "kidplayer"))
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: registry.add_identity(c, second.id, "minecraft", "java_name", "kidplayer"))


async def test_non_digit_steam_id_is_refused(db: Database) -> None:
    person = await make(db, account_type="child")
    with pytest.raises(ValueError, match="steam_id must be digits"):
        await db.run(lambda c: registry.add_identity(c, person.id, "steam", "steam_id", "not-digits"))
    assert await db.run(lambda c: registry.identities_of(c, person.id)) == []


async def test_unknown_identity_kind_is_refused(db: Database) -> None:
    person = await make(db, account_type="child")
    with pytest.raises(ValueError, match="unknown identity kind"):
        await db.run(lambda c: registry.add_identity(c, person.id, "steam", "discord", "1"))


# AC2.4 ----------------------------------------------------------------------------------------------

async def _child_with_parents(db: Database, parent_emails: list[str | None], *, child_email: str | None = None,
                              username: str | None = "kid") -> tuple[registry.Person, list[registry.Person]]:
    child = await make(db, display_name="Kid", account_type="child", email=child_email)
    if username is not None:
        await bind_child(db, child.id, username)
    parents = []
    for index, email in enumerate(parent_emails):
        parents.append(await make(db, display_name=f"Parent {index}", account_type="parent", email=email))

    def link_all(c: sqlite3.Connection) -> None:
        for parent in parents:
            registry.link_parent(c, child.id, parent.id, NOW)

    await db.run(link_all)
    stored = await db.run(lambda c: registry.get_person(c, child.id))
    assert stored is not None
    return stored, parents


async def _address(db: Database, child: registry.Person) -> str | None:
    return await db.run(lambda c: registry.fluxer_address(c, child))


async def test_child_without_email_gets_plus_address_from_parent(db: Database) -> None:
    child, _ = await _child_with_parents(db, ["ann@example.com"])
    assert await _address(db, child) == "ann+kid@example.com"


async def test_override_wins_over_the_derived_address(db: Database) -> None:
    child, _ = await _child_with_parents(db, ["ann@example.com"])
    await db.run(lambda c: registry.update_person(
        c, child.id, display_name=child.display_name, account_type="child", email=None, phone=None,
        fluxer_email_override="override@example.net"))
    refreshed = await db.run(lambda c: registry.get_person(c, child.id))
    assert refreshed is not None
    assert await _address(db, refreshed) == "override@example.net"


async def test_childs_own_email_wins_over_the_parents(db: Database) -> None:
    child, _ = await _child_with_parents(db, ["ann@example.com"], child_email="kid@example.org")
    assert await _address(db, child) == "kid@example.org"


async def test_second_parent_is_used_when_the_first_has_no_email(db: Database) -> None:
    child, _ = await _child_with_parents(db, [None, "bo@example.com"])
    assert await _address(db, child) == "bo+kid@example.com"


async def test_plus_tag_on_parent_email_is_dropped_before_adding_the_child_tag(db: Database) -> None:
    child, _ = await _child_with_parents(db, ["ann+x@example.com"])
    assert await _address(db, child) == "ann+kid@example.com"


async def test_child_without_username_gets_no_derived_address(db: Database) -> None:
    child, _ = await _child_with_parents(db, ["ann@example.com"], username=None)
    assert await _address(db, child) is None


# AC2.6 ----------------------------------------------------------------------------------------------

async def test_linking_an_adult_as_the_child_is_refused_and_creates_no_row(db: Database) -> None:
    adult = await make(db, display_name="Adult", account_type="adult")
    parent = await make(db, display_name="Ann", account_type="parent")

    with pytest.raises(LinkRefused) as info:
        await db.run(lambda c: registry.link_parent(c, adult.id, parent.id, NOW))

    assert str(info.value) == text.LINK_REFUSED_CHILD_TYPE
    assert await db.run(lambda c: registry.parents_of(c, adult.id)) == []


async def test_linking_a_child_as_the_parent_is_refused(db: Database) -> None:
    child = await make(db, display_name="Kid", account_type="child")
    other_child = await make(db, display_name="Other", account_type="child")

    with pytest.raises(LinkRefused) as info:
        await db.run(lambda c: registry.link_parent(c, other_child.id, child.id, NOW))

    assert str(info.value) == text.LINK_REFUSED_PARENT_TYPE
    assert await db.run(lambda c: registry.parents_of(c, other_child.id)) == []


async def test_changing_a_parent_to_child_type_is_refused(db: Database) -> None:
    parent = await make(db, display_name="Ann", account_type="parent")
    child = await make(db, display_name="Kid", account_type="child")
    await db.run(lambda c: registry.link_parent(c, child.id, parent.id, NOW))

    with pytest.raises(LinkRefused) as info:
        await db.run(lambda c: registry.update_person(
            c, parent.id, display_name="Ann", account_type="child", email=None, phone=None,
            fluxer_email_override=None))

    assert str(info.value) == text.TYPE_CHANGE_REFUSED_IS_PARENT


async def test_changing_a_child_with_parents_to_another_type_is_refused(db: Database) -> None:
    parent = await make(db, display_name="Ann", account_type="parent")
    child = await make(db, display_name="Kid", account_type="child")
    await db.run(lambda c: registry.link_parent(c, child.id, parent.id, NOW))

    with pytest.raises(LinkRefused) as info:
        await db.run(lambda c: registry.update_person(
            c, child.id, display_name="Kid", account_type="adult", email=None, phone=None,
            fluxer_email_override=None))

    assert str(info.value) == text.TYPE_CHANGE_REFUSED_HAS_PARENTS
    stored = await db.run(lambda c: registry.get_person(c, child.id))
    assert stored is not None
    assert stored.account_type == "child"


async def test_updating_a_missing_person_raises_no_such_person(db: Database) -> None:
    with pytest.raises(NoSuchPerson):
        await db.run(lambda c: registry.update_person(
            c, 999, display_name="Nobody", account_type="adult", email=None, phone=None,
            fluxer_email_override=None))


# AC2.7 (address part) -------------------------------------------------------------------------------

async def test_child_with_no_email_and_no_parent_email_has_no_address_and_is_flagged(db: Database) -> None:
    child, _ = await _child_with_parents(db, [None, None])
    assert await _address(db, child) is None

    flagged = await db.run(lambda c: registry.people_missing_fluxer_address(c))
    assert [p.id for p in flagged] == [child.id]


async def test_adults_without_email_are_not_flagged_as_missing_a_fluxer_address(db: Database) -> None:
    adult = await make(db, display_name="Adult", account_type="adult")
    flagged = await db.run(lambda c: registry.people_missing_fluxer_address(c))
    assert adult.id not in [p.id for p in flagged]


# Identity binding and enrollment refs --------------------------------------------------------------

async def test_enrollment_ref_matches_only_an_unbound_person(db: Database) -> None:
    person = await make(db, display_name="Kid", account_type="child")
    assert person.enrollment_ref is not None

    found = await db.run(lambda c: registry.get_by_enrollment_ref(c, person.enrollment_ref))
    assert found is not None and found.id == person.id

    await bind_child(db, person.id)
    assert await db.run(lambda c: registry.get_by_enrollment_ref(c, person.enrollment_ref)) is None
    bound = await db.run(lambda c: registry.get_by_uuid(c, CHILD_UUID))
    assert bound is not None
    assert bound.enrollment_ref is None
    assert bound.username == "kid"


async def test_set_invitation_stores_the_link_fields(db: Database) -> None:
    person = await make(db, display_name="Kid", account_type="child")
    await db.run(lambda c: registry.set_invitation(
        c, person.id, invitation_pk="inv-1", invite_url="https://hallpass.invalid/if/flow/x/?itoken=1",
        enrollment_ref="fresh-ref"))
    stored = await db.run(lambda c: registry.get_person(c, person.id))
    assert stored is not None
    assert stored.invitation_pk == "inv-1"
    assert stored.invite_url == "https://hallpass.invalid/if/flow/x/?itoken=1"
    assert stored.enrollment_ref == "fresh-ref"


# Locking --------------------------------------------------------------------------------------------

async def test_lock_then_unlock_restores_the_prior_status(db: Database) -> None:
    person = await make(db, display_name="Kid", account_type="child")
    locked = await db.run(lambda c: registry.set_locked(c, person.id, True))
    assert locked.status == "locked"
    assert locked.prior_status == "invited"

    twice = await db.run(lambda c: registry.set_locked(c, person.id, True))
    assert twice.prior_status == "invited"

    unlocked = await db.run(lambda c: registry.set_locked(c, person.id, False))
    assert unlocked.status == "invited"
    assert unlocked.prior_status is None


async def test_set_locked_on_missing_person_raises(db: Database) -> None:
    with pytest.raises(NoSuchPerson):
        await db.run(lambda c: registry.set_locked(c, 999, True))


# Decision log ---------------------------------------------------------------------------------------

async def test_log_decision_stores_the_row_with_json_columns(db: Database) -> None:
    person = await make(db, display_name="Kid", account_type="child")
    row_id = await db.run(lambda c: registry.log_decision(
        c, at=NOW, actor_kind="admin", actor_person_id=person.id, target_person_id=person.id,
        action="person_created", services=["fluxer"], detail={"k": 1}))

    row = await db.run(lambda c: c.execute(
        "SELECT at, actor_kind, action, services, detail, reversal FROM decision_log WHERE id = ?",
        (row_id,)).fetchone())
    assert row == ("2026-11-13T12:00:00+00:00", "admin", "person_created", '["fluxer"]', '{"k": 1}', 0)
