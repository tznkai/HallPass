# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The monitor API over a real database: the desired body, the report store, token and proxy refusals, the public app's
silence, and the recorded contract example.

Covers hallpass.AC9.1 to AC9.4 and the contract shape in tests/contract/monitor_v1.json.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from hallpass import desired, grants, registry, services, terms
from hallpass.logs import FIELDS_ATTR
from hallpass.monitor_api import PROXY_HEADERS
from hallpass.registry import Person
from tests.helpers import ADMIN_SUB, MONITOR_TOKEN, NOW, Env, publish_terms

UUID_A = "00000000-0000-4000-8000-0000000000a1"
UUID_B = "00000000-0000-4000-8000-0000000000b2"
EXAMPLE = Path(__file__).parent / "contract" / "monitor_v1.json"
DESIRED_PATH = "/api/monitor/v1/desired"
REPORTS_PATH = "/api/monitor/v1/reports"
SUBJECT_KEYS = {"ref", "identities", "desired", "generation"}


def authorized() -> dict[str, str]:
    return {"Authorization": f"Bearer {MONITOR_TOKEN}"}


async def publisher(env: Env) -> Person:
    return await env.db.run(lambda c: registry.get_by_uuid(c, ADMIN_SUB) or registry.create_person(
        c, display_name="Publisher", account_type="adult", now=NOW))


async def service(env: Env, slug: str, *, kind: str, adapter: str = "minecraft") -> services.Service:
    """A service that adults may use, so their desired value is allowed."""
    actor = await publisher(env)

    def change(conn) -> services.Service:
        created = services.create_service(conn, slug=slug, name=slug.title(), kind=kind, adapter=adapter,
                                          auto_suspendable=True, url=None, actor_id=actor.id, now=NOW)
        services.add_entitlement(conn, created.id, "type", "adult", "grant", actor_id=actor.id, now=NOW)
        return created

    return await env.db.run(change)


async def person(env: Env, *, name: str, account_type: str = "adult", email: str | None = None,
                 phone: str | None = None, uuid: str | None = None) -> Person:
    def change(conn) -> Person:
        created = registry.create_person(conn, display_name=name, account_type=account_type, email=email,
                                         phone=phone, now=NOW)
        if uuid is not None:
            registry.bind_identity(conn, created.id, uuid=uuid, authentik_pk=1, username=name.lower().replace(" ", ""))
        if account_type != "child":
            grants.accept_terms(conn, created.id, terms.current(conn).id, NOW)
        return registry.get_person(conn, created.id)

    return await env.db.run(change)


async def seeded(env: Env) -> tuple[Person, Person, Person]:
    """Minecraft (a game) and Fluxer (an oidc service on the fluxer adapter), and calendar (forward auth). Ann has a
    minecraft identity and a bound IdP account; Bo has only a bound account; Cy has neither."""
    await publish_terms(env)
    await service(env, "minecraft", kind="game")
    await service(env, "fluxer", kind="oidc", adapter="fluxer")
    await service(env, "calendar", kind="forward_auth")
    ann = await person(env, name="Ann", uuid=UUID_A)
    await env.db.run(lambda c: registry.add_identity(c, ann.id, "minecraft", "java_name", "Steve"))
    bo = await person(env, name="Bo", uuid=UUID_B)
    cy = await person(env, name="Cy")
    return ann, bo, cy


async def stored_report(env: Env, person_id: int, slug: str) -> desired.Report | None:
    def read(conn) -> desired.Report | None:
        service_row = services.get_by_slug(conn, slug)
        return desired.report_for(conn, person_id, service_row.id)

    return await env.db.run(read)


async def read_desired(env: Env) -> None:
    """A desired read, which stores the pairs it sends. A report is stored only for a pair that has a desired row."""
    response = await env.monitor_client.get(DESIRED_PATH, headers=authorized())
    assert response.status_code == 200


# AC9.1 ----------------------------------------------------------------------------------------------

