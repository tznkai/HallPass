# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Palworld REST adapter (AC12.2): bans and unbans through the REST API, and lifts only the bans it placed.

Every test runs against fakes on httpx MockTransport. No test reaches a real server, and no test lifts a ban that the
adapter did not place.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from hallmonitor import own
from hallmonitor.adapters.base import Capabilities, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.palworld import PalworldAdapter
from hallmonitor.adapters.registry import build_adapter
from hallmonitor.adapters.steam import STEAM64_BASE
from hallmonitor.client import MonitorClient
from hallmonitor.config import Config
from hallmonitor.contract import Identity, Subject
from hallmonitor.db import Database
from hallmonitor.reconcile import cycle
from tests.fake_hallpass import TOKEN, FakeHallpassState
from tests.fake_hallpass import make_transport as make_hallpass_transport
from tests.fake_palworld import BANLIST_PATH, PALWORLD_PASSWORD, REST_PREFIX, SERVER, FakePalworldState
from tests.fake_palworld import make_transport as make_palworld_transport
from tests.fake_palworld import moderator_ban
from tests.fake_ptero_files import PTERO_KEY, FakePteroState

URL = "https://fake.test"
SLUG = "pal"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
OWN_ID = str(STEAM64_BASE + 1001)
OTHER_ID = str(STEAM64_BASE + 1002)
SETTINGS = {
    "rest_url": URL,
    "admin_password": PALWORLD_PASSWORD,
    "ptero_url": URL,
    "ptero_key": PTERO_KEY,
    "ptero_server": SERVER,
    "banlist_path": BANLIST_PATH,
}
SEEN: list[FakePalworldState] = []


@pytest.fixture(autouse=True)
def no_unban_without_a_ban_we_placed() -> Iterator[None]:
    """No unban may reach the fake for an id the ban route did not accept from this adapter."""
    SEEN.clear()
    yield
    for state in SEEN:
        placed = set(state.bans_applied)
        for _, path, body in state.requests:
            if path == f"{REST_PREFIX}unban":
                assert isinstance(body, dict) and body.get("userid") in placed, \
                    "the Palworld adapter must never unban an id it did not ban"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hallmonitor.db")
    try:
        yield database
    finally:
        database.close()


def pal_state(**fields: Any) -> FakePalworldState:
    state = FakePalworldState(**fields)
    SEEN.append(state)
    return state


def steam(value: str) -> dict[str, str]:
    return {"kind": "steam_id", "value": value}


def subject(ref: str, desired: str, *identities: dict[str, str], generation: int = 1) -> dict[str, Any]:
    return {"ref": ref, "identities": list(identities), "desired": desired, "generation": generation}


def pal_service(*subjects: dict[str, Any]) -> FakeHallpassState:
    return FakeHallpassState(services=[{"slug": SLUG, "adapter": "palworld", "subjects": list(subjects)}])


def outcomes(hallpass: FakeHallpassState) -> list[tuple[str, str, str | None]]:
    return [(entry["ref"], entry["observed"], entry["detail"]) for entry in hallpass.reports]


def ctx(db: Database, http: httpx.AsyncClient, settings: dict[str, str] = SETTINGS) -> AdapterContext:
    return AdapterContext(slug=SLUG, settings=settings, config=Config(hallpass_url=URL, hallpass_token=TOKEN),
                          db=db, http=http, clock=lambda: NOW)


def palworld(db: Database, http: httpx.AsyncClient) -> PalworldAdapter:
    adapter = build_adapter("palworld", ctx(db, http))
    assert isinstance(adapter, PalworldAdapter)
    return adapter


async def run(db: Database, hallpass: FakeHallpassState, pal: FakePalworldState, ptero: FakePteroState) -> None:
    async with httpx.AsyncClient(transport=make_palworld_transport(pal, ptero)) as http:
        adapter = palworld(db, http)
        client = MonitorClient(URL, TOKEN, transport=make_hallpass_transport(hallpass))
        try:
            await cycle(client=client, adapter_for=lambda s: adapter if s.slug == SLUG else None, now=NOW)
        finally:
            await client.aclose()


def banlist(text: str = "") -> FakePteroState:
    """A Pterodactyl fake whose server holds the ban-list file with this text."""
    return FakePteroState(files={(SERVER, BANLIST_PATH): text})


def file_text(ptero: FakePteroState) -> str:
    return ptero.files.get((SERVER, BANLIST_PATH), "")


def rest_calls(pal: FakePalworldState) -> list[tuple[str, str]]:
    return [(method, path) for method, path, _ in pal.requests]


async def record_of(db: Database, identity: str) -> str | None:
    found = await db.run(lambda c: own.own_record(c, SLUG, identity))
    return None if found is None else found[0]


# AC12.2: a suspension bans through the REST API and records the ban as ours ---------------------------

