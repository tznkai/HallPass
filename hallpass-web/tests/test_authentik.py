# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Authentik client, exercised against the in-process fake."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from hallpass.authentik import AuthentikClient, AuthentikError
from tests.fake_authentik import TOKEN, FakeAuthentikState, InvitationUsed, add_user, enrol, make_app

BASE = "https://authentik.test"
EXPIRES = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
GROUP = "11111111-1111-4111-8111-111111111111"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def state() -> FakeAuthentikState:
    return FakeAuthentikState()


@pytest.fixture
async def client(state: FakeAuthentikState) -> AsyncIterator[AuthentikClient]:
    authentik = AuthentikClient(BASE, TOKEN, transport=httpx.ASGITransport(app=make_app(state)))
    try:
        yield authentik
    finally:
        await authentik.aclose()


async def invite(client: AuthentikClient, ref: str = "ref-1") -> str:
    created = await client.create_invitation(name="hallpass-test", flow_slug="invite", enrollment_ref=ref,
                                             expires=EXPIRES)
    return created.pk


async def raw_client(state: FakeAuthentikState) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(state)), base_url=BASE, headers=AUTH)


async def test_invitation_is_single_use_with_enrollment_fixed_data(client, state):
    created = await client.create_invitation(name="hallpass-x", flow_slug="invite", enrollment_ref="ref-1",
                                             expires=EXPIRES)
    stored = state.invitations[created.pk]
    assert stored["single_use"] is True
    assert stored["fixed_data"] == {"attributes.hallpass_enrollment": "ref-1"}
    assert stored["flow"] == "flow-1"
    assert created.url == f"{BASE}/if/flow/invite/?itoken={created.pk}"


async def test_flow_lookup_is_cached(client, state):
    await invite(client, "ref-1")
    await invite(client, "ref-2")
    lookups = [path for method, path in state.requests if path.startswith("/api/v3/flows/")]
    assert lookups == ["/api/v3/flows/instances/?slug=invite"]


async def test_enrol_twice_on_the_same_invitation_raises(client, state):
    pk = await invite(client)
    enrol(state, pk, "kid1")
    with pytest.raises(InvitationUsed):
        enrol(state, pk, "kid2")


async def test_delete_invitation_removes_it_and_treats_404_as_success(client, state):
    pk = await invite(client)
    await client.delete_invitation(pk)
    assert pk not in state.invitations
    await client.delete_invitation(pk)   # already gone: success


async def test_user_by_uuid_returns_the_user(client, state):
    user = add_user(state, uuid="22222222-2222-4222-8222-222222222222", username="kid1")
    found = await client.user_by_uuid(user["uuid"])
    assert (found.pk, found.uuid, found.username, found.is_active) == (user["pk"], user["uuid"], "kid1", True)


async def test_user_by_uuid_without_results_is_not_found(client):
    with pytest.raises(AuthentikError) as error:
        await client.user_by_uuid("33333333-3333-4333-8333-333333333333")
    assert error.value.reason == "not_found"


@pytest.mark.parametrize("uuid", ["", None])
async def test_user_by_uuid_refuses_a_blank_uuid_before_any_request(client, state, uuid):
    with pytest.raises(ValueError):
        await client.user_by_uuid(uuid)
    assert state.requests == []


async def test_update_attributes_keeps_other_keys_and_deletes_none_values(client, state):
    user = add_user(state, uuid="44444444-4444-4444-8444-444444444444", username="kid1",
                    attributes={"keep": "a", "drop": "b"})
    await client.update_attributes(user["pk"], {"drop": None, "added": "c"})
    assert state.users[user["pk"]]["attributes"] == {"keep": "a", "added": "c"}


async def test_end_sessions_deletes_only_that_users_sessions(client, state):
    a = add_user(state, uuid="55555555-5555-4555-8555-555555555555", username="a")
    b = add_user(state, uuid="66666666-6666-4666-8666-666666666666", username="b")
    state.sessions = {a["pk"]: ["a1", "a2"], b["pk"]: ["b1"]}

    deleted = await client.end_sessions(a["pk"])

    assert deleted == 2
    assert state.sessions.get(a["pk"], []) == []
    assert state.sessions[b["pk"]] == ["b1"]
    assert ("DELETE", f"/api/v3/core/authenticated_sessions/bulk_delete/?user_pks={a['pk']}") in state.requests
    assert not any(method == "GET" and "authenticated_sessions" in path for method, path in state.requests)