async def test_desired_lists_each_monitored_service_with_its_subjects(env: Env) -> None:
    ann, bo, _ = await seeded(env)

    response = await env.monitor_client.get(DESIRED_PATH, headers=authorized())

    assert response.status_code == 200
    by_slug = {entry["slug"]: entry for entry in response.json()["services"]}
    assert set(by_slug) == {"minecraft", "fluxer"}
    assert by_slug["fluxer"]["adapter"] == "fluxer"
    assert by_slug["minecraft"]["subjects"] == [{
        "ref": ann.ref, "identities": [{"kind": "java_name", "value": "Steve"}],
        "desired": "allowed", "generation": 1,
    }]
    assert by_slug["fluxer"]["subjects"] == [
        {"ref": ann.ref, "identities": [{"kind": "sso_subject", "value": UUID_A}], "desired": "allowed",
         "generation": 1},
        {"ref": bo.ref, "identities": [{"kind": "sso_subject", "value": UUID_B}], "desired": "allowed",
         "generation": 1},
    ]


async def test_each_desired_request_records_the_contact_time(env: Env) -> None:
    await seeded(env)

    await env.monitor_client.get(DESIRED_PATH, headers=authorized())

    contact = await env.db.run(lambda c: c.execute("SELECT last_contact_at FROM monitor_contact").fetchone())
    assert contact == ("2026-11-13T12:00:00+00:00",)


# AC9.2 ----------------------------------------------------------------------------------------------

async def test_reports_are_stored_per_person_and_service_and_unknown_refs_are_skipped(env: Env) -> None:
    ann, _, _ = await seeded(env)
    await read_desired(env)
    body = {
        "reports": [
            {"slug": "minecraft", "ref": ann.ref, "observed": "allowed", "detail": None, "generation": 1,
             "at": "2026-11-13T12:00:00Z"},
            {"slug": "minecraft", "ref": "unknown-ref", "observed": "allowed", "generation": 1,
             "at": "2026-11-13T12:00:00Z"},
        ],
        "unmanaged": [{"slug": "minecraft", "count": 2}],
    }

    response = await env.monitor_client.post(REPORTS_PATH, json=body, headers=authorized())

    assert response.status_code == 204
    assert await stored_report(env, ann.id, "minecraft") == desired.Report(
        observed="allowed", detail=None, generation=1, reported_at="2026-11-13T12:00:00+00:00")
    unmanaged = await env.db.run(lambda c: c.execute(
        "SELECT count FROM service_unmanaged JOIN service ON service.id = service_id WHERE slug = 'minecraft'"
    ).fetchone())
    assert unmanaged == (2,)


async def test_an_older_report_does_not_replace_a_newer_one(env: Env) -> None:
    ann, _, _ = await seeded(env)
    await read_desired(env)
    newer ={"reports": [{"slug": "minecraft", "ref": ann.ref, "observed": "allowed", "generation": 1,
                          "at": "2026-11-13T12:00:00Z"}]}
    older = {"reports": [{"slug": "minecraft", "ref": ann.ref, "observed": "suspended", "generation": 1,
                          "at": "2026-11-13T11:00:00Z"}]}
    await env.monitor_client.post(REPORTS_PATH, json=newer, headers=authorized())

    response = await env.monitor_client.post(REPORTS_PATH, json=older, headers=authorized())

    assert response.status_code == 204
    assert (await stored_report(env, ann.id, "minecraft")).observed == "allowed"


async def test_a_report_dated_ahead_of_the_clock_is_clamped_so_a_later_correct_report_still_replaces_it(
        env: Env) -> None:
    ann, _, _ = await seeded(env)
    await read_desired(env)
    ahead ={"reports": [{"slug": "minecraft", "ref": ann.ref, "observed": "suspended", "generation": 1,
                          "at": "2026-11-14T12:00:00Z"}]}
    assert (await env.monitor_client.post(REPORTS_PATH, json=ahead, headers=authorized())).status_code == 204
    assert (await stored_report(env, ann.id, "minecraft")).reported_at == "2026-11-13T12:00:00+00:00"

    env.clock.set(NOW + timedelta(minutes=5))
    later = {"reports": [{"slug": "minecraft", "ref": ann.ref, "observed": "allowed", "generation": 1,
                          "at": "2026-11-13T12:05:00Z"}]}
    assert (await env.monitor_client.post(REPORTS_PATH, json=later, headers=authorized())).status_code == 204

    report = await stored_report(env, ann.id, "minecraft")
    assert (report.observed, report.reported_at) == ("allowed", "2026-11-13T12:05:00+00:00")


