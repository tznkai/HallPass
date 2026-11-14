# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report display: the status line on a person's page, and the monitor health page for the administrator.

Covers hallpass.AC8.3, AC9.8 (display), AC9.9 and AC12.1 (display).
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from hallpass import desired, holds, registry, services
from hallpass.registry import Person
from tests.helpers import ADMIN_SUB, MONITOR_TOKEN, NOW, Env, admin_client, make_service, signed_in_person

REPORTS_PATH = "/api/monitor/v1/reports"
REPORT_LINE = 'class="report-status">'


def authorized() -> dict[str, str]:
    return {"Authorization": f"Bearer {MONITOR_TOKEN}"}


async def entitle(env: Env, person: Person, slug: str, kind: str) -> None:
    """A service the person is entitled to. A game service also gets the person's identity on it, so it is theirs."""
    service = await make_service(env.db, slug, kind)

    def change(conn) -> None:
        services.add_entitlement(conn, service.id, "type", "adult", "grant", actor_id=person.id, now=NOW)
        if kind == "game":
            registry.add_identity(conn, person.id, slug, "java_name", "Steve")

    await env.db.run(change)


async def admin_hold(env: Env, person: Person) -> None:
    """Places an administrator hold on every service the person has."""
    await admin_client(env)
    admin = await env.db.run(lambda c: registry.get_by_uuid(c, ADMIN_SUB))

    def change(conn) -> None:
        holds.place(conn, actor=holds.Actor("admin", admin.id), target_id=person.id, service_ids=None, now=NOW,
                    window=timedelta(hours=24), admin_email=None)

    await env.db.run(change)


async def post_report(env: Env, *, slug: str, ref: str, observed: str, generation: int,
                      detail: str | None = None) -> httpx.Response:
    body = {"reports": [{"slug": slug, "ref": ref, "observed": observed, "detail": detail,
                         "generation": generation, "at": NOW.isoformat()}], "unmanaged": []}
    return await env.monitor_client.post(REPORTS_PATH, json=body, headers=authorized())


# AC8.3 ----------------------------------------------------------------------------------------------

async def test_a_ban_hallmonitor_did_not_request_reads_banned_in_game(env: Env) -> None:
    person, browser = await signed_in_person(env)
    await entitle(env, person, "minecraft", "game")
    await entitle(env, person, "calendar", "forward_auth")
    await post_report(env, slug="minecraft", ref=person.ref, observed="banned_external", generation=1)

    page = await browser.get("/")

    assert page.status_code == 200
    assert "banned in game" in page.text
    assert page.text.count(REPORT_LINE) == 1, "only the game row has a report line; forward auth rows have none"


# AC9.8 (display) ------------------------------------------------------------------------------------

async def test_a_report_from_an_older_generation_reads_not_yet_confirmed(env: Env) -> None:
    person, browser = await signed_in_person(env)
    await entitle(env, person, "minecraft", "game")
    await browser.get("/")
    await admin_hold(env, person)
    stored = await post_report(env, slug="minecraft", ref=person.ref, observed="suspended", generation=1)
    assert stored.status_code == 204

    page = await browser.get("/")

    minecraft = await env.db.run(lambda c: services.get_by_slug(c, "minecraft"))
    assert await env.db.run(lambda c: desired.report_for(c, person.id, minecraft.id)) is not None
    assert await env.db.run(lambda c: desired.desired_for(c, person.id, minecraft.id)) == ("suspended", 2)
    assert f'{REPORT_LINE}requested, not yet confirmed<' in page.text


# AC9.9 ----------------------------------------------------------------------------------------------

async def test_the_admin_monitor_page_shows_contact_time_errors_and_unmanaged_counts(env: Env) -> None:
    await admin_client(env)
    before = await env.client.get("/admin/monitor")
    assert before.status_code == 200
    assert "never" in before.text

    person, _ = await signed_in_person(env)
    await entitle(env, person, "minecraft", "game")
    await post_report(env, slug="minecraft", ref=person.ref, observed="error", generation=1, detail="unreachable")
    unmanaged = {"reports": [], "unmanaged": [{"slug": "minecraft", "count": 3}]}
    assert (await env.monitor_client.post(REPORTS_PATH, json=unmanaged, headers=authorized())).status_code == 204

    after = await env.client.get("/admin/monitor")

    assert after.status_code == 200
    assert "2026-11-13T12:00:00+00:00" in after.text
    assert "<td>Minecraft</td><td>1</td>" in after.text
    assert "<td>Minecraft</td><td>3</td>" in after.text


async def test_the_admin_monitor_page_is_refused_to_a_person_who_is_not_an_administrator(env: Env) -> None:
    _, browser = await signed_in_person(env)

    page = await browser.get("/admin/monitor")

    assert page.status_code == 403


# AC12.1 (display) -----------------------------------------------------------------------------------

async def test_an_unsupported_report_on_a_held_abiotic_row_reads_cannot_be_automatically_suspended(env: Env) -> None:
    person, browser = await signed_in_person(env)
    await entitle(env, person, "abiotic", "game")
    await browser.get("/")
    await admin_hold(env, person)
    await post_report(env, slug="abiotic", ref=person.ref, observed="unsupported", generation=2)

    page = await browser.get("/")

    assert f'{REPORT_LINE}cannot be automatically suspended<' in page.text
