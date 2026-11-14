# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The email outbox and its SMTP sender: a failed send is kept and retried, and never undoes the decision that queued
it (hallpass.AC13.5). Addresses and subjects never reach the log."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
import pytest

from hallpass import mail, registry
from hallpass.config import Config
from hallpass.db import Database
from hallpass.registry import _iso
from tests.helpers import ENV

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TO = "ann@example.com"
SUBJECT = "Hold placed on the calendar"
BODY = "Someone changed access for your child. Sign in for details."


class SMTPConnectError(Exception):
    """Stands in for an SMTP failure; the outbox records only this class name."""


class RecordingSender:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send(self, *, to_addr: str, subject: str, body: str) -> None:
        self.calls.append((to_addr, subject, body))


class FailingSender(RecordingSender):
    """Raises on each of its first `failures` calls, then delivers."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self._failures = failures

    async def send(self, *, to_addr: str, subject: str, body: str) -> None:
        if self._failures > 0:
            self._failures -= 1
            raise SMTPConnectError("connection refused by the test server")
        await super().send(to_addr=to_addr, subject=subject, body=body)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "hp.db")
    try:
        yield database
    finally:
        database.close()


async def queue_with_decision(db: Database, *, to_addr: str = TO, subject: str = SUBJECT) -> int:
    """Queues one message in the same transaction as a decision_log row, as a decision will do."""
    def change(conn) -> int:
        registry.log_decision(conn, at=NOW, actor_kind="admin", actor_person_id=None, target_person_id=None,
                              action="hold_placed")
        return mail.enqueue(conn, to_addr=to_addr, subject=subject, body=BODY, now=NOW)

    return await db.run(change)


async def outbox_row(db: Database, row_id: int) -> tuple:
    return await db.run(lambda c: c.execute(
        "SELECT sent_at, attempts, next_attempt_at, last_error FROM outbox WHERE id = ?", (row_id,)).fetchone())


async def decision_count(db: Database) -> int:
    return await db.run(lambda c: c.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0])


# AC13.5: a failed send is kept and retried, and the decision stays --------------------------------

async def test_failed_send_keeps_the_message_queued_and_the_decision_committed(db: Database) -> None:
    row_id = await queue_with_decision(db)
    sender = FailingSender(failures=1)

    assert await mail.deliver_due(db, sender, NOW) == (0, 1)

    sent_at, attempts, next_attempt_at, last_error = await outbox_row(db, row_id)
    assert sent_at is None
    assert attempts == 1
    assert next_attempt_at == _iso(NOW + timedelta(minutes=1))
    assert last_error == "SMTPConnectError"
    assert await decision_count(db) == 1


async def test_a_row_is_not_resent_before_its_retry_time(db: Database) -> None:
    await queue_with_decision(db)
    sender = FailingSender(failures=1)
    await mail.deliver_due(db, sender, NOW)

    assert await mail.deliver_due(db, sender, NOW) == (0, 0)
    assert len(sender.calls) == 0


async def test_the_retry_sends_once_the_row_is_due(db: Database) -> None:
    row_id = await queue_with_decision(db)
    sender = FailingSender(failures=1)
    await mail.deliver_due(db, sender, NOW)

    assert await mail.deliver_due(db, sender, NOW + timedelta(minutes=2)) == (1, 0)

    sent_at, *_ = await outbox_row(db, row_id)
    assert sent_at == _iso(NOW + timedelta(minutes=2))
    assert sender.calls == [(TO, SUBJECT, BODY)]


async def test_a_row_stops_being_selected_after_max_attempts(db: Database) -> None:
    row_id = await queue_with_decision(db)
    sender = FailingSender(failures=10**6)

    assert await mail.deliver_due(db, sender, NOW, max_attempts=2) == (0, 1)
    assert await mail.deliver_due(db, sender, NOW + timedelta(minutes=5), max_attempts=2) == (0, 1)
    assert await mail.deliver_due(db, sender, NOW + timedelta(days=30), max_attempts=2) == (0, 0)

    _, attempts, _, _ = await outbox_row(db, row_id)
    assert attempts == 2
    assert len(sender.calls) == 0


async def test_backoff_doubles_then_is_capped_at_six_hours(db: Database) -> None:
    row_id = await queue_with_decision(db)
    sender = FailingSender(failures=10**6)
    moment = NOW
    gaps: list[timedelta] = []
    for _ in range(10):
        await mail.deliver_due(db, sender, moment, max_attempts=50)
        _, _, next_attempt_at, _ = await outbox_row(db, row_id)
        next_moment = datetime.fromisoformat(next_attempt_at)
        gaps.append(next_moment - moment)
        moment = next_moment

    assert gaps[:4] == [timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=4), timedelta(minutes=8)]
    assert gaps[-1] == timedelta(hours=6)


# NullSender and logging --------------------------------------------------------------------------

async def test_null_sender_leaves_mail_queued_untouched(db: Database) -> None:
    row_id = await queue_with_decision(db)

    assert await mail.deliver_due(db, mail.NullSender(), NOW) == (0, 0)

    sent_at, attempts, _, last_error = await outbox_row(db, row_id)
    assert (sent_at, attempts, last_error) == (None, 0, None)


def test_null_sender_logs_mail_disabled_once_on_construction(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="hallpass.mail"):
        mail.NullSender()
    assert [record.getMessage() for record in caplog.records if record.name == "hallpass.mail"] == ["mail_disabled"]


async def test_failure_log_never_contains_the_address_or_the_subject(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    await queue_with_decision(db, to_addr="parent.person@example.com", subject="Your child's access changed")

    with caplog.at_level(logging.DEBUG, logger="hallpass.mail"):
        await mail.deliver_due(db, FailingSender(failures=1), NOW)

    messages = [record.getMessage() for record in caplog.records if record.name == "hallpass.mail"]
    assert "mail_failed" in messages
    assert "parent.person@example.com" not in caplog.text
    assert "Your child's access changed" not in caplog.text


# The SMTP sender ----------------------------------------------------------------------------------

async def test_smtp_sender_builds_the_message_and_passes_the_settings_to_aiosmtplib(monkeypatch) -> None:
    calls: list[tuple[EmailMessage, dict]] = []

    async def fake_send(message, **kwargs):
        calls.append((message, kwargs))
        return {}, "ok"

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    sender = mail.SmtpSender(host="smtp.hallpass.invalid", port=2525, username="mailer", password="pw-for-tests",
                             sender_from="hallpass@hallpass.invalid", starttls=False)

    await sender.send(to_addr=TO, subject=SUBJECT, body=BODY)

    (message, kwargs), = calls
    assert message["To"] == TO
    assert message["From"] == "hallpass@hallpass.invalid"
    assert message["Subject"] == SUBJECT
    assert message.get_content().strip() == BODY
    assert kwargs == {"hostname": "smtp.hallpass.invalid", "port": 2525, "username": "mailer",
                      "password": "pw-for-tests", "start_tls": False}


def test_build_sender_is_null_without_a_host_and_smtp_otherwise() -> None:
    assert isinstance(mail.build_sender(Config.from_env(ENV)), mail.NullSender)
    configured = Config.from_env({**ENV, "SMTP_HOST": "smtp.hallpass.invalid", "SMTP_FROM": "hallpass@hallpass.invalid"})
    assert isinstance(mail.build_sender(configured), mail.SmtpSender)


# The loop -----------------------------------------------------------------------------------------

class FlakyClock:
    """Raises on its first call, then reads a fixed moment, so the loop must survive one failed pass."""

    def __init__(self) -> None:
        self._failed = False

    def __call__(self) -> datetime:
        if not self._failed:
            self._failed = True
            raise RuntimeError("clock failed once")
        return NOW


class LoopDeps:
    def __init__(self, db: Database, sender: RecordingSender) -> None:
        self.db = db
        self.sender = sender
        self.clock = FlakyClock()


async def test_mail_loop_survives_a_failed_pass_and_keeps_delivering(db: Database) -> None:
    row_id = await queue_with_decision(db)
    sender = RecordingSender()
    task = asyncio.create_task(mail.mail_loop(LoopDeps(db, sender), interval=0))
    try:
        for _ in range(500):
            if (await outbox_row(db, row_id))[0] is not None:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert sender.calls == [(TO, SUBJECT, BODY)]