async def test_a_suspension_bans_through_rest_and_records_the_ban(db: Database) -> None:
    pal, ptero = pal_state(), banlist()
    hallpass = pal_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    await run(db, hallpass, pal, ptero)
    assert rest_calls(pal) == [("POST", f"{REST_PREFIX}ban")]
    assert pal.requests[0][2]["userid"] == f"steam_{OWN_ID}"
    assert isinstance(pal.requests[0][2]["message"], str) and pal.requests[0][2]["message"]
    assert file_text(ptero) == f"steam_{OWN_ID}\n"
    assert await db.run(lambda c: own.own_record(c, SLUG, OWN_ID)) == ("ban", NOW)
    assert outcomes(hallpass) == [("r1", "suspended", None)]


async def test_a_steady_suspension_sends_no_second_ban(db: Database) -> None:
    pal, ptero = pal_state(), banlist()
    hallpass = pal_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, pal, ptero)
    await run(db, hallpass, pal, ptero)
    assert rest_calls(pal) == [("POST", f"{REST_PREFIX}ban")]
    assert outcomes(hallpass) == [("r1", "suspended", None), ("r1", "suspended", None)]


# AC12.2: lifting our own ban unbans through REST, forgets the record, and reports allowed -----------

async def test_lifting_our_own_ban_unbans_and_forgets_the_record(db: Database) -> None:
    pal, ptero = pal_state(), banlist()
    hallpass = pal_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    await run(db, hallpass, pal, ptero)
    hallpass.services[0]["subjects"] = [subject("r1", "allowed", steam(OWN_ID), generation=4)]
    await run(db, hallpass, pal, ptero)
    assert rest_calls(pal) == [("POST", f"{REST_PREFIX}ban"), ("POST", f"{REST_PREFIX}unban")]
    assert pal.requests[1][2] == {"userid": f"steam_{OWN_ID}"}
    assert file_text(ptero) == ""
    assert await db.run(lambda c: own.own_record(c, SLUG, OWN_ID)) is None
    assert outcomes(hallpass) == [("r1", "suspended", None), ("r1", "allowed", None)]


# AC12.2: a ban someone else placed is banned_external and is never lifted ------------------------------

async def test_a_moderator_ban_is_never_lifted_when_the_desired_state_is_allowed(db: Database) -> None:
    pal, ptero = pal_state(), banlist()
    moderator_ban(ptero, f"steam_{OTHER_ID}")
    hallpass = pal_service(subject("r1", "allowed", steam(OTHER_ID), generation=2))
    await run(db, hallpass, pal, ptero)
    assert pal.requests == []
    assert file_text(ptero) == f"steam_{OTHER_ID}\n"
    assert outcomes(hallpass) == [("r1", "banned_external", None)]


async def test_a_moderator_ban_is_left_alone_when_the_desired_state_is_suspended(db: Database) -> None:
    pal, ptero = pal_state(), banlist()
    moderator_ban(ptero, f"steam_{OTHER_ID}")
    hallpass = pal_service(subject("r1", "suspended", steam(OTHER_ID), generation=2))
    await run(db, hallpass, pal, ptero)
    assert pal.requests == []
    assert outcomes(hallpass) == [("r1", "banned_external", None)]
    assert await db.run(lambda c: own.own_record(c, SLUG, OTHER_ID)) is None


async def test_a_ban_placed_by_a_moderator_after_our_observe_is_not_lifted(db: Database) -> None:
    """The apply re-reads the ban list, so a ban that appears between observe and apply is never treated as ours."""
    pal, ptero = pal_state(), banlist()
    async with httpx.AsyncClient(transport=make_palworld_transport(pal, ptero)) as http:
        adapter = palworld(db, http)
        moderator_ban(ptero, f"steam_{OWN_ID}")
        result = await adapter.apply(Subject("r1", (Identity("steam_id", OWN_ID),), "suspended", 2), "suspended")
    assert result == Observed("banned_external")
    assert pal.requests == []
    assert await db.run(lambda c: own.own_record(c, SLUG, OWN_ID)) is None


async def test_a_record_whose_ban_is_gone_from_the_file_is_forgotten_without_an_unban(db: Database) -> None:
    pal, ptero = pal_state(), banlist()
    hallpass = pal_service(subject("r1", "suspended", steam(OWN_ID), generation=3))
    await run(db, hallpass, pal, ptero)
    ptero.files[(SERVER, BANLIST_PATH)] = ""
    sent = len(pal.requests)
    hallpass.services[0]["subjects"] = [subject("r1", "allowed", steam(OWN_ID), generation=4)]
    await run(db, hallpass, pal, ptero)
    assert len(pal.requests) == sent
    assert await db.run(lambda c: own.own_record(c, SLUG, OWN_ID)) is None
    assert outcomes(hallpass)[-1] == ("r1", "allowed", None)


# Identity: a missing or malformed steam_id is refused before any request or file write -----------------

async def test_a_missing_ban_list_file_fails_the_service_with_not_found(db: Database) -> None:
    pal = pal_state()
    hallpass = pal_service(subject("r1", "suspended", steam(OWN_ID)))
    await run(db, hallpass, pal, FakePteroState())
    assert pal.requests == []
    assert outcomes(hallpass) == [("r1", "error", "not_found")]


