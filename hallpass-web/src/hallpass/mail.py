# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The email outbox and its SMTP sender. A decision queues its mail in the same transaction as the decision, so a
failed send can be retried and never undoes the decision. Addresses and subjects are never logged."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import partial
from typing import Protocol

import aiosmtplib

from hallpass.config import Config
from hallpass.db import Database
from hallpass.logs import log_event
from hallpass.registry import _iso

log = logging.getLogger(__name__)
BATCH_SIZE = 50
BACKOFF_CAP = timedelta(hours=6)
_MAX_BACKOFF_EXPONENT = 16   # 2 ** 16 minutes is already far past the cap, and keeps timedelta in range


class Sender(Protocol):
    enabled: bool

    async def send(self, *, to_addr: str, subject: str, body: str) -> None: ...


def enqueue(conn: sqlite3.Connection, *, to_addr: str, subject: str, body: str, now: datetime) -> int:
    """Queues one message for delivery. Call it inside the decision's transaction."""
    stamp = _iso(now)
    return conn.execute(
        "INSERT INTO outbox (to_addr, subject, body, created_at, next_attempt_at) VALUES (?, ?, ?, ?, ?)",
        (to_addr, subject, body, stamp, stamp),
    ).lastrowid


class NullSender:
    """Used when no SMTP host is configured: mail stays queued, and nothing is sent."""

    enabled = False

    def __init__(self) -> None:
        log_event(log, "mail_disabled", level=logging.WARNING)

    async def send(self, *, to_addr: str, subject: str, body: str) -> None:
        raise RuntimeError("NullSender never sends; deliver_due does not call it")


@dataclass(frozen=True)
class SmtpSender:
    host: str
    port: int
    username: str | None
    password: str | None = field(repr=False)
    sender_from: str
    starttls: bool = True
    enabled: bool = True

    async def send(self, *, to_addr: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender_from
        message["To"] = to_addr
        message["Subject"] = subject
        message.set_content(body)
        await aiosmtplib.send(message, hostname=self.host, port=self.port, username=self.username,
                              password=self.password, start_tls=self.starttls)


def build_sender(config: Config) -> Sender:
    if config.smtp_host is None or config.smtp_from is None:
        return NullSender()
    return SmtpSender(host=config.smtp_host, port=config.smtp_port, username=config.smtp_username,
                      password=config.smtp_password, sender_from=config.smtp_from, starttls=config.smtp_starttls)


def _backoff(attempts_before: int) -> timedelta:
    """One minute after the first failure, doubling each time, capped at BACKOFF_CAP."""
    return min(timedelta(minutes=2 ** min(attempts_before, _MAX_BACKOFF_EXPONENT)), BACKOFF_CAP)


async def deliver_due(db: Database, sender: Sender, now: datetime, *, max_attempts: int = 10) -> tuple[int, int]:
    """Sends each due message, oldest first, and returns (sent, failed). Each row is updated in its own transaction.
    A failed send is recorded with the error class name only, and the row is retried after a backoff."""
    if not sender.enabled:
        return 0, 0
    now_text = _iso(now)
    due = await db.run(lambda c: c.execute(
        "SELECT id, to_addr, subject, body, attempts FROM outbox "
        "WHERE sent_at IS NULL AND next_attempt_at <= ? AND attempts < ? "
        "ORDER BY created_at, id LIMIT ?",
        (now_text, max_attempts, BATCH_SIZE),
    ).fetchall())
    sent = failed = 0
    for row_id, to_addr, subject, body, attempts in due:
        try:
            await sender.send(to_addr=to_addr, subject=subject, body=body)
        except Exception as exc:  # noqa: BLE001 - any send failure is retried; the decision that queued it is committed
            failed += 1
            next_attempt = _iso(now + _backoff(attempts))
            await db.run(partial(_record_failure, row_id=row_id, attempts=attempts + 1, next_attempt=next_attempt,
                                 error=type(exc).__name__))
            log_event(log, "mail_failed", outbox_id=row_id, attempt=attempts + 1)
        else:
            sent += 1
            await db.run(partial(_record_sent, row_id=row_id, sent_at=now_text))
    return sent, failed


def _record_sent(conn: sqlite3.Connection, *, row_id: int, sent_at: str) -> None:
    conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (sent_at, row_id))


def _record_failure(conn: sqlite3.Connection, *, row_id: int, attempts: int, next_attempt: str, error: str) -> None:
    conn.execute("UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
                 (attempts, next_attempt, error, row_id))


class MailDeps(Protocol):
    db: Database
    sender: Sender
    clock: Callable[[], datetime]


async def mail_loop(deps: MailDeps, interval: float = 30) -> None:
    """Delivers due mail forever. A pass that fails is logged by its class name and retried on the next pass."""
    while True:
        try:
            await deliver_due(deps.db, deps.sender, deps.clock())
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any failure; the next pass retries
            log_event(log, "mail_loop_failed", level=logging.ERROR, code=type(exc).__name__)
        await asyncio.sleep(interval)