async def test_deactivation_then_end_sessions_leaves_user_inactive_with_no_sessions(client, state):
    user = add_user(state, uuid="77777777-7777-4777-8777-777777777777", username="kid1")
    state.sessions = {user["pk"]: ["s1"]}

    await client.set_active(user["pk"], False)
    deleted = await client.end_sessions(user["pk"])

    assert state.users[user["pk"]]["is_active"] is False
    assert state.sessions.get(user["pk"], []) == []
    assert deleted == 0


@pytest.mark.parametrize("pk", [0, -1, None, "", "1", True, False, 1.0])
async def test_end_sessions_refuses_a_non_positive_or_non_int_pk_before_any_request(client, state, pk):
    with pytest.raises(ValueError):
        await client.end_sessions(pk)
    assert state.requests == []


async def test_fake_bulk_delete_without_user_pks_is_400_and_deletes_nothing(state):
    add_user(state, uuid="88888888-8888-4888-8888-888888888888", username="a")
    state.sessions = {1: ["s1"]}
    raw = await raw_client(state)
    try:
        for url in ("/api/v3/core/authenticated_sessions/bulk_delete/",
                    "/api/v3/core/authenticated_sessions/bulk_delete/?user_pks="):
            response = await raw.delete(url)
            assert response.status_code == 400
    finally:
        await raw.aclose()
    assert state.sessions == {1: ["s1"]}


async def test_fake_session_list_ignores_the_user_filter(state):
    add_user(state, uuid="99999999-9999-4999-8999-999999999999", username="a")
    add_user(state, uuid="10101010-1010-4101-8101-101010101010", username="b")
    state.sessions = {1: ["a1", "a2"], 2: ["b1"]}
    raw = await raw_client(state)
    try:
        response = await raw.get("/api/v3/core/authenticated_sessions/?user=1")
    finally:
        await raw.aclose()
    assert response.status_code == 200
    assert len(response.json()["results"]) == 3


async def test_set_active_flips_is_active(client, state):
    user = add_user(state, uuid="12121212-1212-4121-8121-121212121212", username="kid1")
    await client.set_active(user["pk"], False)
    assert state.users[user["pk"]]["is_active"] is False
    await client.set_active(user["pk"], True)
    assert state.users[user["pk"]]["is_active"] is True


async def test_group_add_and_remove_change_membership(client, state):
    state.groups[GROUP] = set()
    await client.add_to_group(GROUP, 3)
    assert state.groups[GROUP] == {3}
    await client.remove_from_group(GROUP, 3)
    assert state.groups[GROUP] == set()


async def test_wrong_token_is_an_http_403(state):
    wrong = AuthentikClient(BASE, "wrong-token", transport=httpx.ASGITransport(app=make_app(state)))
    try:
        with pytest.raises(AuthentikError) as error:
            await wrong.create_invitation(name="x", flow_slug="invite", enrollment_ref="ref", expires=EXPIRES)
    finally:
        await wrong.aclose()
    assert error.value.reason == "http"
    assert error.value.status == 403


async def test_injected_500_is_an_http_error_and_fires_once(client, state):
    user = add_user(state, uuid="13131313-1313-4131-8131-131313131313", username="kid1")
    state.fail_next = 500
    with pytest.raises(AuthentikError) as error:
        await client.user_by_uuid(user["uuid"])
    assert (error.value.reason, error.value.status) == ("http", 500)
    assert (await client.user_by_uuid(user["uuid"])).pk == user["pk"]


async def test_transport_failure_is_unreachable_and_leaks_no_url():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused by the test transport")

    authentik = AuthentikClient(BASE, TOKEN, transport=httpx.MockTransport(refuse))
    try:
        with pytest.raises(AuthentikError) as error:
            await authentik.user_by_uuid("14141414-1414-4141-8141-141414141414")
    finally:
        await authentik.aclose()
    assert error.value.reason == "unreachable"
    assert error.value.status is None
    assert BASE not in str(error.value)
