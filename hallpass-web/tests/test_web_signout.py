# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-out reaches Authentik: POST /logout redirects to the end-session endpoint with exactly two parameters, built
from the discovery document, the stored ID token and the public URL. A request cannot change that address."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx

from hallpass import registry
from hallpass.logout import ProviderError
from tests.helpers import BASE_URL, Env, accept_for, form, new_client, publish_terms, sign_in, store_person
from tests.logout_tokens import END_SESSION, SID, SUB

ID_TOKEN = "header.payload.signature"


async def bound_adult(env: Env) -> None:
    await publish_terms(env)
    person = await store_person(env, account_type="adult")
    await env.db.run(lambda c: registry.bind_identity(c, person.id, uuid=SUB, authentik_pk=7, username="kid1"))
    await accept_for(env, person.id)


async def signed_in(env: Env, *, id_token: str | None = ID_TOKEN, sid: str | None = SID) -> httpx.AsyncClient:
    browser = new_client(env)
    response = await sign_in(env, sub=SUB, username="kid1", sid=sid, id_token=id_token, client=browser)
    assert response.status_code == 303
    return browser


async def sign_out(browser: httpx.AsyncClient, env: Env, **extra: str) -> httpx.Response:
    data = {"csrf": (await form(env, "/", client=browser))["csrf"], **extra}
    return await browser.post("/logout", data=data)


async def test_sign_out_redirects_to_the_end_session_endpoint_with_exactly_two_parameters(env: Env):
    await bound_adult(env)
    browser = await signed_in(env)

    response = await sign_out(browser, env)

    assert response.status_code == 303
    parts = urlsplit(response.headers["location"])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == END_SESSION
    query = parse_qs(parts.query, keep_blank_values=True)
    assert query == {"id_token_hint": [ID_TOKEN], "post_logout_redirect_uri": [f"{BASE_URL}/signed-out"]}


async def test_the_session_is_ended_when_the_sign_out_redirects_away(env: Env):
    await bound_adult(env)
    browser = await signed_in(env)

    await sign_out(browser, env)

    home = await browser.get("/")
    assert home.status_code == 303 and home.headers["location"].startswith("/login")


async def test_a_request_cannot_change_the_sign_out_address(env: Env):
    await bound_adult(env)
    browser = await signed_in(env)

    response = await sign_out(browser, env, post_logout_redirect_uri="https://evil.invalid/",
                              next="https://evil.invalid/", state="x")

    parts = urlsplit(response.headers["location"])
    assert parse_qs(parts.query)["post_logout_redirect_uri"] == [f"{BASE_URL}/signed-out"]
    assert "evil.invalid" not in response.headers["location"]
    assert "state" not in parse_qs(parts.query)


async def test_a_sign_in_without_an_id_token_signs_out_to_signed_out(env: Env):
    await bound_adult(env)
    browser = await signed_in(env, id_token=None)

    response = await sign_out(browser, env)

    assert response.status_code == 303
    assert response.headers["location"] == "/signed-out"


async def test_a_provider_without_an_end_session_endpoint_signs_out_to_signed_out(env: Env):
    await bound_adult(env)
    env.oidc.end_session_endpoint = None
    browser = await signed_in(env)

    response = await sign_out(browser, env)

    assert response.headers["location"] == "/signed-out"
    home = await browser.get("/")
    assert home.headers["location"].startswith("/login")


async def test_an_unreachable_provider_still_ends_the_session_and_goes_to_signed_out(env: Env, caplog):
    await bound_adult(env)
    browser = await signed_in(env)
    env.oidc.provider_error = ProviderError("provider_unavailable")

    response = await sign_out(browser, env)

    assert response.status_code == 303
    assert response.headers["location"] == "/signed-out"
    assert (await browser.get("/")).headers["location"].startswith("/login")


async def test_signed_out_renders_without_a_session(env: Env):
    response = await env.client.get("/signed-out")

    assert response.status_code == 200
    assert "signed out" in response.text.lower()
    assert 'href="/login"' in response.text
    assert "logout" not in response.text.lower()


async def test_signed_out_is_not_behind_the_sign_in_gate(env: Env):
    response = await env.client.get("/signed-out")

    assert response.status_code != 303
    assert "location" not in response.headers
