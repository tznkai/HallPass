# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Source panel adapter for The Ship: bans and unbans through the Pterodactyl console, and reads the ban list back
from `banned_user.cfg` through Pterodactyl. No RCON port is needed.

A suspension is `banid 0 <id>`, `kickid <id>` and `writeid`, sent through the Pterodactyl Client API's command route. A
lift is `removeid <id>` and `writeid`. The console holds its ban list in memory, and `writeid` is the only command that
writes the file, one `banid 0 <id>` line per ban. The server reads that file at startup only when server.cfg runs
`exec banned_user.cfg`. Hallmonitor never writes the file itself. It reads the file back to see what the server holds.

A ban is ours only when hallmonitor recorded it in `own_suspension`. A listed ban with no such record is
`banned_external`, and nothing lifts it. A record for an id the file does not show is kept for the grace window, since
the file may lag the console, and is forgotten after it.

Every Pterodactyl failure leaves as AdapterError with a reason the reconcile loop reports. Neither a SteamID64, a Source
ID, a command, a path nor the server id is ever logged or put in a message.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from hallmonitor import own
from hallmonitor.adapters.base import AdapterError, Capabilities, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.source_rcon import (
    SOURCE_ID,
    UNIVERSE,
    identity_error,
    known_source_ids,
    source_id_of,
    steam_id_of,
)
from hallmonitor.contract import Subject
from hallmonitor.db import Database
from hallmonitor.logs import log_event
from hallmonitor.pterodactyl import PteroError, PteroFiles

log = logging.getLogger(__name__)
ADAPTER = "source_panel"
OWN_BAN = "ban"
DEFAULT_UNIVERSE = "0"
REQUIRED_SETTINGS = ("ptero_url", "ptero_key", "ptero_server", "banlist_path")
# A ban line, once stripped, is exactly `banid 0 <Source ID>`, with runs of spaces or tabs between the parts.
BAN_LINE = re.compile(rf"banid[ \t]+0[ \t]+({SOURCE_ID.pattern})")
BYTE_ORDER_MARK = "\ufeff"


def listed_source_ids(text: str) -> set[str]:
    """The Source IDs named by the ban lines of a `banned_user.cfg` text. Every other line is not a ban: a timed ban,
    a line with trailing text, a comment, or junk. A byte-order mark at the start and CRLF endings are tolerated."""
    found: set[str] = set()
    for line in text.removeprefix(BYTE_ORDER_MARK).splitlines():
        match = BAN_LINE.fullmatch(line.strip())
        if match is not None:
            found.add(match.group(1))
    return found


def _reason(error: PteroError) -> str:
    """Auth, unreachable and http keep their names. A missing file or command target is `http` for this adapter."""
    return "http" if error.reason == "not_found" else error.reason


