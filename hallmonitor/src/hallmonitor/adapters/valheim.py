# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Valheim adapter: holds a player by editing `bannedlist.txt` through Pterodactyl. Valheim has no console, so the file
is the only mechanism.

A line that is all digits is a SteamID64. Every other line is a comment or a blank, and is kept as it is. A suspension
appends the id; a restore removes the lines that carry that id. Only a line with our own `ban` record is ours, so a line
someone else placed is `banned_external` and is never removed.

The server reads the file at start, so unless `live_reload` is on, a change is `pending_restart` until the server's
uptime shows a restart since the change. The uptime is compared with the time the change was placed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from hallmonitor import own
from hallmonitor.adapters.base import AdapterError, Capabilities, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.steam import valid_steam64
from hallmonitor.contract import Subject
from hallmonitor.db import Database
from hallmonitor.logs import log_event
from hallmonitor.pterodactyl import PteroError, PteroFiles

log = logging.getLogger(__name__)
STEAM_KIND = "steam_id"
OWN_BAN = "ban"
OWN_RESTORE = "restore"
PENDING = "pending_restart"
REQUIRED_SETTINGS = ("ptero_url", "ptero_key", "ptero_server", "banlist_path")
LIVE_RELOAD_VALUES = ("true", "false")


def _steam_id(subject: Subject) -> str | None:
    return next((identity.value for identity in subject.identities if identity.kind == STEAM_KIND), None)


def _identity_error(subject: Subject) -> Observed | None:
    """Refuses a subject whose steam_id is missing or malformed. The value is never echoed."""
    steam_id = _steam_id(subject)
    if steam_id is None:
        return Observed("error", "no_identity")
    if not valid_steam64(steam_id):
        return Observed("error", "bad_identity")
    return None


def _id_lines(text: str) -> list[str]:
    """The stripped id lines of the file, in order. A line is an id when its stripped text is all ASCII digits."""
    stripped = [line.strip() for line in text.split("\n")]
    return [line for line in stripped if line and all("0" <= char <= "9" for char in line)]


def _with_id(text: str, steam_id: str) -> str:
    """The file with the id on its own line at the end. A file that ends with a newline keeps one; a file that does not
    keeps none, and an empty file gets one line with a newline."""
    if text == "":
        return f"{steam_id}\n"
    if text.endswith("\n"):
        return f"{text}{steam_id}\n"
    return f"{text}\n{steam_id}"


def _without_id(text: str, steam_id: str) -> str:
    """The file with every line that carries the id removed. Splitting on `\\n` and joining back keeps the trailing
    newline, or its absence, and every other line byte for byte."""
    return "\n".join(part for part in text.split("\n") if part.strip() != steam_id)


def _pending(uptime: float | None, placed_at: datetime, now: datetime) -> bool:
    """True while the server has not restarted since the change. A stopped server counts as not yet restarted. The
    server has run continuously since the change when its uptime is longer than the time since the change."""
    if uptime is None:
        return True
    return uptime > (now - placed_at).total_seconds()