async def test_a_subject_without_a_steam_id_is_no_identity_and_sends_nothing(db: Database) -> None:
    pal, ptero = pal_state(), banlist()
    hallpass = pal_service(subject("r1", "suspended"))
    await run(db, hallpass, pal, ptero)
    assert pal.requests == []
    assert outcomes(hallpass) == [("r1", "error", "no_identity")]


@pytest.mark.parametrize("value", ["12a", "", "+" + OWN_ID, str(STEAM64_BASE - 2), OWN_ID + " ", "9" * 25])
async def test_a_malformed_steam_id_is_bad_identity_and_nothing_is_sent_or_written(db: Database, value: str) -> None:
    pal, ptero = pal_state(), banlist()
    hallpass = pal_service(subject("r1", "suspended", steam(value)))
    await run(db, hallpass, pal, ptero)
    assert pal.requests == []
    assert [method for method, _ in ptero.requests if method != "GET"] == []
    assert outcomes(hallpass) == [("r1", "error", "bad_identity")]


# Errors: auth and unreachable are service-wide, so one failure stops the service's remaining applies ---

async def test_a_wrong_admin_password_is_an_auth_error_and_stops_the_remaining_applies(db: Database) -> None:
    pal, ptero = pal_state(password="not-the-password"), banlist()
    hallpass = pal_service(subject("r1", "suspended", steam(OWN_ID)),
                           subject("r2", "suspended", steam(OTHER_ID)))
    await run(db, hallpass, pal, ptero)
    assert len(pal.requests) == 1
    assert outcomes(hallpass) == [("r1", "error", "auth"), ("r2", "error", "auth")]
    assert await db.run(lambda c: own.own_record(c, SLUG, OWN_ID)) is None


async def test_pterodactyl_unreachable_fails_every_subject_and_sends_no_rest_call(db: Database) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    pal = pal_state()
    hallpass = pal_service(subject("r1", "suspended", steam(OWN_ID)), subject("r2", "allowed", steam(OTHER_ID)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        adapter = palworld(db, http)
        client = MonitorClient(URL, TOKEN, transport=make_hallpass_transport(hallpass))
        try:
            await cycle(client=client, adapter_for=lambda s: adapter, now=NOW)
        finally:
            await client.aclose()
    assert pal.requests == []
    assert outcomes(hallpass) == [("r1", "error", "unreachable"), ("r2", "error", "unreachable")]


# Capabilities, unmanaged entries, and the registry ----------------------------------------------------

async def test_the_palworld_capabilities_claim_suspend_restore_read_and_connected_removal(db: Database) -> None:
    async with httpx.AsyncClient(transport=make_palworld_transport(pal_state(), banlist())) as http:
        assert palworld(db, http).capabilities == Capabilities(
            suspend=True, restore=True, read_state=True, removes_connected=True)


async def test_unmanaged_counts_the_entries_that_match_no_subject(db: Database) -> None:
    pal, ptero = pal_state(), banlist()
    moderator_ban(ptero, f"steam_{OTHER_ID}")
    moderator_ban(ptero, "steam_" + str(STEAM64_BASE + 1003))
    async with httpx.AsyncClient(transport=make_palworld_transport(pal, ptero)) as http:
        adapter = palworld(db, http)
        known = [Subject("r1", (Identity("steam_id", OTHER_ID),), "allowed", 1)]
        assert await adapter.unmanaged_count(known) == 1


@pytest.mark.parametrize("missing", sorted(SETTINGS))
async def test_the_registry_needs_every_palworld_setting(db: Database, caplog: pytest.LogCaptureFixture,
                                                         missing: str) -> None:
    settings = {key: value for key, value in SETTINGS.items() if key != missing}
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.INFO):
            assert build_adapter("palworld", ctx(db, http, settings)) is None
    assert [r.getMessage() for r in caplog.records].count("adapter_not_configured") == 1


async def test_the_registry_builds_the_adapter_when_every_setting_is_present(db: Database) -> None:
    async with httpx.AsyncClient() as http:
        assert isinstance(build_adapter("palworld", ctx(db, http)), PalworldAdapter)


async def test_apply_refuses_to_unban_a_ban_nobody_here_placed(db: Database) -> None:
    """Reconcile never applies to a banned_external subject, so the apply's own refusal is checked directly."""
    pal, ptero = pal_state(), banlist()
    moderator_ban(ptero, f"steam_{OTHER_ID}")
    async with httpx.AsyncClient(transport=make_palworld_transport(pal, ptero)) as http:
        adapter = palworld(db, http)
        result = await adapter.apply(Subject("r1", (Identity("steam_id", OTHER_ID),), "allowed", 2), "allowed")
    assert result == Observed("banned_external")
    assert pal.requests == []
    assert file_text(ptero) == f"steam_{OTHER_ID}\n"
