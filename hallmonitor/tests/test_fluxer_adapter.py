# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Fluxer adapter (AC10.3 to AC10.5, as amended: holds end sessions, and no ban is ever placed or lifted)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from hallmonitor.adapters.base import AdapterError, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.fluxer import DISABLED, FluxerAdapter
from hallmonitor.adapters.registry import build_adapter
from hallmonitor.client import MonitorClient
from hallmonitor.config import Config
from hallmonitor.contract import Identity, Subject
from hallmonitor.db import Database
from hallmonitor.logs import JsonFormatter
from hallmonitor.reconcile import cycle
from tests.fake_fluxer_admin import (
    DECOY_EMAIL,
    DECOY_IP,
    FLUXER_KEY,
    FakeFluxerAdminState,
    active_sessions,
    add_user,
    external_ban,
    link_sso,
    mutations,
    sign_in,
)
from tests.fake_fluxer_admin import make_transport as make_fluxer_transport
from tests.fake_hallpass import TOKEN, FakeHallpassState
from tests.fake_hallpass import make_transport as make_hallpass_transport

ADMIN_URL = "https://fluxer.invalid"
PROVIDER = "prov-1"
SUBJECT = "00000000-0000-4000-8000-00000000abcd"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
HALLPASS_URL = "https://hallpass.invalid"
BAN_PARTS = ("/ban", "/enforcement-decisions", "/flags")
SEEN: list[FakeFluxerAdminState] = []


@pytest.fixture(autouse=True)
def no_ban_path_ever() -> Iterator[None]:
    """Every test in this module: no request the adapter sent may reach a ban, decision or flags route."""
    SEEN.clear()
    yield
    for state in SEEN:
        banned = [path for _, path in state.requests if any(part in path for part in BAN_PARTS)]
        assert banned == [], "the Fluxer adapter must never reach a ban, enforcement or flags path"


@dataclass
class Env:
    state: FakeFluxerAdminState
    adapter: FluxerAdapter
    http: httpx.AsyncClient
    hallpass: FakeHallpassState
    client: MonitorClient


def fluxer_subject(desired: str = "suspended", generation: int = 2, identities: list[dict[str, str]] | None = None,
                   ref: str = "r1") -> dict[str, Any]:
    if identities is None:
        identities = [{"kind": "sso_subject", "value": SUBJECT}]
    return {"ref": ref, "identities": identities, "desired": desired, "generation": generation}


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    state = FakeFluxerAdminState()
    SEEN.append(state)
    http = httpx.AsyncClient(transport=make_fluxer_transport(state))
    adapter = FluxerAdapter(http=http, base_url=ADMIN_URL, key=FLUXER_KEY, sso_provider_id=PROVIDER)
    hallpass = FakeHallpassState(services=[{
        "slug": "fluxer", "adapter": "fluxer", "subjects": [fluxer_subject()],
    }])
    client = MonitorClient(HALLPASS_URL, TOKEN, transport=make_hallpass_transport(hallpass))
    try:
        yield Env(state=state, adapter=adapter, http=http, hallpass=hallpass, client=client)
    finally:
        await client.aclose()
        await http.aclose()


def subject_with(identities: list[Identity], desired: str = "suspended") -> Subject:
    return Subject(ref="r1", identities=tuple(identities), desired=desired, generation=2)


SSO_SUBJECT = subject_with([Identity("sso_subject", SUBJECT)])


def _reports(env: Env) -> list[dict[str, Any]]:
    return env.hallpass.reports


async def run_cycle(env: Env) -> dict[str, dict[str, Any]]:
    """One reconcile cycle over the fake hallpass; returns the latest report for each ref."""
    adapters = {"fluxer": env.adapter}
    await cycle(client=env.client, adapter_for=lambda s: adapters.get(s.slug), now=NOW)
    return {entry["ref"]: entry for entry in _reports(env)}


def session_delete_count(env: Env) -> int:
    return sum(1 for method, path in mutations(env.state) if method == "DELETE" and path.endswith("/sessions"))


# AC10.3: a hold ends the sessions, and reports suspended ------------------------------------------

async def test_a_hold_ends_the_active_sessions_and_reports_suspended(env: Env) -> None:
    add_user(env.state, "1001")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    sign_in(env.state, "1001")
    sign_in(env.state, "1001")
    env.state.sessions["1001"].append({"id": "old", "deleted_at": "2026-11-01T00:00:00Z"})
    assert await env.adapter.observe([SSO_SUBJECT]) == {"r1": Observed("allowed")}

    reports = await run_cycle(env)

    assert reports["r1"]["observed"] == "suspended"
    assert reports["r1"]["generation"] == 2
    assert session_delete_count(env) == 1
    assert active_sessions(env.state, "1001") == 0