def report_body(ref: str, *, generation: int, observed: str = "allowed") -> dict:
    return {"reports": [{"slug": "minecraft", "ref": ref, "observed": observed, "generation": generation,
                         "at": "2026-11-13T12:00:00Z"}]}


async def test_a_report_for_a_pair_with_no_desired_row_is_skipped_and_not_stored(env: Env) -> None:
    ann, _, _ = await seeded(env)

    response = await env.monitor_client.post(REPORTS_PATH, json=report_body(ann.ref, generation=1),
                                             headers=authorized())

    assert response.status_code == 204
    assert await stored_report(env, ann.id, "minecraft") is None


async def test_a_report_above_the_stored_generation_is_skipped_and_one_at_it_is_stored(env: Env) -> None:
    ann, _, _ = await seeded(env)
    await read_desired(env)

    ahead = await env.monitor_client.post(REPORTS_PATH, json=report_body(ann.ref, generation=2),
                                          headers=authorized())
    assert ahead.status_code == 204
    assert await stored_report(env, ann.id, "minecraft") is None

    at = await env.monitor_client.post(REPORTS_PATH, json=report_body(ann.ref, generation=1),
                                       headers=authorized())
    assert at.status_code == 204
    assert (await stored_report(env, ann.id, "minecraft")).generation == 1


async def test_a_late_report_for_a_subject_that_was_pruned_and_returned_is_not_stored(env: Env) -> None:
    ann, _, _ = await seeded(env)
    await read_desired(env)
    await env.monitor_client.post(REPORTS_PATH, json=report_body(ann.ref, generation=1), headers=authorized())

    def remove_minecraft(conn) -> None:
        for identity in registry.identities_of(conn, ann.id):
            if identity.service_slug == "minecraft":
                registry.remove_identity(conn, identity.id)

    await env.db.run(remove_minecraft)
    await read_desired(env)
    assert await stored_report(env, ann.id, "minecraft") is None
    await env.db.run(lambda c: registry.add_identity(c, ann.id, "minecraft", "java_name", "Steve"))
    await read_desired(env)

    late = await env.monitor_client.post(REPORTS_PATH, json=report_body(ann.ref, generation=2, observed="suspended"),
                                         headers=authorized())

    assert late.status_code == 204
    assert await stored_report(env, ann.id, "minecraft") is None