class ValheimAdapter:
    capabilities = Capabilities(suspend=True, restore=True, read_state=True, removes_connected=False)

    def __init__(self, *, slug: str, db: Database, clock: Callable[[], datetime], files: PteroFiles,
                 banlist_path: str, live_reload: bool) -> None:
        self._slug = slug
        self._db = db
        self._clock = clock
        self._files = files
        self._banlist = banlist_path
        self._live_reload = live_reload

    async def _read(self) -> str:
        try:
            return await self._files.read(self._banlist)
        except PteroError as error:
            raise AdapterError(error.reason) from None

    async def _write(self, text: str) -> None:
        try:
            await self._files.write(self._banlist, text)
        except PteroError as error:
            raise AdapterError(error.reason) from None

    async def _pending_check(self, now: datetime) -> Callable[[datetime], bool]:
        """A test of whether a change placed at a time is still pending. The uptime is read once, here. With live reload
        on, no change is ever pending, so the uptime is not read at all."""
        if self._live_reload:
            return lambda placed_at: False
        try:
            uptime = await self._files.uptime_seconds()
        except PteroError as error:
            raise AdapterError(error.reason) from None
        return lambda placed_at: _pending(uptime, placed_at, now)

    async def _record(self, steam_id: str) -> tuple[str, datetime] | None:
        return await self._db.run(lambda c: own.own_record(c, self._slug, steam_id))

    async def _set_record(self, steam_id: str, ref: str, now: datetime) -> None:
        await self._db.run(lambda c: own.record_own(c, self._slug, steam_id, ref, now))

    async def _forget(self, steam_id: str) -> None:
        await self._db.run(lambda c: own.forget_own(c, self._slug, steam_id))

    async def _observe_one(self, subject: Subject, listed: set[str], pending: Callable[[datetime], bool]) -> Observed:
        failure = _identity_error(subject)
        if failure is not None:
            return failure
        steam_id = _steam_id(subject)
        assert steam_id is not None
        record = await self._record(steam_id)
        if steam_id in listed:
            if record is not None and record[0] == OWN_BAN:
                return Observed("suspended", PENDING if pending(record[1]) else None)
            return Observed("banned_external")   # no record, or a restore record: a line nobody here placed
        if record is None:
            return Observed("allowed")
        ref, placed_at = record
        if ref == OWN_BAN:
            await self._forget(steam_id)          # our line is gone from the file, so the record describes nothing
            return Observed("allowed")
        if pending(placed_at):
            return Observed("allowed", PENDING)
        await self._forget(steam_id)              # the restore has reached a restarted server
        return Observed("allowed")

    async def observe(self, subjects: Sequence[Subject]) -> Mapping[str, Observed]:
        listed = set(_id_lines(await self._read()))
        pending = await self._pending_check(self._clock())
        return {subject.ref: await self._observe_one(subject, listed, pending) for subject in subjects}

    async def _suspend(self, steam_id: str, text: str, listed: bool, record: tuple[str, datetime] | None,
                       now: datetime) -> Observed:
        if listed:
            if record is not None and record[0] == OWN_BAN:
                pending = await self._pending_check(now)
                return Observed("suspended", PENDING if pending(record[1]) else None)
            return Observed("banned_external")
        await self._write(_with_id(text, steam_id))
        await self._set_record(steam_id, OWN_BAN, now)
        pending = await self._pending_check(now)
        return Observed("suspended", PENDING if pending(now) else None)

    async def _restore(self, steam_id: str, text: str, listed: bool, record: tuple[str, datetime] | None,
                       now: datetime) -> Observed:
        if listed:
            if record is None or record[0] != OWN_BAN:
                return Observed("banned_external")
            await self._write(_without_id(text, steam_id))
            await self._set_record(steam_id, OWN_RESTORE, now)
            pending = await self._pending_check(now)
            return Observed("allowed", PENDING if pending(now) else None)
        if record is None:
            return Observed("allowed")            # nothing of ours is in the file or pending: nothing to change
        if record[0] == OWN_BAN:
            await self._set_record(steam_id, OWN_RESTORE, now)   # our line already gone; the restore starts now
            pending = await self._pending_check(now)
            return Observed("allowed", PENDING if pending(now) else None)
        pending = await self._pending_check(now)  # a restore already in progress
        return Observed("allowed", PENDING if pending(record[1]) else None)

    async def apply(self, subject: Subject, desired: str) -> Observed:
        if desired not in ("allowed", "suspended"):
            raise ValueError("desired must be allowed or suspended")
        failure = _identity_error(subject)
        if failure is not None:
            return failure
        steam_id = _steam_id(subject)
        assert steam_id is not None
        text = await self._read()
        listed = steam_id in set(_id_lines(text))
        record = await self._record(steam_id)
        now = self._clock()
        if desired == "suspended":
            return await self._suspend(steam_id, text, listed, record, now)
        return await self._restore(steam_id, text, listed, record, now)

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
        known = {_steam_id(subject) for subject in subjects if _identity_error(subject) is None}
        return sum(1 for line in _id_lines(await self._read()) if line not in known)


def create(ctx: AdapterContext) -> ValheimAdapter | None:
    settings = ctx.settings
    live_reload = settings.get("live_reload", "false")
    if not (all(settings.get(name) for name in REQUIRED_SETTINGS) and live_reload in LIVE_RELOAD_VALUES):
        log_event(log, "adapter_not_configured", adapter="valheim", service=ctx.slug)
        return None
    files = PteroFiles(settings["ptero_url"], settings["ptero_key"], settings["ptero_server"], http=ctx.http)
    return ValheimAdapter(slug=ctx.slug, db=ctx.db, clock=ctx.clock, files=files,
                          banlist_path=settings["banlist_path"], live_reload=live_reload == "true")
