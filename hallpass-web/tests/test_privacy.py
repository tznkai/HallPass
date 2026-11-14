# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hallpass holds no service credentials and logs no personal data.

Structural guards read the config fields, the .env template and the migration columns. The behavioural guard runs a
full flow with a logging handler that has no redaction filter, so nothing is hidden by a backstop: the log text must
be clean by construction.

Covers hallpass.AC13.1 and AC13.2 (the hallpass part).
"""

from __future__ import annotations

import dataclasses
import io
import logging
import re
from datetime import timedelta
from pathlib import Path

from hallpass import db as dbmod
from hallpass import holds, mail, registry, services
from hallpass.config import Config
from hallpass.logs import JsonFormatter
from tests.helpers import ADMIN_SUB, MONITOR_TOKEN, NOW, Env, admin_client, form, signed_in_person

PROJECT = Path(__file__).resolve().parents[1]
SERVICE_CREDENTIAL_NAMES = re.compile(r"rcon|pterodactyl|ptero|fluxer_admin|palworld|valheim|steam_api", re.IGNORECASE)
CREDENTIAL_COLUMNS = re.compile(r"password|api_key|rcon", re.IGNORECASE)
TOKEN_IN_URL = re.compile(r"token=([A-Za-z0-9_-]+)")
COLUMN_DEFINITION = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(?:INTEGER|TEXT)\b", re.IGNORECASE | re.MULTILINE)
ENV_NAME = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)
ZEBULON = "Zebulon Quux"
ZQ_EMAIL = "zq@example.com"
MUM_EMAIL = "mum@example.com"
FEED_URL = "https://feed.hallpass.test/calendar.ics"


# AC13.1 (structural) --------------------------------------------------------------------------------

def test_the_config_has_no_field_for_a_service_credential() -> None:
    names = [field.name for field in dataclasses.fields(Config)]
    assert [name for name in names if SERVICE_CREDENTIAL_NAMES.search(name)] == []


def test_the_env_template_names_no_service_credential_variable() -> None:
    names = ENV_NAME.findall((PROJECT / ".env.example").read_text("utf-8"))
    assert names, "the template lists variables"
    assert [name for name in names if SERVICE_CREDENTIAL_NAMES.search(name)] == []


def test_the_migrations_define_no_credential_column() -> None:
    columns = [name for _, sql in dbmod.load_migrations() for name in COLUMN_DEFINITION.findall(sql)]
    assert "person_id" in columns, "the column scan found the schema"
    assert [name for name in columns if CREDENTIAL_COLUMNS.search(name)] == []


# AC13.2 (behavioural) -------------------------------------------------------------------------------

async def service(env: Env, admin_id: int, slug: str, kind: str, *, url: str | None = None) -> services.Service:
    """A service the child is entitled to, created through the store."""
    def change(conn) -> services.Service:
        created = services.create_service(conn, slug=slug, name=slug.title(), kind=kind, adapter="minecraft",
                                          auto_suspendable=True, url=url, actor_id=admin_id, now=NOW)
        services.add_entitlement(conn, created.id, "type", "child", "grant", actor_id=admin_id, now=NOW)
        return created

    return await env.db.run(change)


async def run_flow(env: Env) -> tuple[str, list[str]]:
    """Creates a parent and a child, signs both in, places and lifts holds, issues a feed token, delivers mail and calls
    the monitor API. Returns the raw feed token and the subject of each mail delivered."""
    await admin_client(env)
    admin = await env.db.run(lambda c: registry.get_by_uuid(c, ADMIN_SUB))
    parent, _ = await signed_in_person(env, account_type="parent", display_name="Mum Quux", email=MUM_EMAIL)
    child, child_browser = await signed_in_person(env, account_type="child", display_name=ZEBULON, email=ZQ_EMAIL)
    await env.db.run(lambda c: registry.link_parent(c, child.id, parent.id, NOW))
    calendar = await service(env, admin.id, "calendar", "forward_auth")
    minecraft = await service(env, admin.id, "minecraft", "game")
    feed = await service(env, admin.id, "feed", "feed", url=FEED_URL)
    await env.db.run(lambda c: registry.add_identity(c, child.id, "minecraft", "java_name", "Steve"))
    window = timedelta(hours=24)

    def admin_places(conn) -> None:
        holds.place(conn, actor=holds.Actor("admin", admin.id), target_id=child.id, service_ids=[calendar.id],
                    now=NOW, window=window, admin_email=None)

    def parent_places(conn) -> None:
        holds.place(conn, actor=holds.Actor("parent", parent.id), target_id=child.id, service_ids=[minecraft.id],
                    now=NOW, window=window, admin_email=None)

    def admin_lifts(conn) -> None:
        holds.lift(conn, actor=holds.Actor("admin", admin.id), target_id=child.id,
                   service_ids=[calendar.id, minecraft.id], now=NOW, window=window, admin_email=None,
                   include_parent=True)

    await env.db.run(admin_places)
    await env.db.run(parent_places)
    await env.db.run(admin_lifts)

    fields = await form(env, "/", client=child_browser)
    issued = await child_browser.post(f"/tokens/feed/{feed.id}", data=fields)
    match = TOKEN_IN_URL.search(issued.text)
    assert match is not None, "the feed token was issued"

    await mail.deliver_due(env.db, env.sender, NOW)
    subjects = [subject for _, subject, _ in env.sender.calls]

    desired = await env.monitor_client.get("/api/monitor/v1/desired", headers={"Authorization": f"Bearer {MONITOR_TOKEN}"})
    assert desired.status_code == 200
    report = {"reports": [{"slug": "minecraft", "ref": child.ref, "observed": "error", "detail": "unreachable",
                           "generation": 1, "at": NOW.isoformat()}], "unmanaged": []}
    posted = await env.monitor_client.post("/api/monitor/v1/reports", json=report,
                                           headers={"Authorization": f"Bearer {MONITOR_TOKEN}"})
    assert posted.status_code == 204
    return match.group(1), subjects


async def test_a_full_flow_logs_no_name_contact_token_secret_or_mail_subject(env: Env) -> None:
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        raw_token, subjects = await run_flow(env)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    logged = captured.getvalue()
    assert '"event": "monitor_desired"' in logged, "the flow logged, so the check below is not vacuous"
    assert subjects, "mail was delivered through the sender"
    secrets = [ZEBULON, ZQ_EMAIL, MUM_EMAIL, raw_token, MONITOR_TOKEN, env.config.session_secret, *subjects]
    for secret in secrets:
        assert secret not in logged