async def test_skipped_items_are_counted_and_logged_by_reason(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    ann, _, _ = await seeded(env)
    caplog.set_level(logging.INFO)
    body = {
        "reports": [
            {"slug": "minecraft", "ref": "unknown-ref", "observed": "allowed", "generation": 1,
             "at": "2026-11-13T12:00:00Z"},
            {"slug": "no-such-service", "ref": ann.ref, "observed": "allowed", "generation": 1,
             "at": "2026-11-13T12:00:00Z"},
        ],
        "unmanaged": [{"slug": "no-such-service", "count": 4}, {"slug": "also-missing", "count": 1}],
    }

    response = await env.monitor_client.post(REPORTS_PATH, json=body, headers=authorized())

    assert response.status_code == 204
    skipped = [getattr(record, FIELDS_ATTR) for record in caplog.records
               if record.getMessage() == "monitor_reports_skipped"]
    assert skipped == [{"count": 1, "reason": "unknown_ref"}, {"count": 3, "reason": "unknown_slug"}]
    assert await env.db.run(lambda c: c.execute("SELECT COUNT(*) FROM service_unmanaged").fetchone()) == (0,)


async def test_a_report_with_no_timezone_is_refused(env: Env) -> None:
    ann, _, _ = await seeded(env)
    body = {"reports": [{"slug": "minecraft", "ref": ann.ref, "observed": "allowed", "generation": 1,
                         "at": "2026-11-13T12:00:00"}]}

    response = await env.monitor_client.post(REPORTS_PATH, json=body, headers=authorized())

    assert response.status_code == 422
    assert await stored_report(env, ann.id, "minecraft") is None


# AC9.3 ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("method", "path"), [("GET", DESIRED_PATH), ("POST", REPORTS_PATH)])
async def test_a_missing_or_wrong_token_is_refused_with_401(env: Env, method: str, path: str) -> None:
    await seeded(env)
    payload: dict[str, Any] = {"json": {"reports": []}} if method == "POST" else {}
    for headers in ({}, {"Authorization": "Bearer wrong-token-value-that-is-long-enough-1234"},
                    {"Authorization": MONITOR_TOKEN}):
        response = await env.monitor_client.request(method, path, headers=headers, **payload)
        assert response.status_code == 401, headers.keys()


@pytest.mark.parametrize(("method", "path"), [("GET", DESIRED_PATH), ("POST", REPORTS_PATH)])
async def test_a_proxy_header_is_refused_with_403_even_with_the_token(env: Env, method: str, path: str) -> None:
    await seeded(env)
    payload: dict[str, Any] = {"json": {"reports": []}} if method == "POST" else {}
    for header in PROXY_HEADERS:
        response = await env.monitor_client.request(method, path, headers={**authorized(), header: "127.0.0.1"},
                                                    **payload)
        assert response.status_code == 403, header


async def test_the_public_app_does_not_serve_the_monitor_api(env: Env) -> None:
    await seeded(env)
    assert (await env.client.get(DESIRED_PATH, headers=authorized())).status_code == 404
    assert (await env.client.post(REPORTS_PATH, json={"reports": []}, headers=authorized())).status_code == 404


# AC9.4 ----------------------------------------------------------------------------------------------

async def test_the_desired_response_carries_no_name_contact_account_type_or_parent_link(env: Env) -> None:
    await publish_terms(env)
    await service(env, "minecraft", kind="game")
    await service(env, "fluxer", kind="oidc", adapter="fluxer")
    parent = await person(env, name="Mum", account_type="parent", email="mum@example.com")
    child = await person(env, name="Zebulon Quux", account_type="child", email="zq@example.com", phone="5550100",
                         uuid=UUID_A)
    await env.db.run(lambda c: registry.link_parent(c, child.id, parent.id, NOW))
    await env.db.run(lambda c: registry.add_identity(c, child.id, "minecraft", "java_name", "Steve"))

    response = await env.monitor_client.get(DESIRED_PATH, headers=authorized())

    assert response.status_code == 200
    for leaked in ("Zebulon Quux", "zq@example.com", "5550100", "mum@example.com", "Mum", "child", "adult", "parent",
                    "account_type"):
        assert leaked not in response.text, leaked
    subjects = [subject for entry in response.json()["services"] for subject in entry["subjects"]]
    assert subjects
    assert all(set(subject) == SUBJECT_KEYS for subject in subjects)


# Contract -------------------------------------------------------------------------------------------

def assert_same_shape(actual: Any, expected: Any, path: str = "$") -> None:
    """Compares the keys of every object recursively, the first item of each list, and the type of each value. A null in
    the example allows any value."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        assert set(actual) == set(expected), path
        for key in expected:
            assert_same_shape(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and actual, path
        for index, item in enumerate(actual):
            assert_same_shape(item, expected[0], f"{path}[{index}]")
    elif expected is not None:
        assert type(actual) is type(expected), path


async def test_the_desired_body_has_the_shape_of_the_recorded_example(env: Env) -> None:
    await seeded(env)
    example = json.loads(EXAMPLE.read_text("utf-8"))

    response = await env.monitor_client.get(DESIRED_PATH, headers=authorized())

    assert_same_shape(response.json(), example["desired"])


async def test_the_recorded_reports_example_is_accepted_with_its_refs_replaced(env: Env) -> None:
    ann, _, _ = await seeded(env)
    await read_desired(env)
    body = json.loads(EXAMPLE.read_text("utf-8"))["reports"]
    for report in body["reports"]:
        report["ref"] = ann.ref

    response = await env.monitor_client.post(REPORTS_PATH, json=body, headers=authorized())

    assert response.status_code == 204
    # The example's minecraft report is at generation 3, above the generation 1 this seeded pair holds, so it is
    # skipped. The unmanaged count in the same body is still stored.
    assert await stored_report(env, ann.id, "minecraft") is None
    unmanaged = await env.db.run(lambda c: c.execute(
        "SELECT count FROM service_unmanaged JOIN service ON service.id = service_id WHERE slug = 'minecraft'"
    ).fetchone())
    assert unmanaged == (1,)
