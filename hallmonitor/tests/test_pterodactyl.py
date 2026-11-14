# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Pterodactyl file and resource client, against the fake served through an httpx MockTransport. No network."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from hallmonitor.pterodactyl import PteroError, PteroFiles
from tests.fake_ptero_files import PTERO_KEY, FakePteroState, handle, make_transport

BASE = "https://fake.test"
SERVER = "srv1abcd"
PATH = "banlist.txt"
WRONG_KEY = "not-the-key"


@asynccontextmanager
async def ptero(state: FakePteroState, key: str = PTERO_KEY, server: str = SERVER) -> AsyncIterator[PteroFiles]:
    async with httpx.AsyncClient(transport=make_transport(state)) as http:
        yield PteroFiles(BASE, key, server, http=http)


async def test_a_read_and_write_round_trip_the_file_text():
    state = FakePteroState(files={(SERVER, PATH): "one\n"})
    async with ptero(state) as client:
        assert await client.read(PATH) == "one\n"
        await client.write(PATH, "one\ntwo\n")
        assert await client.read(PATH) == "one\ntwo\n"
    assert state.files[(SERVER, PATH)] == "one\ntwo\n"
    assert state.requests == [
        ("GET", f"/api/client/servers/{SERVER}/files/contents"),
        ("POST", f"/api/client/servers/{SERVER}/files/write"),
        ("GET", f"/api/client/servers/{SERVER}/files/contents"),
    ]


async def test_a_wrong_key_is_an_auth_error_for_every_call():
    state = FakePteroState(files={(SERVER, PATH): "one\n"})
    async with ptero(state, key=WRONG_KEY) as client:
        with pytest.raises(PteroError) as read_error:
            await client.read(PATH)
        with pytest.raises(PteroError) as write_error:
            await client.write(PATH, "x")
        with pytest.raises(PteroError) as uptime_error:
            await client.uptime_seconds()
    assert read_error.value.reason == "auth"
    assert write_error.value.reason == "auth"
    assert uptime_error.value.reason == "auth"
    assert state.files[(SERVER, PATH)] == "one\n"


async def test_a_missing_file_is_not_found():
    state = FakePteroState()
    async with ptero(state) as client:
        with pytest.raises(PteroError) as info:
            await client.read(PATH)
    assert info.value.reason == "not_found"


async def test_a_file_on_another_server_is_not_found():
    state = FakePteroState(files={(SERVER, PATH): "one\n"})
    async with ptero(state, server="other1") as client:
        with pytest.raises(PteroError) as info:
            await client.read(PATH)
    assert info.value.reason == "not_found"


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "auth"), (403, "auth"), (404, "not_found"), (409, "http"), (500, "http"), (503, "http")],
)
async def test_a_failed_status_maps_to_a_reason(status: int, reason: str):
    state = FakePteroState(files={(SERVER, PATH): "one\n"}, fail_next={"contents": status})
    async with ptero(state) as client:
        with pytest.raises(PteroError) as info:
            await client.read(PATH)
    assert info.value.reason == reason


async def test_a_write_that_does_not_answer_204_is_an_http_error_and_changes_nothing():
    state = FakePteroState(files={(SERVER, PATH): "one\n"}, fail_next={"write": 200})
    async with ptero(state) as client:
        with pytest.raises(PteroError) as info:
            await client.write(PATH, "two\n")
    assert info.value.reason == "http"
    assert state.files[(SERVER, PATH)] == "one\n"


async def test_a_transport_failure_is_unreachable():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        client = PteroFiles(BASE, PTERO_KEY, SERVER, http=http)
        with pytest.raises(PteroError) as info:
            await client.read(PATH)
    assert info.value.reason == "unreachable"


async def test_uptime_is_seconds_while_the_server_runs():
    state = FakePteroState(state="running", uptime_ms=600_000)
    async with ptero(state) as client:
        assert await client.uptime_seconds() == 600.0


@pytest.mark.parametrize("server_state", ["offline", "starting", "stopping"])
async def test_uptime_is_none_when_the_server_is_not_running(server_state: str):
    state = FakePteroState(state=server_state, uptime_ms=3_600_000)
    async with ptero(state) as client:
        assert await client.uptime_seconds() is None


async def test_an_unknown_server_for_resources_is_not_found():
    state = FakePteroState(fail_next={"resources": 404})
    async with ptero(state) as client:
        with pytest.raises(PteroError) as info:
            await client.uptime_seconds()
    assert info.value.reason == "not_found"


async def test_the_client_repr_hides_the_key_and_the_server_id():
    async with ptero(FakePteroState()) as client:
        text = repr(client)
    assert PTERO_KEY not in text and SERVER not in text


def test_a_path_the_fake_does_not_own_is_left_to_another_handler():
    request = httpx.Request("GET", f"{BASE}/v1/api/players")
    assert handle(FakePteroState(), request) is None
