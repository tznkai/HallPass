# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registry store tests against a real SQLite database under tmp_path."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from hallpass import registry, text
from hallpass.db import Database
from hallpass.registry import LinkRefused, NoSuchPerson
from tests.helpers import make_service

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
# Built from parts so no tracked file holds a 15+ digit run. A SteamID64 shape, not a real account.
STEAM_ID = str(registry.STEAM64_BASE + 1001)  # a canonical SteamID64 built from the base, never written out
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
    for slug in ("fluxer", "minecraft", "minecraft-bedrock", "steam"):
        await make_service(db, slug)

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


async def test_duplicate_identity_for_the_same_person_raises_integrity_error(db: Database) -> None:
    person = await make(db, display_name="First", account_type="child")
    await make_service(db, "minecraft")
    await db.run(lambda c: registry.add_identity(c, person.id, "minecraft", "java_name", "kidplayer"))
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: registry.add_identity(c, person.id, "minecraft", "java_name", "kidplayer"))


@pytest.mark.parametrize("kind", ["java_name", "bedrock_gamertag", "steam_id"])
async def test_the_same_identity_may_be_held_by_two_people(db: Database, kind: str) -> None:
    value = STEAM_ID if kind == "steam_id" else "PlayerOne"
    service = "steam" if kind == "steam_id" else "minecraft"
    one = await make(db, display_name="One", account_type="child")
    two = await make(db, display_name="Two", account_type="child")
    await make_service(db, service)
    await db.run(lambda c: registry.add_identity(c, one.id, service, kind, value))
    await db.run(lambda c: registry.add_identity(c, two.id, service, kind, value))
    assert [(i.kind, i.value) for i in await db.run(lambda c: registry.identities_of(c, two.id))] == [(kind, value)]


@pytest.mark.parametrize("kind", ["java_name", "bedrock_gamertag"])
@pytest.mark.parametrize("first, second", [("PlayerOne", "playerone"), ("playerone", "PlayerOne")],
                         ids=["upper-then-lower", "lower-then-upper"])
async def test_a_case_variant_minecraft_name_may_be_held_by_another_person_with_its_case_kept(
        db: Database, kind: str, first: str, second: str) -> None:
    one = await make(db, display_name="One", account_type="child")
    two = await make(db, display_name="Two", account_type="child")
    await make_service(db, "minecraft")
    await db.run(lambda c: registry.add_identity(c, one.id, "minecraft", kind, first))
    await db.run(lambda c: registry.add_identity(c, two.id, "minecraft", kind, second))
    stored_one = await db.run(lambda c: registry.identities_of(c, one.id))
    stored_two = await db.run(lambda c: registry.identities_of(c, two.id))
    assert [(i.kind, i.value) for i in stored_one] == [(kind, first)]
    assert [(i.kind, i.value) for i in stored_two] == [(kind, second)]


async def test_a_fluxer_user_id_held_by_another_person_is_refused(db: Database) -> None:
    one = await make(db, display_name="One", account_type="child")
    two = await make(db, display_name="Two", account_type="child")
    await make_service(db, "fluxer")
    await db.run(lambda c: registry.add_identity(c, one.id, "fluxer", "fluxer_user_id", "1001"))
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: registry.add_identity(c, two.id, "fluxer", "fluxer_user_id", "1001"))
    assert await db.run(lambda c: registry.identities_of(c, two.id)) == []


async def test_other_holders_names_the_other_people_who_hold_the_same_identity(db: Database) -> None:
    one = await make(db, display_name="One", account_type="child")
    two = await make(db, display_name="Two", account_type="child")
    three = await make(db, display_name="Three", account_type="child")
    await make_service(db, "minecraft")
    await db.run(lambda c: registry.add_identity(c, one.id, "minecraft", "java_name", "PlayerOne"))
    await db.run(lambda c: registry.add_identity(c, two.id, "minecraft", "java_name", "playerone"))
    await db.run(lambda c: registry.add_identity(c, three.id, "minecraft", "java_name", "Other"))

    def holders(conn) -> dict[str, list[int]]:
        return {i.value: [p.id for p in registry.other_holders(conn, i)]
                for i in registry.identities_of(conn, one.id) + registry.identities_of(conn, three.id)}

    assert await db.run(holders) == {"PlayerOne": [two.id], "Other": []}