class SourcePanelAdapter:
    capabilities = Capabilities(suspend=True, restore=True, read_state=True, removes_connected=True)

    def __init__(self, *, slug: str, db: Database, clock: Callable[[], datetime], files: PteroFiles,
                 banlist_path: str, universe: str, grace_seconds: int) -> None:
        self._slug = slug
        self._db = db
        self._clock = clock
        self._files = files
        self._banlist = banlist_path
        self._universe = universe
        self._grace = grace_seconds

    async def _listed(self) -> set[str]:
        """The Source IDs the ban file names. A Pterodactyl failure leaves as AdapterError."""
        try:
            text = await self._files.read(self._banlist)
        except PteroError as error:
            raise AdapterError(_reason(error)) from None
        return listed_source_ids(text)

    async def _command(self, text: str) -> None:
        try:
            await self._files.command(text)
        except PteroError as error:
            raise AdapterError(_reason(error)) from None

    async def _record(self, steam_id: str) -> tuple[str, datetime] | None:
        return await self._db.run(lambda c: own.own_record(c, self._slug, steam_id))

    async def _owned(self, steam_id: str) -> bool:
        return await self._record(steam_id) is not None

    async def _forget(self, steam_id: str) -> None:
        await self._db.run(lambda c: own.forget_own(c, self._slug, steam_id))

    async def _observe_one(self, subject: Subject, listed: set[str], now: datetime) -> Observed:
        failure = identity_error(subject, self._universe)
        if failure is not None:
            return failure
        steam_id = steam_id_of(subject)
        assert steam_id is not None
        sid = source_id_of(steam_id, self._universe)
        assert sid is not None
        record = await self._record(steam_id)
        if sid in listed:
            return Observed("suspended") if record is not None else Observed("banned_external")
        if record is None:
            return Observed("allowed")
        if own.within_grace(record[1], now, self._grace):
            return Observed("suspended")   # our ban may simply not be in the file yet
        # Past the grace window and still not listed: the record no longer describes a ban.
        await self._forget(steam_id)
        return Observed("allowed")

    async def observe(self, subjects: Sequence[Subject]) -> Mapping[str, Observed]:
        listed = await self._listed()
        now = self._clock()
        return {subject.ref: await self._observe_one(subject, listed, now) for subject in subjects}

    async def _suspend(self, steam_id: str, sid: str) -> Observed:
        if sid in await self._listed():
            # Re-read here, because a moderator's ban may have appeared since observe. Ours is already in force.
            return Observed("suspended") if await self._owned(steam_id) else Observed("banned_external")
        now = self._clock()
        await self._db.run(lambda c: own.record_own(c, self._slug, steam_id, OWN_BAN, now))
        try:
            await self._command(f"banid 0 {sid}")
            await self._command(f"kickid {sid}")
            await self._command("writeid")
        except AdapterError:
            await self._forget(steam_id)   # the ban may not have been placed, so no record is kept for it
            raise
        if sid not in await self._listed():
            # The file may lag the console. The record is inside the grace window, so the ban is about to be in force.
            log_event(log, "ban_not_yet_listed", adapter=ADAPTER, service=self._slug)
        return Observed("suspended")

    async def _restore(self, steam_id: str, sid: str) -> Observed:
        if not await self._owned(steam_id):
            return Observed("banned_external")
        await self._command(f"removeid {sid}")
        await self._command("writeid")
        if sid in await self._listed():
            log_event(log, "not_applied", adapter=ADAPTER, service=self._slug, outcome="listing_disagrees")
            return Observed("error", "not_applied")
        await self._forget(steam_id)
        return Observed("allowed")

    async def apply(self, subject: Subject, desired: str) -> Observed:
        if desired not in ("allowed", "suspended"):
            raise ValueError("desired must be allowed or suspended")
        failure = identity_error(subject, self._universe)
        if failure is not None:
            return failure
        steam_id = steam_id_of(subject)
        assert steam_id is not None
        sid = source_id_of(steam_id, self._universe)
        assert sid is not None
        if desired == "suspended":
            return await self._suspend(steam_id, sid)
        return await self._restore(steam_id, sid)

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
        known = known_source_ids(subjects, self._universe)
        return len(await self._listed() - known)


def create(ctx: AdapterContext) -> SourcePanelAdapter | None:
    settings = ctx.settings
    grace = own.parse_grace_seconds(settings.get("forget_grace_seconds", str(own.DEFAULT_GRACE_SECONDS)))
    universe = settings.get("steamid_universe", DEFAULT_UNIVERSE)
    valid_universe = UNIVERSE.fullmatch(universe) is not None
    if not all(settings.get(name) for name in REQUIRED_SETTINGS) or grace is None or not valid_universe:
        log_event(log, "adapter_not_configured", adapter=ADAPTER, service=ctx.slug)
        return None
    files = PteroFiles(settings["ptero_url"], settings["ptero_key"], settings["ptero_server"], http=ctx.http)
    return SourcePanelAdapter(slug=ctx.slug, db=ctx.db, clock=ctx.clock, files=files,
                              banlist_path=settings["banlist_path"], universe=universe, grace_seconds=grace)
