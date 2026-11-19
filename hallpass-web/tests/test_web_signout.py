# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-out reaches Authentik: POST /logout redirects to the end-session endpoint with exactly two parameters, built
from the discovery document, the stored ID token and the public URL. A request cannot change that address."""

from __future__ import annotations

import base64
import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import httpx

from hallpass import registry
from hallpass.logout import ProviderError
from tests.helpers import (
    BASE_URL,
    Env,
    StubOidcClient,
    accept_for,
    form,
    new_client,
    publish_terms,
    sign_in,
    store_person,
    use_oidc_client,
)
from tests.logout_tokens import END_SESSION, ISSUER, SID, SUB

ID_TOKEN = "header.payload.signature"
CLAIMS_MARKER = "claims-marker-7f3a"
ID_TOKEN_WITH_CLAIMS = f"header.payload-{CLAIMS_MARKER}.signature"
SESSION = "hallpass_session"


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


def session_cookie(response: httpx.Response) -> str:
    """The raw value of the session cookie a response sets, with the cookie's own quoting removed."""
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    return jar[SESSION].value


def session_contents(raw: str) -> dict:
    """What the signed session cookie holds: the value is base64 JSON, then a timestamp and a signature."""
    return json.loads(base64.b64decode(raw.split(".")[0]))


async def stored_tokens(env: Env) -> int:
    return await env.db.run(lambda c: c.execute("SELECT count(*) FROM oidc_session").fetchone()[0])


async def test_the_session_cookie_holds_a_handle_and_no_id_token_or_name(env: Env):
    await bound_adult(env)
    browser = new_client(env)

    response = await sign_in(env, sub=SUB, username="kid1", sid=SID, id_token=ID_TOKEN_WITH_CLAIMS, client=browser)

    assert response.status_code == 303
    raw = session_cookie(response)
    contents = session_contents(raw)
    assert "id_token" not in contents
    assert "name" not in contents and "username" not in contents
    assert CLAIMS_MARKER not in raw
    assert len(contents["id_token_handle"]) >= 32
    assert await stored_tokens(env) == 1


async def test_sign_out_sends_the_stored_token_and_deletes_its_row(env: Env):
    await bound_adult(env)
    browser = await signed_in(env, id_token=ID_TOKEN_WITH_CLAIMS)

    response = await sign_out(browser, env)

    assert parse_qs(urlsplit(response.headers["location"]).query)["id_token_hint"] == [ID_TOKEN_WITH_CLAIMS]
    assert await stored_tokens(env) == 0


async def test_sign_out_without_a_stored_token_signs_out_to_signed_out(env: Env):
    await bound_adult(env)
    browser = await signed_in(env)
    await env.db.run(lambda c: c.execute("DELETE FROM oidc_session"))

    response = await sign_out(browser, env)

    assert response.headers["location"] == "/signed-out"


async def test_sign_out_reaches_the_end_session_endpoint_even_when_the_keys_cannot_be_fetched(env: Env):
    await bound_adult(env)
    browser = await signed_in(env)
    stub = StubOidcClient(metadata={"issuer": ISSUER, "end_session_endpoint": END_SESSION},
                          jwks_error=httpx.ConnectError("unreachable"))
    use_oidc_client(env, stub)

    response = await sign_out(browser, env)

    assert response.status_code == 303
    parts = urlsplit(response.headers["location"])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == END_SESSION
    assert stub.jwks_calls == []


async def test_a_copied_cookie_is_ended_by_sign_out_at_once(env: Env):
    """A copy of the session cookie, taken before sign-out, is refused by the next request: sign-out records the sid
    locally as well as at the provider."""
    await bound_adult(env)
    browser = await signed_in(env)
    copied = browser.cookies.get(SESSION)
    assert copied is not None

    await sign_out(browser, env)

    replay = new_client(env)
    replay.cookies.set(SESSION, copied)
    home = await replay.get("/")
    assert home.status_code == 303
    assert home.headers["location"].startswith("/login")