@pytest.mark.parametrize("kind", ["java_name", "bedrock_gamertag"])
async def test_a_case_variant_minecraft_name_on_the_same_person_is_refused(db: Database, kind: str) -> None:
    person = await make(db, display_name="One", account_type="child")
    await make_service(db, "minecraft")
    await db.run(lambda c: registry.add_identity(c, person.id, "minecraft", kind, "PlayerOne"))
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: registry.add_identity(c, person.id, "minecraft", kind, "playerone"))
    stored = await db.run(lambda c: registry.identities_of(c, person.id))
    assert [(i.kind, i.value) for i in stored] == [(kind, "PlayerOne")]


@pytest.mark.parametrize("kind", ["java_name", "bedrock_gamertag"])
async def test_a_minecraft_name_is_stored_with_the_case_it_was_entered(db: Database, kind: str) -> None:
    person = await make(db, display_name="One", account_type="child")
    await make_service(db, "minecraft")
    await db.run(lambda c: registry.add_identity(c, person.id, "minecraft", kind, "PlayerOne"))
    stored = await db.run(lambda c: registry.identities_of(c, person.id))
    assert [(i.kind, i.value) for i in stored] == [(kind, "PlayerOne")]


async def test_a_fluxer_user_id_is_compared_without_case_folding(db: Database) -> None:
    one = await make(db, display_name="One", account_type="child")
    two = await make(db, display_name="Two", account_type="child")
    await make_service(db, "fluxer")
    await db.run(lambda c: registry.add_identity(c, one.id, "fluxer", "fluxer_user_id", "AbC"))
    await db.run(lambda c: registry.add_identity(c, two.id, "fluxer", "fluxer_user_id", "abc"))
    first = await db.run(lambda c: registry.identities_of(c, one.id))
    second = await db.run(lambda c: registry.identities_of(c, two.id))
    assert [i.value for i in first + second] == ["AbC", "abc"]


@pytest.mark.parametrize("kind, value, expected", [
    ("java_name", "PlayerOne", "playerone"),
    ("bedrock_gamertag", "PlayerOne", "playerone"),
    ("fluxer_user_id", "PlayerOne", "PlayerOne"),
    ("steam_id", "1001", "1001"),
])
def test_identity_key_lowers_only_minecraft_names(kind: str, value: str, expected: str) -> None:
    assert registry.identity_key(kind, value) == expected


async def test_identity_for_an_unknown_service_is_refused_and_stores_nothing(db: Database) -> None:
    person = await make(db, account_type="child")
    with pytest.raises(ValueError, match="unknown service"):
        await db.run(lambda c: registry.add_identity(c, person.id, "fluxer", "fluxer_user_id", "1001"))
    assert await db.run(lambda c: registry.identities_of(c, person.id)) == []


async def test_non_digit_steam_id_is_refused(db: Database) -> None:
    person = await make(db, account_type="child")
    await make_service(db, "steam")
    with pytest.raises(ValueError, match="steam_id must be digits"):
        await db.run(lambda c: registry.add_identity(c, person.id, "steam", "steam_id", "not-digits"))
    assert await db.run(lambda c: registry.identities_of(c, person.id)) == []


@pytest.mark.parametrize("value", [
    "0" + STEAM_ID,                       # a leading zero: the game never matches this form
    "00" + STEAM_ID,
    str(registry.STEAM64_BASE - 1),       # one below the individual range
    str(registry.STEAM64_LIMIT),          # one past the individual range
    "9" * 25,                             # too long to be an id
    "1" * 5000,                           # far too long, and refused without converting it
])
async def test_a_non_canonical_steam_id_is_refused_and_stores_nothing(db: Database, value: str) -> None:
    person = await make(db, account_type="child")
    await make_service(db, "steam")
    with pytest.raises(ValueError, match="steam_id must be a SteamID64"):
        await db.run(lambda c: registry.add_identity(c, person.id, "steam", "steam_id", value))
    assert await db.run(lambda c: registry.identities_of(c, person.id)) == []