async def test_the_sso_lookup_carries_both_the_provider_and_the_subject(env: Env) -> None:
    add_user(env.state, "1001")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    await env.adapter.observe([SSO_SUBJECT])
    lookups = [path for method, path in env.state.requests if path.startswith("/v1/admin/sso-identities")]
    assert lookups == [f"/v1/admin/sso-identities?provider_id={PROVIDER}&subject={SUBJECT}"]


async def test_a_second_cycle_observes_suspended_and_sends_no_delete(env: Env) -> None:
    add_user(env.state, "1001")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    sign_in(env.state, "1001")
    await run_cycle(env)
    before = list(mutations(env.state))

    reports = await run_cycle(env)

    assert reports["r1"]["observed"] == "suspended"
    assert mutations(env.state) == before


async def test_drift_a_new_sign_in_is_ended_again_on_the_next_cycle(env: Env) -> None:
    add_user(env.state, "1001")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    await run_cycle(env)
    sign_in(env.state, "1001")   # the person signs in again while still suspended in hallpass

    reports = await run_cycle(env)

    assert reports["r1"]["observed"] == "suspended"
    assert active_sessions(env.state, "1001") == 0
    assert session_delete_count(env) == 1


# AC10.4: lifting a hold needs no Fluxer call ------------------------------------------------------

async def test_lifting_a_hold_makes_no_fluxer_mutation(env: Env) -> None:
    add_user(env.state, "1001")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    await run_cycle(env)
    flags_before = env.state.users["1001"]["flags"]
    mutations_before = list(mutations(env.state))
    env.hallpass.services[0]["subjects"][0]["desired"] = "allowed"
    env.hallpass.services[0]["subjects"][0]["generation"] = 3

    reports = await run_cycle(env)

    assert reports["r1"]["observed"] == "allowed"
    assert mutations(env.state) == mutations_before
    assert env.state.users["1001"]["flags"] == flags_before


# AC10.5: a ban the adapter did not place is reported and never changed ------------------------------

@pytest.mark.parametrize("desired", ["allowed", "suspended"])
async def test_an_external_ban_is_reported_and_never_changed(env: Env, desired: str) -> None:
    add_user(env.state, "1001")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    external_ban(env.state, "1001")
    env.hallpass.services[0]["subjects"][0]["desired"] = desired
    mutations_before = list(mutations(env.state))

    reports = await run_cycle(env)

    assert reports["r1"]["observed"] == "banned_external"
    assert mutations(env.state) == mutations_before
    assert int(env.state.users["1001"]["flags"]) & DISABLED


# Lookup -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("desired", ["allowed", "suspended"])
async def test_a_person_who_has_not_signed_in_to_fluxer_has_no_account_yet(env: Env, desired: str) -> None:
    observed = await env.adapter.observe([subject_with([Identity("sso_subject", SUBJECT)], desired)])
    assert observed == {"r1": Observed(desired, "no_account")}  # type: ignore[arg-type]
    assert env.state.requests == [("GET", f"/v1/admin/sso-identities?provider_id={PROVIDER}&subject={SUBJECT}")]


async def test_a_fluxer_user_id_identity_is_an_override_and_skips_the_lookup(env: Env) -> None:
    add_user(env.state, "1001")
    subject = subject_with([Identity("fluxer_user_id", "1001")], desired="allowed")
    assert await env.adapter.observe([subject]) == {"r1": Observed("allowed")}
    assert not any("sso-identities" in path for _, path in env.state.requests)


async def test_a_subject_with_no_identity_is_an_error_and_makes_no_request(env: Env) -> None:
    observed = await env.adapter.observe([subject_with([Identity("java_name", "Steve")])])
    assert observed == {"r1": Observed("error", "no_identity")}
    assert env.state.requests == []


async def test_a_lookup_that_names_an_unknown_user_is_unknown_user(env: Env) -> None:
    link_sso(env.state, PROVIDER, SUBJECT, "1002")   # no such user
    observed = await env.adapter.observe([SSO_SUBJECT])
    assert observed == {"r1": Observed("error", "unknown_user")}


# Errors -------------------------------------------------------------------------------------------

async def test_a_wrong_key_raises_auth(env: Env) -> None:
    wrong = FluxerAdapter(http=env.http, base_url=ADMIN_URL, key="w" * 40, sso_provider_id=PROVIDER)
    with pytest.raises(AdapterError) as info:
        await wrong.observe([SSO_SUBJECT])
    assert info.value.reason == "auth"


