# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Token store tests against a real SQLite database under tmp_path.

Covers hallpass.AC8.4 (store) and hallpass.AC8.5 (visibility rule).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hallpass import registry, services, tokens
from hallpass.db import Database
from hallpass.registry import Person
from hallpass.services import Service
from tests.helpers import make_service

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
INVITE = "https://invite.invalid/if/flow/join/?itoken=abc123"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hp.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def admin(db: Database) -> Person:
    return await db.run(lambda c: registry.create_person(c, display_name="Admin", account_type="adult", now=NOW))


async def make_person(db: Database, *, account_type: str = "adult", name: str = "Ann") -> Person:
    return await db.run(lambda c: registry.create_person(c, display_name=name, account_type=account_type, now=NOW))


async def entitle(db: Database, admin: Person, service: Service, subject_kind: str, subject: str) -> services.Entitlement:
    return await db.run(lambda c: services.add_entitlement(
        c, service.id, subject_kind, subject, "grant", actor_id=admin.id, now=NOW))


async def issue(db: Database, person: Person, service: Service) -> str:
    return await db.run(lambda c: tokens.issue_feed(c, person_id=person.id, service_id=service.id, now=NOW))


# AC8.4 (store) --------------------------------------------------------------------------------------

async def test_issued_feed_token_is_at_least_32_characters_and_stored_as_its_hash_only(db: Database,
                                                                                       admin: Person) -> None:
    person = await make_person(db)
    feed = await make_service(db, "cal-feed", kind="feed")

    raw = await issue(db, person, feed)

    assert len(raw) >= 32
    row = await db.run(lambda c: c.execute(
        "SELECT hash, value, kind, person_id FROM token WHERE service_id = ?", (feed.id,)).fetchone())
    assert row == (tokens.hash_token(raw), None, "feed", person.id)


async def test_the_raw_token_appears_nowhere_in_the_database_files(db: Database, tmp_path: Path) -> None:
    person = await make_person(db)
    feed = await make_service(db, "cal-feed", kind="feed")
    raw = await issue(db, person, feed)

    await db.checkpoint()
    for name in ("hp.db", "hp.db-wal"):
        path = tmp_path / name
        if path.exists():
            assert raw.encode() not in path.read_bytes(), name


async def test_issuing_again_revokes_the_first_token(db: Database, admin: Person) -> None:
    person = await make_person(db)
    feed = await make_service(db, "cal-feed", kind="feed")
    first = await issue(db, person, feed)
    second = await issue(db, person, feed)

    assert await db.run(lambda c: tokens.feed_owner(c, service_id=feed.id, raw=first)) is None
    assert await db.run(lambda c: tokens.feed_owner(c, service_id=feed.id, raw=second)) == person.id
    revoked = await db.run(lambda c: c.execute(
        "SELECT COUNT(*) FROM token WHERE service_id = ? AND revoked_at IS NOT NULL", (feed.id,)).fetchone()[0])
    assert revoked == 1


async def test_feed_owner_is_scoped_to_its_service_and_refuses_unknown_tokens(db: Database) -> None:
    person = await make_person(db)
    feed = await make_service(db, "cal-feed", kind="feed")
    other = await make_service(db, "other-feed", kind="feed")
    raw = await issue(db, person, feed)

    assert await db.run(lambda c: tokens.feed_owner(c, service_id=other.id, raw=raw)) is None
    assert await db.run(lambda c: tokens.feed_owner(c, service_id=feed.id, raw="not-a-token")) is None


async def test_issuing_writes_a_decision_row_that_does_not_carry_the_token(db: Database) -> None:
    person = await make_person(db)
    feed = await make_service(db, "cal-feed", kind="feed")
    raw = await issue(db, person, feed)

    rows = await db.run(lambda c: c.execute(
        "SELECT action, actor_kind, target_person_id, services, detail FROM decision_log "
        "WHERE action = 'feed_token_issued'").fetchall())
    assert rows == [("feed_token_issued", "person", person.id, '["cal-feed"]', '{"revoked": 0}')]
    assert raw not in repr(rows)


