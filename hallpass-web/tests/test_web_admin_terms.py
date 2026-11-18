# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The admin terms pages, driven through HTTP: publishing, the acceptance report and the admin exemption.

Covers hallpass.AC3.1 and AC3.3 through the UI, and the admin exemption from the page gate.
"""

from __future__ import annotations

import re

from hallpass import registry, terms, text
from hallpass.registry import Person
from tests.fake_authentik import add_user
from tests.helpers import (
    ADMIN_SUB,
    TERMS_PRIVACY,
    TERMS_TOS,
    Env,
    accept_current,
    admin_client,
    form,
    sign_in,
    signed_in_person,
)

NOW_TEXT = "2026-11-13T12:00:00+00:00"


def listed(page: str, section: str) -> set[int]:
    """The person ids listed inside the report section with this id."""
    start = page.index(f'id="{section}"')
    end = page.index("</section>", start)
    return {int(person_id) for person_id in re.findall(r'data-person="(\d+)"', page[start:end])}


async def version_count(env: Env) -> int:
    return await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM terms_version").fetchone()[0])


async def admin_person(env: Env) -> Person:
    return await env.db.run(lambda c: registry.get_by_uuid(c, ADMIN_SUB))


# The admin exemption: only the admin terms pages are reachable before a version exists ---------------

async def test_the_admin_reaches_the_terms_pages_before_any_version_exists(env: Env):
    add_user(env.authentik, uuid=ADMIN_SUB, username="root")
    await sign_in(env, sub=ADMIN_SUB, username="root")

    assert (await env.client.get("/admin/terms")).status_code == 200
    elsewhere = await env.client.get("/admin/people")
    assert elsewhere.status_code == 303 and elsewhere.headers["location"].startswith("/terms")


# AC3.1: publishing through the form ----------------------------------------------------------------------

async def test_the_admin_publishes_a_version_through_the_form(env: Env):
    await admin_client(env)
    data = await form(env, "/admin/terms", tos_text="Second terms.", privacy_text="Second privacy.", blocking="on")

    response = await env.client.post("/admin/terms", data=data)

    assert response.status_code == 303 and response.headers["location"] == "/admin/terms"
    current = await env.db.run(terms.current)
    assert (current.version, current.tos_text, current.blocking) == (2, "Second terms.", True)
    assert "Second terms." in (await env.client.get("/terms")).text


async def test_an_optional_version_is_published_without_the_blocking_box(env: Env):
    await admin_client(env)
    data = await form(env, "/admin/terms", tos_text="Optional terms.", privacy_text="Optional privacy.")

    assert (await env.client.post("/admin/terms", data=data)).status_code == 303

    current = await env.db.run(terms.current)
    assert (current.version, current.blocking) == (2, False)


async def test_a_blank_version_is_refused_and_nothing_is_published(env: Env):
    await admin_client(env)
    data = await form(env, "/admin/terms", tos_text="   ", privacy_text="Privacy", blocking="on")

    response = await env.client.post("/admin/terms", data=data)

    assert response.status_code == 400
    assert text.INVALID_INPUT in response.text
    assert await version_count(env) == 1


# AC3.3: the acceptance report ----------------------------------------------------------------------------

async def test_the_report_lists_who_has_and_has_not_accepted_each_version(env: Env):
    await admin_client(env)
    first = await env.db.run(terms.current)
    ana, _ = await signed_in_person(env, display_name="Ana")
    ben, _ = await signed_in_person(env, display_name="Ben")
    cy, _ = await signed_in_person(env, display_name="Cy", accepted=False)
    admin = await admin_person(env)

    report = (await env.client.get(f"/admin/terms/{first.id}")).text

    assert listed(report, "accepted") == {admin.id, ana.id, ben.id}
    assert listed(report, "not-accepted") == {cy.id}
    assert NOW_TEXT in report
    assert "Ana" in report and "Cy" in report


async def test_each_version_has_its_own_report(env: Env):
    await admin_client(env)
    first = await env.db.run(terms.current)
    ana, ana_browser = await signed_in_person(env, display_name="Ana")
    admin = await admin_person(env)
    data = await form(env, "/admin/terms", tos_text="Second terms.", privacy_text="Second privacy.", blocking="on")
    assert (await env.client.post("/admin/terms", data=data)).status_code == 303
    await accept_current(env, ana_browser)
    second = await env.db.run(terms.current)

    second_report = (await env.client.get(f"/admin/terms/{second.id}")).text
    first_report = (await env.client.get(f"/admin/terms/{first.id}")).text

    assert listed(second_report, "accepted") == {ana.id}
    assert listed(second_report, "not-accepted") == {admin.id}
    assert listed(first_report, "accepted") == {admin.id, ana.id}
    assert listed(first_report, "not-accepted") == set()


async def test_an_unknown_version_is_not_found(env: Env):
    await admin_client(env)

    response = await env.client.get("/admin/terms/999")

    assert response.status_code == 404
    assert text.NOT_FOUND in response.text


# Admin-only: a non-admin gets 403 on all three routes, accepted or not ----------------------------------

async def test_a_non_admin_gets_403_on_every_terms_admin_route(env: Env):
    await admin_client(env)
    version = await env.db.run(terms.current)
    _, browser = await signed_in_person(env, accepted=False)
    token = (await form(env, "/terms", client=browser))["csrf"]

    assert (await browser.get("/admin/terms")).status_code == 403
    publish = await browser.post("/admin/terms", data={"csrf": token, "tos_text": "Sneaky.", "privacy_text": "Sneaky."})
    assert publish.status_code == 403
    assert (await browser.get(f"/admin/terms/{version.id}")).status_code == 403
    assert await version_count(env) == 1
    assert TERMS_TOS in (await env.client.get("/terms")).text
    assert TERMS_PRIVACY in (await env.client.get("/terms")).text


# The version page shows the texts stored with that version, escaped ------------------------------------

async def test_the_version_page_shows_the_texts_of_that_version_and_not_a_newer_one(env: Env):
    await admin_client(env)
    first = await env.db.run(terms.current)
    data = await form(env, "/admin/terms", tos_text="Second terms.", privacy_text="Second privacy.", blocking="on")
    assert (await env.client.post("/admin/terms", data=data)).status_code == 303

    older = (await env.client.get(f"/admin/terms/{first.id}")).text

    assert "Terms of service" in older and "Privacy disclosure" in older
    assert TERMS_TOS in older and TERMS_PRIVACY in older
    assert "Second terms." not in older and "Second privacy." not in older
    assert older.index("Terms of service") < older.index('id="accepted"')


async def test_the_version_page_escapes_html_in_the_stored_texts(env: Env):
    await admin_client(env)
    data = await form(env, "/admin/terms", tos_text="<script>alert('tos')</script>",
                      privacy_text="<b>bold</b>", blocking="on")
    assert (await env.client.post("/admin/terms", data=data)).status_code == 303
    current = await env.db.run(terms.current)

    shown = (await env.client.get(f"/admin/terms/{current.id}")).text

    assert "<script>alert(" not in shown and "&lt;script&gt;" in shown
    assert "<b>bold</b>" not in shown and "&lt;b&gt;bold&lt;/b&gt;" in shown
