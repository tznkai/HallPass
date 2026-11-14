# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin pages for services, entitlements and attached tokens, driven through HTTP as the admin.

Covers hallpass.AC5.1 and AC5.2 through the UI. The non-admin 403 on every admin route is checked by
test_web_admin.py, which walks all /admin routes, including these.
"""

from __future__ import annotations

import httpx
import pytest

from hallpass import registry, services, text, tokens
from tests.helpers import Env, admin_client, form, signed_in_person

INVITE = "https://invite.hallpass.test/abc"


async def create_service_form(env: Env, slug: str, kind: str, *, auto: bool = False,
                              url: str = "") -> httpx.Response:
    fields = {"slug": slug, "name": slug.title(), "kind": kind, "adapter": "", "url": url}
    if auto:
        fields["auto_suspendable"] = "on"
    data = await form(env, "/admin/services", **fields)
    return await env.client.post("/admin/services", data=data)


async def service_for(env: Env, slug: str) -> services.Service:
    return await env.db.run(lambda c: services.get_by_slug(c, slug))


async def post_as_admin(env: Env, path: str, **fields: str) -> httpx.Response:
    data = await form(env, "/admin/services", **fields)
    return await env.client.post(path, data=data)


async def check(browser: httpx.AsyncClient, slug: str) -> int:
    response = await browser.get("/forward-auth", params={"service": slug},
                                 headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "calendar.hallpass.test",
                                          "X-Forwarded-Uri": "/"})
    return response.status_code


# AC5.1: adding and retiring services -------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["oidc", "forward_auth", "game", "feed"])
async def test_the_admin_adds_a_service_of_each_kind_with_the_flag(env: Env, kind: str):
    await admin_client(env)

    response = await create_service_form(env, f"svc-{kind.replace('_', '-')}", kind, auto=True,
                                         url="https://svc.hallpass.test/")

    assert response.status_code == 303
    service = await service_for(env, f"svc-{kind.replace('_', '-')}")
    assert service.kind == kind
    assert service.auto_suspendable is True
    assert service.url == "https://svc.hallpass.test/"


async def test_a_duplicate_slug_and_a_bad_slug_are_refused_and_nothing_is_stored(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")

    duplicate = await create_service_form(env, "calendar", "game")
    bad = await create_service_form(env, "Not A Slug", "game")

    assert duplicate.status_code == 400 and text.SERVICE_TAKEN in duplicate.text
    assert bad.status_code == 400 and text.SERVICE_SLUG_INVALID in bad.text
    assert len(await env.db.run(services.list_services)) == 1


async def test_a_slug_with_an_underscore_is_refused_with_the_slug_rule(env: Env):
    await admin_client(env)

    refused = await create_service_form(env, "the_ship", "game")

    assert refused.status_code == 400
    assert text.SERVICE_SLUG_INVALID in refused.text
    assert await service_for(env, "the_ship") is None


async def test_a_hyphenated_slug_is_accepted_and_a_65_character_one_is_refused(env: Env):
    await admin_client(env)

    accepted = await create_service_form(env, "the-ship", "game")
    too_long = await create_service_form(env, "a" * 65, "game")

    assert accepted.status_code == 303
    assert (await service_for(env, "the-ship")).slug == "the-ship"
    assert too_long.status_code == 400 and text.SERVICE_SLUG_INVALID in too_long.text


async def test_a_retired_service_leaves_the_pages_and_forward_auth_refuses_it(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth", auto=True)
    calendar = await service_for(env, "calendar")
    person, browser = await signed_in_person(env, account_type="adult")
    await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements", subject="type:adult", effect="grant")
    assert "Calendar" in (await browser.get("/")).text
    assert await check(browser, "calendar") == 200

    retired = await post_as_admin(env, f"/admin/services/{calendar.id}/retire")

    assert retired.status_code == 303
    assert "Calendar" not in (await browser.get("/")).text
    assert await check(browser, "calendar") == 403


async def test_unretiring_a_service_brings_it_back(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    calendar = await service_for(env, "calendar")
    person, browser = await signed_in_person(env, account_type="adult")
    await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements", subject="type:adult", effect="grant")
    await post_as_admin(env, f"/admin/services/{calendar.id}/retire")

    await post_as_admin(env, f"/admin/services/{calendar.id}/unretire")

    assert await check(browser, "calendar") == 200


async def test_the_admin_edits_a_service_but_not_its_slug(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    calendar = await service_for(env, "calendar")

    edited = await post_as_admin(env, f"/admin/services/{calendar.id}", name="Family calendar", adapter="manual",
                                 url="https://cal.hallpass.test/", auto_suspendable="on")

    assert edited.status_code == 303
    after = await service_for(env, "calendar")
    assert after.name == "Family calendar" and after.url == "https://cal.hallpass.test/"
    assert after.auto_suspendable is True
    assert after.slug == "calendar"


async def test_an_unknown_service_id_is_not_found(env: Env):
    await admin_client(env)

    response = await env.client.get("/admin/services/999999")

    assert response.status_code == 404


async def test_the_identity_form_offers_the_services_as_a_select(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    person = await env.db.run(lambda c: registry.create_person(c, display_name="Kid", account_type="child",
                                                              now=env.clock.now()))

    page = (await env.client.get(f"/admin/people/{person.id}")).text

    assert '<select name="service_slug"' in page
    assert '<option value="calendar">' in page


# AC5.2: grants through the UI ----------------------------------------------------------------------------------

async def entitlement_id(env: Env, service: services.Service, subject_kind: str, subject: str) -> int:
    rows = await env.db.run(lambda c: services.entitlements_for(c, service.id))
    return next(row.id for row in rows if row.subject_kind == subject_kind and row.subject == subject)


async def test_a_type_grant_added_and_removed_through_the_form_drives_forward_auth(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    calendar = await service_for(env, "calendar")
    _, browser = await signed_in_person(env, account_type="adult")
    assert await check(browser, "calendar") == 403

    added = await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements", subject="type:adult",
                                effect="grant")
    assert added.status_code == 303
    assert await check(browser, "calendar") == 200

    removed = await post_as_admin(env, f"/admin/entitlements/{await entitlement_id(env, calendar, 'type', 'adult')}"
                                       "/remove")
    assert removed.status_code == 303
    assert await check(browser, "calendar") == 403


async def test_a_group_grant_added_and_removed_through_the_form_drives_forward_auth(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    calendar = await service_for(env, "calendar")
    person, browser = await signed_in_person(env, account_type="adult")
    group_id = await env.db.run(lambda c: registry.create_group(c, "Gardeners"))
    await env.db.run(lambda c: registry.add_member(c, group_id, person.id))
    assert await check(browser, "calendar") == 403

    await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements", subject=f"group:{group_id}",
                        effect="grant")
    assert await check(browser, "calendar") == 200

    await post_as_admin(env, f"/admin/entitlements/{await entitlement_id(env, calendar, 'group', str(group_id))}"
                             "/remove")
    assert await check(browser, "calendar") == 403


async def test_a_person_grant_through_the_form_drives_forward_auth(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    calendar = await service_for(env, "calendar")
    person, browser = await signed_in_person(env, account_type="adult")
    assert await check(browser, "calendar") == 403

    await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements", subject=f"person:{person.id}",
                        effect="grant")

    assert await check(browser, "calendar") == 200


async def test_a_person_deny_through_the_form_overrides_a_type_grant(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    calendar = await service_for(env, "calendar")
    person, browser = await signed_in_person(env, account_type="adult")
    await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements", subject="type:adult", effect="grant")
    assert await check(browser, "calendar") == 200

    denied = await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements",
                                 subject=f"person:{person.id}", effect="deny")

    assert denied.status_code == 303
    assert await check(browser, "calendar") == 403


async def test_a_deny_on_a_type_is_refused_and_stores_nothing(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    calendar = await service_for(env, "calendar")

    refused = await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements", subject="type:adult",
                                  effect="deny")

    assert refused.status_code == 400
    assert text.ENTITLEMENT_DENY_NOT_PERSON in refused.text
    assert await env.db.run(lambda c: services.entitlements_for(c, calendar.id)) == []


async def test_a_duplicate_entitlement_is_refused(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    calendar = await service_for(env, "calendar")
    await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements", subject="type:adult", effect="grant")

    again = await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements", subject="type:adult",
                                effect="grant")

    assert again.status_code == 400
    assert text.ENTITLEMENT_TAKEN in again.text


async def test_an_entitlement_for_an_unknown_person_or_group_is_not_found(env: Env):
    await admin_client(env)
    await create_service_form(env, "calendar", "forward_auth")
    calendar = await service_for(env, "calendar")

    missing_person = await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements",
                                         subject="person:999999", effect="grant")
    missing_group = await post_as_admin(env, f"/admin/services/{calendar.id}/entitlements",
                                        subject="group:999999", effect="grant")

    assert missing_person.status_code == 404
    assert missing_group.status_code == 404


# Attached tokens ------------------------------------------------------------------------------------------------

async def test_an_attached_invite_link_is_shown_to_entitled_people_and_is_logged_without_its_value(env: Env):
    await admin_client(env)
    await create_service_form(env, "fluxer", "oidc")
    fluxer = await service_for(env, "fluxer")
    await post_as_admin(env, f"/admin/services/{fluxer.id}/entitlements", subject="type:adult", effect="grant")
    _, member = await signed_in_person(env, account_type="adult", display_name="Member")
    _, outsider = await signed_in_person(env, account_type="child", display_name="Ana")

    attached = await post_as_admin(env, f"/admin/services/{fluxer.id}/tokens", kind="invite_link",
                                   label="Fluxer invite", value=INVITE, person="")

    assert attached.status_code == 303
    assert INVITE in (await member.get("/")).text
    assert INVITE not in (await outsider.get("/")).text
    logged = await env.db.run(lambda c: c.execute(
        "SELECT action, services, detail FROM decision_log WHERE action = 'token_attached'").fetchall())
    assert len(logged) == 1
    assert INVITE not in " ".join(str(part) for part in logged[0])


async def test_a_per_person_credential_is_shown_only_to_its_person(env: Env):
    await admin_client(env)
    await create_service_form(env, "valheim", "game")
    valheim = await service_for(env, "valheim")
    await post_as_admin(env, f"/admin/services/{valheim.id}/entitlements", subject="type:adult", effect="grant")
    owner, owner_browser = await signed_in_person(env, account_type="adult", display_name="Owner")
    _, other_browser = await signed_in_person(env, account_type="adult", display_name="Other")

    await post_as_admin(env, f"/admin/services/{valheim.id}/tokens", kind="game_credential", label="login",
                        value="owner-credential", person=str(owner.id))

    assert "owner-credential" in (await owner_browser.get("/")).text
    assert "owner-credential" not in (await other_browser.get("/")).text


async def test_a_revoked_token_disappears_and_the_revocation_is_logged(env: Env):
    await admin_client(env)
    await create_service_form(env, "fluxer", "oidc")
    fluxer = await service_for(env, "fluxer")
    await post_as_admin(env, f"/admin/services/{fluxer.id}/entitlements", subject="type:adult", effect="grant")
    _, member = await signed_in_person(env, account_type="adult", display_name="Member")
    await post_as_admin(env, f"/admin/services/{fluxer.id}/tokens", kind="invite_link", label="Fluxer invite",
                        value=INVITE, person="")
    token_id = await env.db.run(lambda c: tokens.attached_for(c, fluxer.id)[0].id)

    revoked = await post_as_admin(env, f"/admin/tokens/{token_id}/revoke")

    assert revoked.status_code == 303
    assert INVITE not in (await member.get("/")).text
    logged = await env.db.run(lambda c: c.execute(
        "SELECT action FROM decision_log WHERE action = 'token_revoked'").fetchall())
    assert logged == [("token_revoked",)]


async def test_an_attached_token_with_an_unknown_person_is_not_found(env: Env):
    await admin_client(env)
    await create_service_form(env, "fluxer", "oidc")
    fluxer = await service_for(env, "fluxer")

    response = await post_as_admin(env, f"/admin/services/{fluxer.id}/tokens", kind="invite_link", label="x",
                                   value=INVITE, person="999999")

    assert response.status_code == 404


async def test_the_service_page_lists_its_entitlements_and_tokens(env: Env):
    await admin_client(env)
    await create_service_form(env, "fluxer", "oidc")
    fluxer = await service_for(env, "fluxer")
    await post_as_admin(env, f"/admin/services/{fluxer.id}/entitlements", subject="type:adult", effect="grant")
    await post_as_admin(env, f"/admin/services/{fluxer.id}/tokens", kind="invite_link", label="Fluxer invite",
                        value=INVITE, person="")

    page = (await env.client.get(f"/admin/services/{fluxer.id}")).text

    assert "adult" in page
    assert INVITE in page
