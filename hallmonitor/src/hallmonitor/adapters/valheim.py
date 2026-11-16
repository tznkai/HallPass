# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Valheim adapter: the permitted list, edited through Pterodactyl. Valheim has no console, so the file is the only
mechanism, and a subject is allowed while an entry of `permittedlist.txt` names its SteamID64.

An entry names a SteamID64 in bare digits (`<digits>`) or with the Steam_ prefix (`Steam_<digits>`). Both forms are
read, and a change writes the Steam_ form, so the line keeps working if crossplay is turned on later. Comments (`//`)
and blank lines are never entries. Every other line is kept byte for byte. A leading byte-order mark is ignored when a
line is classified, and it stays where it is in the file.

Hallmonitor never writes the ban list. `bannedlist.txt` is read only, when it is configured, and an id it names is
`banned_external`, which is never added or removed. Removing the last entry would open the server to anyone, so a
suspension that would leave no entry writes nothing.

The server reads the file at start, so unless `live_reload` is on, a change is `pending_restart` until the server's
uptime shows a restart since the change. The uptime is compared with the time the change was placed. With `live_reload`
on, a removal disconnects a player who is online at once, so the adapter says so in its capabilities.

The file has no compare-and-swap in Pterodactyl, so a write replaces the whole file with the text read before it. A
moderator's edit saved between that read and that write would be overwritten. The window is one read and one write,
with only a local database call between them, and the race is accepted for that reason. The live check should watch
for it.
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
STEAM_PREFIX = "Steam_"
BOM = "\ufeff"         # the UTF-8 byte-order mark; str.strip does not remove it, so it is removed before a line is read
OWN_ADD = "add"        # an own record: hallmonitor added the id's line, so a restart must pass before it takes effect
OWN_REMOVE = "remove"  # an own record: hallmonitor removed the id's line, likewise
PENDING = "pending_restart"
REQUIRED_SETTINGS = ("ptero_url", "ptero_key", "ptero_server", "permittedlist_path")
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


def _body(line: str) -> str:
    """The line without a leading byte-order mark and without surrounding whitespace. Every line is classified through
    here, so a BOM at the start of the file never makes a comment an entry or hides an id."""
    return line.removeprefix(BOM).strip()


def _is_entry(line: str) -> bool:
    """A line is an entry when it is not blank and not a `//` comment."""
    body = _body(line)
    return body != "" and not body.startswith("//")


def _line_id(line: str) -> str | None:
    """The digits of the SteamID64 a line names, in either form, or None when it names none. The digits are not checked
    here: a line whose digits are not a valid id names no subject, and that is decided where the id is compared."""
    body = _body(line)
    digits = body[len(STEAM_PREFIX):] if body.startswith(STEAM_PREFIX) else body
    return digits if digits != "" and all("0" <= char <= "9" for char in digits) else None


def _ids_named(text: str) -> set[str]:
    """Every SteamID64 that some line of the file names, in either form."""
    return {line_id for line in text.split("\n") if (line_id := _line_id(line)) is not None}


def _has_entry(text: str) -> bool:
    return any(_is_entry(line) for line in text.split("\n"))


def _eol(text: str) -> str:
    """The line ending the file uses: CRLF when the text contains one, otherwise LF."""
    return "\r\n" if "\r\n" in text else "\n"


def _with_line(text: str, line: str) -> str:
    """The file with one line added at the end, ended the way the file ends its lines. A file that ends with a newline
    keeps one; a file that does not keeps none, and an empty file gets the line with a newline."""
    if text == "":
        return f"{line}\n"
    eol = _eol(text)
    if text.endswith("\n"):
        return f"{text}{line}{eol}"
    return f"{text}{eol}{line}"


def _without_id(text: str, steam_id: str) -> str:
    """The file with every line that names the id removed, in either form. A leading byte-order mark stays at the start
    of the file even when the line it sits on is the one removed. Splitting on `\\n` and joining back keeps the trailing
    newline, or its absence, and every other line byte for byte."""
    bom = BOM if text.startswith(BOM) else ""
    kept = [line for line in text[len(bom):].split("\n") if _line_id(line) != steam_id]
    return bom + "\n".join(kept)


def _same_file(first: str, second: str) -> bool:
    """True when two configured paths name one file: surrounding whitespace and leading slashes are not significant."""
    return first.strip().lstrip("/") == second.strip().lstrip("/")


def _pending(uptime: float | None, placed_at: datetime, now: datetime) -> bool:
    """True while the server has not restarted since the change. A stopped server counts as not yet restarted. The
    server has run continuously since the change when its uptime reaches the time since the change."""
    if uptime is None:
        return True
    # placed_at is stored to the second, so the time since the change can run up to one second over the true time. The
    # one-second margin keeps a change pending when the uptime is within a second of that time. The error is kept on the
    # pending side: calling a change restarted when it was not is the unsafe mistake.
    return uptime >= (now - placed_at).total_seconds() - 1


