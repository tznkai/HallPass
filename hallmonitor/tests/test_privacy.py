# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HallMonitor holds no names, contacts or parent links, and its logs carry no identity value and no credential.

Structural guards read the migration SQL, the config fields and the subject type. The behavioural guard runs a full
cycle with a logging handler that has no redaction filter, so nothing is hidden by a backstop: the log text must be
clean by construction. The third-party HTTP loggers are pinned to WARNING, as `setup_logging` does in production,
because httpx would otherwise log every request URL, and the Fluxer lookup URL carries the Authentik uuid.

Covers hallmonitor's part of hallpass.AC13.1 and AC13.2.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from hallmonitor.adapters.fluxer import FluxerAdapter
from hallmonitor.client import MonitorClient
from hallmonitor.config import Config
from hallmonitor.contract import Subject
from hallmonitor.logs import PINNED_LOGGERS, JsonFormatter, log_event
from hallmonitor.reconcile import cycle
from tests.fake_adapter import ListAdapter
from tests.fake_fluxer_admin import FakeFluxerAdminState, add_user, link_sso, sign_in
from tests.fake_fluxer_admin import make_transport as make_fluxer_transport
from tests.fake_hallpass import FakeHallpassState
from tests.fake_hallpass import make_transport as make_hallpass_transport

PROJECT = Path(__file__).resolve().parents[1]
MIGRATIONS = PROJECT / "src" / "hallmonitor" / "migrations"
PERSONAL_COLUMNS = re.compile(r"name|email|phone|parent|contact|display", re.IGNORECASE)
COLUMN_DEFINITION = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(?:INTEGER|TEXT)\b", re.IGNORECASE | re.MULTILINE)
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
HALLPASS_URL = "https://hallpass.invalid"
FLUXER_ADMIN_URL = "https://fluxer.invalid"
HALLPASS_TOKEN = "SENTINEL-HALLPASS-TOKEN"
FLUXER_KEY = "SENTINEL-FLUXER-KEY"
PROVIDER = "prov-1"
STEVE = "Steve"
FLUXER_USER_ID = "1001"
SSO_SUBJECT = "00000000-0000-4000-8000-00000000abcd"
IDENTITY_VALUES = (STEVE, FLUXER_USER_ID, SSO_SUBJECT)


# AC13.1 (structural) --------------------------------------------------------------------------------

def test_the_migrations_define_no_personal_column() -> None:
    columns = [name for path in sorted(MIGRATIONS.glob("*.sql"))
               for name in COLUMN_DEFINITION.findall(path.read_text("utf-8"))]
    assert "slug" in columns, "the column scan found the schema"
    assert [name for name in columns if PERSONAL_COLUMNS.search(name)] == []


def test_the_config_has_no_personal_field() -> None:
    names = [field.name for field in dataclasses.fields(Config)]
    assert "hallpass_url" in names, "the field scan found the config"
    assert [name for name in names if PERSONAL_COLUMNS.search(name)] == []


def test_a_subject_carries_only_the_wire_fields_and_no_personal_data() -> None:
    assert {field.name for field in dataclasses.fields(Subject)} == {"ref", "identities", "desired", "generation"}


# AC13.2 (behavioural) -------------------------------------------------------------------------------

def subject(ref: str, identities: list[tuple[str, str]], desired: str, generation: int) -> dict[str, object]:
    return {"ref": ref, "identities": [{"kind": kind, "value": value} for kind, value in identities],
            "desired": desired, "generation": generation}


async def test_a_full_cycle_logs_no_identity_value_and_no_credential() -> None:
    fluxer_state = FakeFluxerAdminState(key=FLUXER_KEY)
    add_user(fluxer_state, FLUXER_USER_ID)
    add_user(fluxer_state, "1002")
    link_sso(fluxer_state, PROVIDER, SSO_SUBJECT, "1002")
    sign_in(fluxer_state, FLUXER_USER_ID)
    sign_in(fluxer_state, "1002")
    fluxer_state.fail_next["sessions_delete"] = 500   # the first held account's session delete fails
    hallpass_state = FakeHallpassState(token=HALLPASS_TOKEN, services=[
        {"slug": "minecraft", "adapter": "list", "subjects": [
            subject("m1", [("java_name", STEVE)], "suspended", 4),
        ]},
        {"slug": "valheim", "adapter": "list", "subjects": [
            subject("v1", [("java_name", STEVE)], "allowed", 1),
        ]},
        {"slug": "fluxer", "adapter": "fluxer", "subjects": [
            subject("f1", [("fluxer_user_id", FLUXER_USER_ID)], "suspended", 2),
            subject("f2", [("sso_subject", SSO_SUBJECT)], "suspended", 2),
        ]},
    ])
    http = httpx.AsyncClient(transport=make_fluxer_transport(fluxer_state))
    client = MonitorClient(HALLPASS_URL, HALLPASS_TOKEN, transport=make_hallpass_transport(hallpass_state))
    adapters = {
        "minecraft": ListAdapter(allow={STEVE}),
        "valheim": ListAdapter(unreachable=True),
        "fluxer": FluxerAdapter(http=http, base_url=FLUXER_ADMIN_URL, key=FLUXER_KEY, sso_provider_id=PROVIDER),
    }
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous_level = root.level
    pinned = {name: logging.getLogger(name).level for name in PINNED_LOGGERS}
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    for name in PINNED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        log_event(logging.getLogger("hallmonitor.test"), "guard_started", count=len(IDENTITY_VALUES))
        await cycle(client=client, adapter_for=lambda s: adapters.get(s.slug), now=NOW)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        for name, level in pinned.items():
            logging.getLogger(name).setLevel(level)
        await client.aclose()
        await http.aclose()

    logged = captured.getvalue()
    assert '"event": "cycle_done"' in logged, "the cycle logged, so the check below is not vacuous"
    assert '"event": "observe_failed"' in logged, "the failure path of an unreachable service was logged"
    assert '"event": "sessions_not_ended"' in logged, "the failure path of a session delete was logged"
    assert [report["observed"] for report in hallpass_state.reports] == [
        "suspended", "error", "error", "suspended",   # m1 applied, v1 unreachable, f1 delete failed, f2 applied
    ]
    for value in (*IDENTITY_VALUES, HALLPASS_TOKEN, FLUXER_KEY):
        assert value not in logged
