# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Source RCON adapter for The Ship: bans and unbans through the server's console, in the `source` dialect.

A suspension is `banid 0 <id>`, `kickid <id>` and `writeid`. A lift is `removeid <id>` and `writeid`. The live check
on 2026-11-18 showed `banid 0 <id> kick` does not kick a player given a Steam id, so that form is never sent. The
console lists bans with `listid`. A ban is ours only when hallmonitor recorded it in `own_suspension`, so a listed ban
without that record is `banned_external` and is never removed.

The Source ID is built from the SteamID64 by `steam2`, and must match the Source ID pattern before any command is built.
Neither the SteamID64, the Source ID, the password nor a command is ever logged.

Every RCON failure leaves as AdapterError with the reason the client gave, so the reconcile loop reports that reason.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime

from hallmonitor import own
from hallmonitor.adapters.base import AdapterError, Capabilities, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.steam import steam2, valid_steam64
from hallmonitor.contract import Subject
from hallmonitor.db import Database
from hallmonitor.logs import log_event
from hallmonitor.rcon import RconClient, RconError

log = logging.getLogger(__name__)
STEAM_KIND = "steam_id"
OWN_BAN = "ban"
DEFAULT_PORT = 27015
DEFAULT_UNIVERSE = "0"
# The Source ID as the engine prints and accepts it. ASCII digits only, so a Unicode digit never matches.
SOURCE_ID = re.compile(r"STEAM_[0-5]:[01]:[0-9]+")
UNIVERSE = re.compile(r"[0-5]")
PORT_DIGITS = re.compile(r"[0-9]{1,5}")


def steam_id_of(subject: Subject) -> str | None:
    return next((identity.value for identity in subject.identities if identity.kind == STEAM_KIND), None)


def source_id_of(steam_id: str, universe: str) -> str | None:
    """The Source ID for a SteamID64, or None when it cannot be built into a valid ID."""
    try:
        sid = steam2(steam_id, universe)
    except ValueError:
        return None
    return sid if SOURCE_ID.fullmatch(sid) is not None else None


def identity_error(subject: Subject, universe: str) -> Observed | None:
    """Refuses a subject whose steam_id is missing or malformed. The value is never echoed."""
    steam_id = steam_id_of(subject)
    if steam_id is None:
        return Observed("error", "no_identity")
    if not valid_steam64(steam_id) or source_id_of(steam_id, universe) is None:
        return Observed("error", "bad_identity")
    return None


def known_source_ids(subjects: Sequence[Subject], universe: str) -> set[str]:
    known: set[str] = set()
    for subject in subjects:
        steam_id = steam_id_of(subject)
        if steam_id is not None and valid_steam64(steam_id):
            sid = source_id_of(steam_id, universe)
            if sid is not None:
                known.add(sid)
    return known


class SourceRconAdapter:
    capabilities = Capabilities(suspend=True, restore=True, read_state=True, removes_connected=True)

    def __init__(self, *, slug: str, db: Database, clock: Callable[[], datetime], host: str, port: int,
                 password: str, universe: str) -> None:
        self._slug = slug
        self._db = db
        self._clock = clock
        self._host = host
        self._port = port
        self._password = password
        self._universe = universe

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[RconClient]:
        """One authenticated `source` connection. A client failure leaves as AdapterError with the client's reason."""
        try:
            async with RconClient(self._host, self._port, self._password, dialect="source") as client:
                yield client
        except RconError as error:
            raise AdapterError(error.reason) from None

    @staticmethod
    async def _listed(client: RconClient) -> list[str]:
        """The Source IDs in the console's ban list, in the order listed."""
        return SOURCE_ID.findall(await client.command("listid"))

    async def _owned(self, steam_id: str) -> bool:
        return await self._db.run(lambda c: own.own_ref(c, self._slug, steam_id)) is not None

    async def _forget(self, steam_id: str) -> None:
        await self._db.run(lambda c: own.forget_own(c, self._slug, steam_id))

    async def _observe_one(self, subject: Subject, listed: set[str]) -> Observed:
        failure = identity_error(subject, self._universe)
        if failure is not None:
            return failure
        steam_id = steam_id_of(subject)
        assert steam_id is not None
        sid = source_id_of(steam_id, self._universe)
        assert sid is not None
        if sid not in listed:
            if await self._owned(steam_id):
                await self._forget(steam_id)   # the ban is gone from the list: the record no longer describes a ban
            return Observed("allowed")
        return Observed("suspended") if await self._owned(steam_id) else Observed("banned_external")

    async def observe(self, subjects: Sequence[Subject]) -> Mapping[str, Observed]:
        async with self._session() as client:
            listed = set(await self._listed(client))
        return {subject.ref: await self._observe_one(subject, listed) for subject in subjects}

    async def _suspend(self, steam_id: str, sid: str) -> Observed:
        async with self._session() as client:
            if sid in await self._listed(client):
                # Re-checked here, because a moderator's ban may have appeared since observe. Ours is already in force.
                return Observed("suspended") if await self._owned(steam_id) else Observed("banned_external")
            await client.command(f"banid 0 {sid}")
            await client.command(f"kickid {sid}")
            await client.command("writeid")
            if sid not in await self._listed(client):
                log_event(log, "not_applied", service="source_rcon", outcome="listing_disagrees",
                          level=logging.WARNING)
                return Observed("error", "not_applied")
        now = self._clock()
        await self._db.run(lambda c: own.record_own(c, self._slug, steam_id, OWN_BAN, now))
        return Observed("suspended")

    async def _restore(self, steam_id: str, sid: str) -> Observed:
        if not await self._owned(steam_id):
            # No ban of ours to lift. Read the console list again: a listed id is a ban we did not place, and an unlisted
            # one was lifted by an earlier restore of a subject that shares this id. Only `listid` is sent here.
            async with self._session() as client:
                listed = sid in await self._listed(client)
            return Observed("banned_external") if listed else Observed("allowed")
        async with self._session() as client:
            await client.command(f"removeid {sid}")
            await client.command("writeid")
            if sid in await self._listed(client):
                log_event(log, "not_applied", service="source_rcon", outcome="listing_disagrees",
                          level=logging.WARNING)
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
        async with self._session() as client:
            listed = await self._listed(client)
        return sum(1 for sid in listed if sid not in known)


def _port(raw: str) -> int | None:
    if not PORT_DIGITS.fullmatch(raw):
        return None
    value = int(raw)
    return value if 1 <= value <= 65535 else None


def create(ctx: AdapterContext) -> SourceRconAdapter | None:
    host = ctx.settings.get("host")
    password = ctx.settings.get("rcon_password")
    port = _port(ctx.settings.get("port", str(DEFAULT_PORT)))
    universe = ctx.settings.get("steamid_universe", DEFAULT_UNIVERSE)
    if not (host and password and port is not None and UNIVERSE.fullmatch(universe)):
        log_event(log, "adapter_not_configured", adapter="source_rcon", service=ctx.slug)
        return None
    return SourceRconAdapter(slug=ctx.slug, db=ctx.db, clock=ctx.clock, host=host, port=port, password=password,
                             universe=universe)
