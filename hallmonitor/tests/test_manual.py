# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The manual adapter: it observes and applies nothing, and says so."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from hallmonitor.adapters.base import Observed
from hallmonitor.adapters.manual import ManualAdapter
from hallmonitor.adapters.registry import ADAPTERS, AdapterContext, build_adapter
from hallmonitor.config import Config
from hallmonitor.contract import Identity, Subject
from hallmonitor.db import Database


def subject(ref: str, desired: str = "suspended") -> Subject:
    return Subject(ref=ref, identities=(Identity("java_name", "Steve"),), desired=desired, generation=1)


async def test_observe_reports_unsupported_for_every_subject() -> None:
    observed = await ManualAdapter().observe([subject("ref-a"), subject("ref-b", "allowed")])
    assert observed == {"ref-a": Observed("unsupported"), "ref-b": Observed("unsupported")}


async def test_apply_returns_unsupported_and_changes_nothing() -> None:
    assert await ManualAdapter().apply(subject("ref-a"), "suspended") == Observed("unsupported")
    assert await ManualAdapter().apply(subject("ref-a"), "allowed") == Observed("unsupported")


async def test_unmanaged_count_is_not_known() -> None:
    assert await ManualAdapter().unmanaged_count([subject("ref-a")]) is None


def test_no_capability_is_claimed() -> None:
    caps = ManualAdapter.capabilities
    assert not (caps.suspend or caps.restore or caps.read_state or caps.removes_connected)


async def test_registry_builds_the_manual_adapter(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "hallmonitor.db")
    try:
        async with httpx.AsyncClient() as http:
            ctx = AdapterContext(
                slug="abiotic",
                settings={},
                config=Config(hallpass_url="https://hallpass.invalid", hallpass_token="t" * 40),
                db=db,
                http=http,
                clock=lambda: datetime(2026, 11, 13, tzinfo=UTC),
            )
            assert isinstance(build_adapter("manual", ctx), ManualAdapter)
            assert build_adapter("no-such-adapter", ctx) is None
    finally:
        db.close()
    assert "manual" in ADAPTERS
