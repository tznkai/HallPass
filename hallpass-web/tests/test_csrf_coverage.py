# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSRF coverage (AC13.4): every POST route depends on require_csrf, and a POST without the right token changes nothing."""

from __future__ import annotations

from hallpass import registry
from hallpass.auth import require_csrf
from tests.helpers import Env, admin_client, api_routes, form


def _dependency_calls(dependant) -> set:
    calls = set()
    for sub in dependant.dependencies:
        calls.add(sub.call)
        calls |= _dependency_calls(sub)
    return calls


def _post_routes(env: Env) -> list:
    return [route for route in api_routes(env.application) if "POST" in route.methods]


# The provider's back-channel logout. It has no session and no form token: the signed logout token is its authority,
# and the route is checked for those checks in test_backchannel.py.
PUBLIC_POSTS = {"/auth/backchannel-logout"}


async def test_every_post_route_depends_on_require_csrf(env: Env):
    posts = _post_routes(env)
    assert posts, "no POST routes were found"
    assert PUBLIC_POSTS <= {route.path for route in posts}, "a public POST route in the list is gone"
    missing = [route.path for route in posts
               if route.path not in PUBLIC_POSTS and require_csrf not in _dependency_calls(route.dependant)]
    assert missing == []


async def test_a_post_without_the_csrf_field_is_refused_and_creates_nothing(env: Env):
    await admin_client(env)
    before = await env.db.run(registry.list_people)

    response = await env.client.post("/admin/people", data={"display_name": "Sneaky", "account_type": "adult"})

    assert response.status_code == 403
    assert await env.db.run(registry.list_people) == before


async def test_a_post_with_a_wrong_token_is_refused_and_creates_nothing(env: Env):
    await admin_client(env)
    before = await env.db.run(registry.list_people)

    response = await env.client.post("/admin/people", data={"csrf": "not-the-token", "display_name": "Sneaky",
                                                             "account_type": "adult"})

    assert response.status_code == 403
    assert await env.db.run(registry.list_people) == before


async def test_a_post_with_the_right_token_from_another_origin_is_refused(env: Env):
    await admin_client(env)
    before = await env.db.run(registry.list_people)
    data = await form(env, "/admin/people", display_name="Sneaky", account_type="adult", email="", phone="")

    response = await env.client.post("/admin/people", data=data, headers={"Origin": "https://evil.invalid"})

    assert response.status_code == 403
    assert await env.db.run(registry.list_people) == before