class ValheimAdapter:
    def __init__(self, *, slug: str, db: Database, clock: Callable[[], datetime], files: PteroFiles,
                 permittedlist_path: str, bannedlist_path: str | None, live_reload: bool) -> None:
        self._slug = slug
        self._db = db
        self._clock = clock
        self._files = files
        self._permitted = permittedlist_path
        self._banned = bannedlist_path
        self._live_reload = live_reload
        self.capabilities = Capabilities(suspend=True, restore=True, read_state=True, removes_connected=live_reload)

    async def _read(self, path: str) -> str:
        try:
            return await self._files.read(path)
        except PteroError as error:
            raise AdapterError(error.reason) from None

    async def _write(self, text: str) -> None:
        try:
            await self._files.write(self._permitted, text)
        except PteroError as error:
            raise AdapterError(error.reason) from None

    async def _banned_ids(self) -> set[str]:
        if self._banned is None:
            return set()
        return _ids_named(await self._read(self._banned))

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

    async def _change_detail(self, steam_id: str, listed: bool, pending: Callable[[datetime], bool]) -> str | None:
        """The detail for the subject's own change, if one is on record: `pending_restart` while the change has not
        reached a restarted server. A record that no longer matches the file, or whose change has reached a restarted
        server, is forgotten."""
        record = await self._record(steam_id)
        if record is None:
            return None
        ref, placed_at = record
        if ref == (OWN_ADD if listed else OWN_REMOVE) and pending(placed_at):
            return PENDING
        await self._forget(steam_id)
        return None

    async def _observe_one(self, subject: Subject, listed: set[str], banned: set[str],
                           pending: Callable[[datetime], bool]) -> Observed:
        failure = _identity_error(subject)
        if failure is not None:
            return failure
        steam_id = _steam_id(subject)
        assert steam_id is not None
        if steam_id in banned:
            return Observed("banned_external")
        is_listed = steam_id in listed
        detail = await self._change_detail(steam_id, is_listed, pending)
        return Observed("allowed" if is_listed else "suspended", detail)

    async def observe(self, subjects: Sequence[Subject]) -> Mapping[str, Observed]:
        listed = _ids_named(await self._read(self._permitted))
        banned = await self._banned_ids()
        pending = await self._pending_check(self._clock())
        return {subject.ref: await self._observe_one(subject, listed, banned, pending) for subject in subjects}

    async def _change(self, steam_id: str, ref: str, now: datetime, text: str) -> Observed:
        """Writes one change and reports its state. The record is placed before the write, so a write that fails
        leaves a record that no longer matches the file, and the next observe forgets it. With live reload nothing is
        recorded, and nothing is pending, so the uptime is not read."""
        state = "allowed" if ref == OWN_ADD else "suspended"
        if self._live_reload:
            await self._write(text)
            return Observed(state)
        await self._set_record(steam_id, ref, now)
        await self._write(text)
        return Observed(state, PENDING)

    async def _suspend(self, steam_id: str) -> Observed:
        text = await self._read(self._permitted)
        now = self._clock()
        if steam_id not in _ids_named(text):
            return Observed("suspended", await self._detail_now(steam_id, False, now))
        remaining = _without_id(text, steam_id)
        if not _has_entry(remaining):
            log_event(log, "not_applied", service="valheim", outcome="would_open_server", level=logging.WARNING)
            return Observed("error", "would_open_server")
        return await self._change(steam_id, OWN_REMOVE, now, remaining)

    async def _allow(self, steam_id: str) -> Observed:
        if steam_id in await self._banned_ids():
            return Observed("banned_external")
        text = await self._read(self._permitted)
        now = self._clock()
        if steam_id in _ids_named(text):
            return Observed("allowed", await self._detail_now(steam_id, True, now))
        return await self._change(steam_id, OWN_ADD, now, _with_line(text, STEAM_PREFIX + steam_id))

    async def _detail_now(self, steam_id: str, listed: bool, now: datetime) -> str | None:
        pending = await self._pending_check(now)
        return await self._change_detail(steam_id, listed, pending)

    async def apply(self, subject: Subject, desired: str) -> Observed:
        if desired not in ("allowed", "suspended"):
            raise ValueError("desired must be allowed or suspended")
        failure = _identity_error(subject)
        if failure is not None:
            return failure
        steam_id = _steam_id(subject)
        assert steam_id is not None
        if desired == "suspended":
            return await self._suspend(steam_id)
        return await self._allow(steam_id)

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
        known = {_steam_id(subject) for subject in subjects if _identity_error(subject) is None}
        return sum(1 for line in (await self._read(self._permitted)).split("\n")
                   if _is_entry(line) and _line_id(line) not in known)


def create(ctx: AdapterContext) -> ValheimAdapter | None:
    settings = ctx.settings
    live_reload = settings.get("live_reload", "false")
    banned = settings.get("bannedlist_path") or None
    usable = all(settings.get(name) for name in REQUIRED_SETTINGS) and live_reload in LIVE_RELOAD_VALUES
    # Both lists in one file would make the ban list the target of a write, so equal paths are not configured.
    same_file = banned is not None and _same_file(banned, settings.get("permittedlist_path", ""))
    if not usable or same_file:
        log_event(log, "adapter_not_configured", adapter="valheim", service=ctx.slug)
        return None
    files = PteroFiles(settings["ptero_url"], settings["ptero_key"], settings["ptero_server"], http=ctx.http)
    return ValheimAdapter(slug=ctx.slug, db=ctx.db, clock=ctx.clock, files=files,
                          permittedlist_path=settings["permittedlist_path"], bannedlist_path=banned,
                          live_reload=live_reload == "true")