async def test_a_wrong_key_is_reported_as_an_error_in_the_cycle(env: Env) -> None:
    wrong = FluxerAdapter(http=env.http, base_url=ADMIN_URL, key="w" * 40, sso_provider_id=PROVIDER)
    adapters = {"fluxer": wrong}
    await cycle(client=env.client, adapter_for=lambda s: adapters.get(s.slug), now=NOW)
    assert _reports(env)[0]["observed"] == "error"
    assert _reports(env)[0]["detail"] == "auth"


async def test_one_accounts_failed_lookup_does_not_block_another_held_account(env: Env) -> None:
    other = "00000000-0000-4000-8000-00000000beef"
    add_user(env.state, "1001")
    add_user(env.state, "1002")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    link_sso(env.state, PROVIDER, other, "1002")
    sign_in(env.state, "1001")
    sign_in(env.state, "1002")
    env.hallpass.services[0]["subjects"] = [
        fluxer_subject(ref="r1"),
        fluxer_subject(ref="r2", identities=[{"kind": "sso_subject", "value": other}]),
    ]
    env.state.fail_next["user"] = 500   # the first user lookup is r1's, so only r1 fails

    reports = await run_cycle(env)

    assert reports["r1"]["observed"] == "error"
    assert reports["r1"]["detail"] == "http"
    assert reports["r2"]["observed"] == "suspended"
    assert active_sessions(env.state, "1002") == 0
    assert active_sessions(env.state, "1001") == 1
    assert session_delete_count(env) == 1


async def test_a_failed_session_delete_is_an_error_and_the_sessions_stay_active(env: Env) -> None:
    add_user(env.state, "1001")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    sign_in(env.state, "1001")
    env.state.fail_next["sessions_delete"] = 500

    reports = await run_cycle(env)

    assert reports["r1"]["observed"] == "error"
    assert reports["r1"]["detail"] == "sessions_not_ended"
    assert active_sessions(env.state, "1001") == 1


async def test_the_session_delete_is_accepted_because_it_carries_the_audit_reason(env: Env) -> None:
    # The fake refuses a DELETE without X-Audit-Log-Reason (400), so a suspended result proves the header was sent.
    add_user(env.state, "1001")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    sign_in(env.state, "1001")
    assert await env.adapter.apply(SSO_SUBJECT, "suspended") == Observed("suspended")
    assert active_sessions(env.state, "1001") == 0


async def test_the_fake_refuses_a_delete_without_the_audit_reason(env: Env) -> None:
    add_user(env.state, "1001")
    response = await env.http.delete(f"{ADMIN_URL}/v1/admin/users/1001/sessions",
                                     headers={"Authorization": f"Admin {FLUXER_KEY}"})
    assert response.status_code == 400


# Registry ----------------------------------------------------------------------------------------

@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database.open(tmp_path / "hallmonitor.db")
    try:
        yield db
    finally:
        db.close()


def _ctx(database: Database, settings: dict[str, str], *, url: str | None = ADMIN_URL,
         key: str | None = FLUXER_KEY) -> AdapterContext:
    config = Config(hallpass_url=HALLPASS_URL, hallpass_token=TOKEN, fluxer_admin_url=url, fluxer_admin_key=key)
    return AdapterContext(slug="fluxer", settings=settings, config=config, db=database,
                          http=httpx.AsyncClient(), clock=lambda: NOW)


def test_the_registry_needs_the_provider_id_and_both_admin_settings(database: Database) -> None:
    assert build_adapter("fluxer", _ctx(database, {})) is None
    assert build_adapter("fluxer", _ctx(database, {}, url=None)) is None
    assert build_adapter("fluxer", _ctx(database, {}, key=None)) is None
    configured = build_adapter("fluxer", _ctx(database, {"sso_provider_id": PROVIDER}))
    assert isinstance(configured, FluxerAdapter)


def test_the_fluxer_capabilities_claim_suspend_restore_read_and_session_removal() -> None:
    caps = FluxerAdapter.capabilities
    assert caps.suspend and caps.restore and caps.read_state and caps.removes_connected


# Privacy ------------------------------------------------------------------------------------------

async def test_no_decoy_value_reaches_an_observation_a_report_or_a_log(env: Env,
                                                                        caplog: pytest.LogCaptureFixture) -> None:
    add_user(env.state, "1001")
    link_sso(env.state, PROVIDER, SUBJECT, "1001")
    sign_in(env.state, "1001")
    with caplog.at_level(logging.DEBUG):
        observed = await env.adapter.observe([SSO_SUBJECT])
        await run_cycle(env)
    logged = "".join(JsonFormatter().format(record) for record in caplog.records)
    shown = repr(observed) + repr(_reports(env)) + logged
    assert DECOY_EMAIL not in shown
    assert DECOY_IP not in shown