async def test_feed_services_for_lists_only_entitled_feed_services(db: Database, admin: Person) -> None:
    person = await make_person(db)
    mine = await make_service(db, "cal-feed", kind="feed")
    await make_service(db, "other-feed", kind="feed")
    game = await make_service(db, "valheim", kind="game")
    await entitle(db, admin, mine, "type", "adult")
    await entitle(db, admin, game, "type", "adult")

    found = await db.run(lambda c: tokens.feed_services_for(c, registry.get_person(c, person.id)))
    assert [s.slug for s in found] == ["cal-feed"]


# AC8.5 (visibility) ---------------------------------------------------------------------------------

async def test_service_wide_invite_link_is_visible_to_an_entitled_person_and_not_to_others(
        db: Database, admin: Person) -> None:
    ann = await make_person(db, name="Ann")
    bob = await make_person(db, account_type="child", name="Bob")
    calendar = await make_service(db, "calendar")
    await entitle(db, admin, calendar, "type", "adult")
    await db.run(lambda c: tokens.attach(c, service_id=calendar.id, person_id=None, kind="invite_link",
                                         label="Join", value=INVITE, now=NOW))

    ann_now = await db.run(lambda c: registry.get_person(c, ann.id))
    bob_now = await db.run(lambda c: registry.get_person(c, bob.id))
    assert [t.value for t in await db.run(lambda c: tokens.visible_to(c, ann_now))] == [INVITE]
    assert await db.run(lambda c: tokens.visible_to(c, bob_now)) == []


async def test_per_person_game_credential_is_visible_only_to_its_person_while_entitled(
        db: Database, admin: Person) -> None:
    ann = await make_person(db, name="Ann")
    bob = await make_person(db, name="Bob")
    valheim = await make_service(db, "valheim", kind="game")
    ann_grant = await entitle(db, admin, valheim, "person", str(ann.id))
    await entitle(db, admin, valheim, "person", str(bob.id))
    await db.run(lambda c: tokens.attach(c, service_id=valheim.id, person_id=ann.id, kind="game_credential",
                                         label="Login", value="ann-login-value", now=NOW))

    ann_now = await db.run(lambda c: registry.get_person(c, ann.id))
    bob_now = await db.run(lambda c: registry.get_person(c, bob.id))
    assert [t.value for t in await db.run(lambda c: tokens.visible_to(c, ann_now))] == ["ann-login-value"]
    assert await db.run(lambda c: tokens.visible_to(c, bob_now)) == []

    await db.run(lambda c: services.remove_entitlement(c, ann_grant.id, actor_id=admin.id, now=NOW))
    assert await db.run(lambda c: tokens.visible_to(c, ann_now)) == []


async def test_revoked_feed_and_retired_service_tokens_are_not_listed(db: Database, admin: Person) -> None:
    ann = await make_person(db)
    calendar = await make_service(db, "calendar")
    await entitle(db, admin, calendar, "type", "adult")
    token_id = await db.run(lambda c: tokens.attach(c, service_id=calendar.id, person_id=None,
                                                    kind="invite_link", label="Join", value=INVITE, now=NOW))
    feed = await make_service(db, "cal-feed", kind="feed")
    await entitle(db, admin, feed, "type", "adult")
    await issue(db, ann, feed)
    ann_now = await db.run(lambda c: registry.get_person(c, ann.id))
    assert [t.value for t in await db.run(lambda c: tokens.visible_to(c, ann_now))] == [INVITE]

    await db.run(lambda c: tokens.revoke(c, token_id, NOW))
    assert await db.run(lambda c: tokens.visible_to(c, ann_now)) == []

    await db.run(lambda c: tokens.attach(c, service_id=calendar.id, person_id=None, kind="invite_link",
                                         label="Join again", value=INVITE, now=NOW))
    await db.run(lambda c: services.retire_service(c, calendar.id, actor_id=admin.id, now=NOW))
    assert await db.run(lambda c: tokens.visible_to(c, ann_now)) == []


async def test_attach_refuses_an_unknown_kind_and_an_empty_value(db: Database) -> None:
    calendar = await make_service(db, "calendar")
    with pytest.raises(ValueError, match="unknown token kind"):
        await db.run(lambda c: tokens.attach(c, service_id=calendar.id, person_id=None, kind="feed",
                                             label="", value="x", now=NOW))
    with pytest.raises(ValueError, match="token value is required"):
        await db.run(lambda c: tokens.attach(c, service_id=calendar.id, person_id=None, kind="api_key",
                                             label="", value="  ", now=NOW))