@pytest.mark.parametrize("offset", [0, (1 << 32) - 1])
async def test_steam_ids_at_both_ends_of_the_individual_range_are_stored(db: Database, offset: int) -> None:
    person = await make(db, account_type="child")
    await make_service(db, "steam")
    value = str(registry.STEAM64_BASE + offset)
    await db.run(lambda c: registry.add_identity(c, person.id, "steam", "steam_id", value))
    stored = await db.run(lambda c: registry.identities_of(c, person.id))
    assert [(i.kind, i.value) for i in stored] == [("steam_id", value)]


async def test_unknown_identity_kind_is_refused(db: Database) -> None:
    person = await make(db, account_type="child")
    with pytest.raises(ValueError, match="unknown identity kind"):
        await db.run(lambda c: registry.add_identity(c, person.id, "steam", "discord", "1"))


# A Minecraft name reaches a console command, so a java_name or bedrock_gamertag must match its pattern in full.
# A selector such as @a, an over-long name, a space, a non-ASCII space and punctuation are all refused.
MINECRAFT_BAD_NAMES = ["", "Ste ve", "Ste\nve", "@a", "@e[type=player]", "@p", "@r", "A" * 17, "Ste\u00a0ve",
                       "Steve;op", "Steve/op", "Steve[x]"]


@pytest.mark.parametrize("kind", ["java_name", "bedrock_gamertag"])
@pytest.mark.parametrize("value", MINECRAFT_BAD_NAMES)
async def test_a_minecraft_name_outside_the_pattern_is_refused_and_nothing_is_stored(db: Database, kind: str,
                                                                                     value: str) -> None:
    person = await make(db, account_type="child")
    await make_service(db, "minecraft")
    with pytest.raises(ValueError):
        await db.run(lambda c: registry.add_identity(c, person.id, "minecraft", kind, value))
    assert await db.run(lambda c: registry.identities_of(c, person.id)) == []


@pytest.mark.parametrize("kind", ["java_name", "bedrock_gamertag"])
@pytest.mark.parametrize("value", ["Steve", "kid_player_9", "A" * 16])
async def test_a_minecraft_name_inside_the_pattern_is_stored(db: Database, kind: str, value: str) -> None:
    person = await make(db, account_type="child")
    await make_service(db, "minecraft")
    await db.run(lambda c: registry.add_identity(c, person.id, "minecraft", kind, value))
    stored = await db.run(lambda c: registry.identities_of(c, person.id))
    assert [(i.kind, i.value) for i in stored] == [(kind, value)]


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


# Display names are one line of plain text: control characters are refused ---------------------------

@pytest.mark.parametrize("name", ["Ann\r\nBcc: x@example.com", "Ann\tB", "Ann\x00B", "Ann\u0085B",
                                  "Ann\u2028B", "Ann\u2029B"])
async def test_a_display_name_with_a_control_character_is_refused_and_nothing_is_stored(db: Database, name: str) -> None:
    with pytest.raises(ValueError):
        await db.run(lambda c: registry.create_person(c, display_name=name, account_type="adult", now=NOW))
    assert await db.run(registry.list_people) == []


def test_every_display_name_the_registry_accepts_is_a_valid_email_subject() -> None:
    candidates = ["Ann", "Ann B", "Ann\u00a0B", "Ann\u00e9", "  Ann  ", "Ann\u2028B", "Ann\u2029B", "Ann\r\nB"]
    accepted = []
    for candidate in candidates:
        try:
            accepted.append(registry._display_name(candidate))
        except ValueError:
            continue
    assert "Ann" in accepted and "Ann\u00a0B" in accepted  # the check below must cover real names
    for name in accepted:
        for template in (text.CHANGE_EMAIL_SUBJECT, text.LOCKDOWN_EMAIL_SUBJECT):
            msg = EmailMessage()
            msg["Subject"] = template.format(child=name)  # raises ValueError on a separator or control character
            assert msg["Subject"] == template.format(child=name)


async def test_an_update_to_a_name_with_a_control_character_is_refused_and_changes_nothing(db: Database) -> None:
    person = await db.run(lambda c: registry.create_person(c, display_name="Ann", account_type="adult", now=NOW))
    with pytest.raises(ValueError):
        await db.run(lambda c: registry.update_person(c, person.id, display_name="Ann\r\nX", account_type="adult",
                                                       email=None, phone=None, fluxer_email_override=None))
    assert (await db.run(lambda c: registry.get_person(c, person.id))).display_name == "Ann"
