# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The personal page, the feed-URL generation and the parent's view of a child's services.

Covers hallpass.AC8.1, AC8.4, AC8.5, AC8.6 and AC8.7.
"""

from __future__ import annotations

import re

import httpx
import pytest

from hallpass import registry, services, text, tokens
from hallpass.registry import Person
from hallpass.web.routes_me import feed_link
from tests.helpers import (
    NOW,
    Env,
    api_routes,
    form,
    publish_terms,
    signed_in_person,
)
from tests.test_web_family import family

TOKEN_IN_URL = re.compile(r"token=([A-Za-z0-9_-]+)")
FEED_URL = "https://feeds.hallpass.test/feed.ics"
STEAM_ID = "".join(["7656", "1190", "00001"])   # a SteamID64 built from parts; the store wants digits


async def create_service(env: Env, slug: str, kind: str, *, actor: Person, auto: bool = False,
                         url: str | None = None) -> services.Service:
    return await env.db.run(lambda c: services.create_service(
        c, slug=slug, name=slug.title(), kind=kind, adapter="manual", auto_suspendable=auto, url=url,
        actor_id=actor.id, now=NOW))


async def grant(env: Env, service: services.Service, subject_kind: str, subject: str, actor: Person) -> None:
    await env.db.run(lambda c: services.add_entitlement(
        c, service.id, subject_kind, subject, "grant", actor_id=actor.id, now=NOW))


async def generate_feed(env: Env, browser: httpx.AsyncClient, service: services.Service) -> httpx.Response:
    token = (await form(env, "/", client=browser))["csrf"]
    return await browser.post(f"/tokens/feed/{service.id}", data={"csrf": token})


# AC8.1: each service with its status and suspendability ---------------------------------------------------------

async def test_the_personal_page_shows_each_service_with_status_and_suspendability(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    calendar = await create_service(env, "calendar", "forward_auth", actor=person, auto=True)
    valheim = await create_service(env, "valheim", "game", actor=person, auto=False)
    await grant(env, calendar, "type", "adult", person)
    await grant(env, valheim, "type", "adult", person)
    await env.db.run(lambda c: registry.add_identity(c, person.id, "valheim", "steam_id", STEAM_ID))

    page = (await browser.get("/")).text

    assert "Calendar" in page and "Valheim" in page
    assert page.count(text.STATUS_ALLOWED) == 2
    assert text.CAN_AUTO_SUSPEND in page
    assert text.CANNOT_AUTO_SUSPEND in page
    assert text.NO_GAME_IDENTITY not in page


async def test_a_suspended_service_shows_its_reasons(env: Env):
    # A child waiting for a parent's approval is entitled to the service but not active, so the row is suspended.
    parent, child, _, child_browser = await family(env)
    parent_person = await env.db.run(lambda c: registry.get_person(c, parent))
    calendar = await create_service(env, "calendar", "forward_auth", actor=parent_person)
    await grant(env, calendar, "type", "child", parent_person)

    page = (await child_browser.get("/")).text

    assert text.STATUS_SUSPENDED in page
    assert text.REASON_NOT_ACTIVE in page


# AC8.7: a game service without an identity -------------------------------------------------------------------

async def test_an_entitled_game_person_without_an_identity_sees_no_game_identity_on_file(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    valheim = await create_service(env, "valheim", "game", actor=person)
    await grant(env, valheim, "type", "adult", person)

    page = (await browser.get("/")).text

    assert text.NO_GAME_IDENTITY in page


# AC8.4: generating a feed URL ------------------------------------------------------------------------------------

async def test_a_generated_feed_url_is_shown_once_and_never_again(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    feed = await create_service(env, "cal-feed", "feed", actor=person, url=FEED_URL)
    await grant(env, feed, "type", "adult", person)

    shown = await generate_feed(env, browser, feed)

    assert shown.status_code == 200
    assert shown.headers["cache-control"] == "no-store"
    assert text.FEED_SHOWN_ONCE in shown.text
    assert shown.text.count("token=") == 1
    raw = TOKEN_IN_URL.search(shown.text).group(1)
    assert len(raw) >= 32

    reloaded = (await browser.get("/")).text
    assert raw not in reloaded
    assert "token=" not in reloaded


async def test_generating_again_shows_a_new_token_and_revokes_the_first(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    feed = await create_service(env, "cal-feed", "feed", actor=person, url=FEED_URL)
    await grant(env, feed, "type", "adult", person)
    first = TOKEN_IN_URL.search((await generate_feed(env, browser, feed)).text).group(1)

    second = TOKEN_IN_URL.search((await generate_feed(env, browser, feed)).text).group(1)

    assert second != first
    assert await env.db.run(lambda c: tokens.feed_owner(c, service_id=feed.id, raw=first)) is None
    assert await env.db.run(lambda c: tokens.feed_owner(c, service_id=feed.id, raw=second)) == person.id


async def test_a_person_not_entitled_to_the_feed_cannot_generate_one(env: Env):
    await publish_terms(env)
    person, _ = await signed_in_person(env, account_type="adult")
    feed = await create_service(env, "cal-feed", "feed", actor=person, url=FEED_URL)
    _, stranger = await signed_in_person(env, account_type="adult", display_name="Ana")

    refused = await generate_feed(env, stranger, feed)

    assert refused.status_code == 403
    assert "token=" not in refused.text


async def test_a_feed_service_with_no_address_issues_no_token(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    feed = await create_service(env, "cal-feed", "feed", actor=person, url=None)
    await grant(env, feed, "type", "adult", person)

    refused = await generate_feed(env, browser, feed)

    assert refused.status_code == 409
    assert text.FEED_NO_ADDRESS in refused.text
    assert await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM token").fetchone()[0]) == 0


# AC8.5: attached tokens appear only to people entitled to the service --------------------------------------------

async def test_a_service_wide_invite_link_shows_only_to_people_entitled_to_the_service(env: Env):
    await publish_terms(env)
    person, browser = await signed_in_person(env, account_type="adult")
    chat = await create_service(env, "fluxer", "oidc", actor=person)
    await grant(env, chat, "type", "adult", person)
    await env.db.run(lambda c: tokens.attach(c, service_id=chat.id, person_id=None, kind="invite_link",
                                             label="Fluxer invite", value="https://invite.hallpass.test/abc",
                                             now=NOW))
    _, outsider = await signed_in_person(env, account_type="child", display_name="Ana")

    assert "https://invite.hallpass.test/abc" in (await browser.get("/")).text
    assert "https://invite.hallpass.test/abc" not in (await outsider.get("/")).text


# AC8.6: nobody sees another person's tokens; a parent sees a child's services but not its tokens ----------------

async def test_a_person_cannot_see_another_persons_game_credential(env: Env):
    await publish_terms(env)
    owner, owner_browser = await signed_in_person(env, account_type="adult", display_name="Owner")
    other, other_browser = await signed_in_person(env, account_type="adult", display_name="Other")
    valheim = await create_service(env, "valheim", "game", actor=owner)
    await grant(env, valheim, "type", "adult", owner)
    await env.db.run(lambda c: tokens.attach(c, service_id=valheim.id, person_id=owner.id, kind="game_credential",
                                             label="login", value="owner-secret-value", now=NOW))

    assert "owner-secret-value" in (await owner_browser.get("/")).text
    assert "owner-secret-value" not in (await other_browser.get("/")).text


async def test_no_route_takes_a_person_id_outside_the_admin_and_children_routes(env: Env):
    for route in api_routes(env.application):
        if "GET" not in route.methods:
            continue
        if re.search(r"\{(person_id|child_id|child|person|owner)\w*\}", route.path):
            assert route.path.startswith(("/admin/", "/children/")), route.path


async def test_a_person_who_is_not_a_parent_gets_403_on_another_persons_child_page(env: Env):
    parent, child, _, _ = await family(env)
    _, stranger = await signed_in_person(env, account_type="adult", display_name="Ana")

    response = await stranger.get(f"/children/{child}")

    assert response.status_code == 403


async def test_a_parent_viewing_a_child_row_gets_neutral_wording_not_you(env: Env):
    # The child has an identity for the game but no entitlement, so the row is listed with its reason.
    parent, child, parent_browser, _ = await family(env)
    parent_person = await env.db.run(lambda c: registry.get_person(c, parent))
    await create_service(env, "valheim", "game", actor=parent_person)
    await env.db.run(lambda c: registry.add_identity(c, child, "valheim", "steam_id", STEAM_ID))

    response = await parent_browser.get(f"/children/{child}")

    assert response.status_code == 200
    assert "Valheim" in response.text
    assert text.REASON_NOT_ENTITLED in response.text
    assert "You are not entitled" not in response.text


@pytest.mark.parametrize("name", [name for name in dir(text) if name.startswith("REASON_")])
def test_no_reason_text_addresses_the_reader_as_you(name: str):
    # Reason texts appear on the parent's view of a child as well, so they must not say "you".
    assert re.search(r"\byou\b", getattr(text, name), re.IGNORECASE) is None


@pytest.mark.parametrize("base, expected", [
    # An existing query and a fragment: the token joins the query and the fragment stays last.
    ("https://feeds.hallpass.test/feed.ics?lang=en#top", "https://feeds.hallpass.test/feed.ics?lang=en&token=RAW#top"),
    # No query: the token starts one, and the fragment stays last.
    ("https://feeds.hallpass.test/feed.ics#top", "https://feeds.hallpass.test/feed.ics?token=RAW#top"),
    # A plain address is unchanged apart from the token.
    ("https://feeds.hallpass.test/feed.ics", "https://feeds.hallpass.test/feed.ics?token=RAW"),
    # A fragment with a question mark must not be taken for a query.
    ("https://feeds.hallpass.test/feed.ics#a?b", "https://feeds.hallpass.test/feed.ics?token=RAW#a?b"),
])
def test_the_feed_token_goes_in_the_query_and_never_after_a_fragment(base: str, expected: str):
    assert feed_link(base, "RAW") == expected


async def test_a_linked_parent_sees_the_childs_services_but_not_its_tokens(env: Env):
    parent, child, parent_browser, _ = await family(env)
    parent_person = await env.db.run(lambda c: registry.get_person(c, parent))
    valheim = await create_service(env, "valheim", "game", actor=parent_person)
    await grant(env, valheim, "type", "child", parent_person)
    await env.db.run(lambda c: tokens.attach(c, service_id=valheim.id, person_id=child, kind="game_credential",
                                             label="login", value="child-secret-value", now=NOW))

    response = await parent_browser.get(f"/children/{child}")

    assert response.status_code == 200
    assert "Valheim" in response.text
    assert "child-secret-value" not in response.text
    assert 'action="/tokens/feed' not in response.text
    assert "Tokens" not in response.text


