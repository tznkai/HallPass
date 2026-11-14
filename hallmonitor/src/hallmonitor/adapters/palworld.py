# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Palworld adapter: bans and unbans through the server's REST API, and reads the ban-list file through Pterodactyl.

The file shows bans placed by anyone, so a subject is banned when its `steam_<id>` is listed. A ban is ours only when
hallmonitor recorded it in `own_suspension`. A listed ban without that record is `banned_external`, and the adapter
never sends an unban for it. An unban is sent only for a ban the adapter placed.

Every REST failure leaves as AdapterError with the reason the client gave, and every Pterodactyl failure does the same.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

import httpx

from hallmonitor import own
from hallmonitor.adapters.base import AdapterError, Capabilities, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.adapters.steam import valid_steam64
from hallmonitor.contract import Subject
from hallmonitor.db import Database
from hallmonitor.logs import log_event
from hallmonitor.pterodactyl import PteroError, PteroFiles

log = logging.getLogger(__name__)
ADMIN_USER = "admin"
REST_PREFIX = "/v1/api"
STEAM_KIND = "steam_id"
OWN_BAN = "ban"
BAN_MESSAGE = "Access paused. Ask a parent or the administrator."
REQUEST_TIMEOUT_SECONDS = 30.0
REQUIRED_SETTINGS = ("rest_url", "admin_password", "ptero_url", "ptero_key", "ptero_server", "banlist_path")


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


def _userid(steam_id: str) -> str:
    return f"steam_{steam_id}"


def _known_userids(subjects: Sequence[Subject]) -> set[str]:
    known: set[str] = set()
    for subject in subjects:
        steam_id = _steam_id(subject)
        if steam_id is not None and valid_steam64(steam_id):
            known.add(_userid(steam_id))
    return known


class PalworldAdapter:
    capabilities = Capabilities(suspend=True, restore=True, read_state=True, removes_connected=True)

    def __init__(self, *, slug: str, db: Database, clock: Callable[[], datetime], http: httpx.AsyncClient,
                 rest_url: str, admin_password: str, files: PteroFiles, banlist_path: str,
                 grace_seconds: int) -> None:
        self._slug = slug
        self._db = db
        self._clock = clock
        self._http = http
        self._rest = rest_url.rstrip("/")
        self._password = admin_password
        self._files = files
        self._banlist = banlist_path
        self._grace = grace_seconds

    async def _ban_entries(self) -> list[str]:
        """The non-blank lines of the ban-list file, stripped. A Pterodactyl failure leaves as AdapterError."""
        try:
            text = await self._files.read(self._banlist)
        except PteroError as error:
            raise AdapterError(error.reason) from None
        return [line.strip() for line in text.splitlines() if line.strip()]

    async def _owned(self, steam_id: str) -> bool:
        return await self._db.run(lambda c: own.own_ref(c, self._slug, steam_id)) is not None

    async def _post(self, action: str, body: dict[str, str]) -> None:
        """One REST call with HTTP Basic auth. 401 and 403 are `auth`; other non-2xx answers are `http`."""
        try:
            response = await self._http.post(f"{self._rest}{REST_PREFIX}/{action}", json=body,
                                             auth=httpx.BasicAuth(ADMIN_USER, self._password),
                                             timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            raise AdapterError("unreachable") from None
        if response.status_code in (401, 403):
            raise AdapterError("auth")
        if not 200 <= response.status_code < 300:
            raise AdapterError("http")

    async def _forget(self, steam_id: str) -> None:
        await self._db.run(lambda c: own.forget_own(c, self._slug, steam_id))

    async def _record(self, steam_id: str) -> tuple[str, datetime] | None:
        return await self._db.run(lambda c: own.own_record(c, self._slug, steam_id))

    def _in_flight(self, record: tuple[str, datetime] | None, now: datetime) -> bool:
        """True for our own ban placed within the grace window. The ban list may not show it yet: the server writes the
        file after the REST call, so a ban that is not listed inside the window is in flight, not gone. Observe reads it
        as suspended, so the record is kept and no second ban is sent. Past the window it is forgotten, as a ban that
        never appeared. Inside the window (a) a moderator's ban on the same id counts as ours and is lifted on restore,
        and (b) a moderator's unban is reported as suspended and not re-enforced until the window ends. Both are
        accepted costs of handling the ban-list lag, pending the live check."""
        return record is not None and record[0] == OWN_BAN and own.within_grace(record[1], now, self._grace)

    async def _observe_one(self, subject: Subject, banned: set[str], now: datetime) -> Observed:
        failure = _identity_error(subject)
        if failure is not None:
            return failure
        steam_id = _steam_id(subject)
        assert steam_id is not None
        if _userid(steam_id) not in banned:
            record = await self._record(steam_id)
            if record is None:
                return Observed("allowed")
            if self._in_flight(record, now):
                return Observed("suspended")
            await self._forget(steam_id)   # past the grace window and still not listed: the record no longer describes a ban
            return Observed("allowed")
        return Observed("suspended") if await self._owned(steam_id) else Observed("banned_external")

    async def observe(self, subjects: Sequence[Subject]) -> Mapping[str, Observed]:
        banned = set(await self._ban_entries())
        now = self._clock()
        return {subject.ref: await self._observe_one(subject, banned, now) for subject in subjects}

    async def _suspend(self, steam_id: str) -> Observed:
        userid = _userid(steam_id)
        if userid in set(await self._ban_entries()):
            # Re-checked here, because a moderator's ban may have appeared since observe. Ours is already in force.
            return Observed("suspended") if await self._owned(steam_id) else Observed("banned_external")
        if self._in_flight(await self._record(steam_id), self._clock()):
            return Observed("suspended")          # our ban is already in flight: never a second ban
        await self._post("ban", {"userid": userid, "message": BAN_MESSAGE})
        now = self._clock()
        await self._db.run(lambda c: own.record_own(c, self._slug, steam_id, OWN_BAN, now))
        return Observed("suspended")

    async def _restore(self, steam_id: str) -> Observed:
        if not await self._owned(steam_id):
            return Observed("banned_external")
        await self._post("unban", {"userid": _userid(steam_id)})
        await self._forget(steam_id)
        return Observed("allowed")

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
        return await self._restore(steam_id)

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
        known = _known_userids(subjects)
        return sum(1 for entry in await self._ban_entries() if entry not in known)


def create(ctx: AdapterContext) -> PalworldAdapter | None:
    settings = ctx.settings
    grace = own.parse_grace_seconds(settings.get("forget_grace_seconds", str(own.DEFAULT_GRACE_SECONDS)))
    if not all(settings.get(name) for name in REQUIRED_SETTINGS) or grace is None:
        log_event(log, "adapter_not_configured", adapter="palworld", service=ctx.slug)
        return None
    files = PteroFiles(settings["ptero_url"], settings["ptero_key"], settings["ptero_server"], http=ctx.http)
    return PalworldAdapter(slug=ctx.slug, db=ctx.db, clock=ctx.clock, http=ctx.http,
                           rest_url=settings["rest_url"], admin_password=settings["admin_password"],
                           files=files, banlist_path=settings["banlist_path"], grace_seconds=grace)
