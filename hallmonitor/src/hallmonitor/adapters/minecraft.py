# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Minecraft adapter: a whitelist managed over RCON. Hallmonitor never pardons, and never changes the ban list.

A Java player is managed with `whitelist add|remove <name>`. A Bedrock player is managed through Floodgate with
`fwhitelist add|remove <gamertag>`, and appears in the vanilla list under the Floodgate prefix. A suspension removes the
name from the whitelist and then kicks the player, because removal alone may not disconnect them. A ban is only read
(`banlist players`), so a ban that someone else placed is reported as `banned_external` and is never lifted.

Every RCON failure is raised by the client as RconError and leaves this module as AdapterError with the same reason,
so the reconcile loop reports that reason rather than a generic apply failure.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from hallmonitor.adapters.base import AdapterError, Capabilities, Observed
from hallmonitor.adapters.context import AdapterContext
from hallmonitor.contract import Subject
from hallmonitor.logs import log_event
from hallmonitor.rcon import RconClient, RconError

log = logging.getLogger(__name__)
JAVA_KIND = "java_name"
BEDROCK_KIND = "bedrock_gamertag"
DEFAULT_PORT = 25575
DEFAULT_FLOODGATE_PREFIX = "."
DEFAULT_KICK_MESSAGE = "Access paused. Ask a parent or the administrator."
NO_WHITELISTED = "There are no whitelisted players"
WHITELIST_LINE = re.compile(r"whitelisted players?:\s*(.*)$")
BAN_HEADER = re.compile(r"There are (?:no bans|\d+ ban\(s\):)")
PORT_DIGITS = re.compile(r"[0-9]{1,5}")

# Console command arguments. A target selector (@a, @e[...], @p, @r) would make `whitelist remove` and `kick` act on
# every matching player, and any other character could split or extend a command, so a name is refused unless it
# matches its kind's pattern in full. A Java name is 1 to 16 letters, digits or underscores. A Bedrock gamertag uses
# the same class for now: spaces stay refused until the live check settles how Floodgate passes them to the console.
JAVA_NAME = re.compile(r"[A-Za-z0-9_]{1,16}")
BEDROCK_NAME = re.compile(r"[A-Za-z0-9_]{1,16}")
# The Floodgate prefix is prepended to every Bedrock name, so it is one character from a short allowlist, or empty.
FLOODGATE_PREFIXES = frozenset({"", ".", "*", "_"})

ListState = Literal["allowed", "suspended", "partial"]


@dataclass(frozen=True)
class _Entry:
    command: str   # "whitelist" for a Java name, "fwhitelist" for a Bedrock name
    arg: str       # the name the add or remove command takes
    listed: str    # the name the vanilla list shows, which is also the name `kick` takes
    safe: bool     # the name matches its kind's pattern, and the prefix is allowlisted


def _entries(subject: Subject, prefix: str) -> list[_Entry]:
    """The names a subject has on this server. Other identity kinds are ignored."""
    entries: list[_Entry] = []
    for identity in subject.identities:
        if identity.kind == JAVA_KIND:
            safe = JAVA_NAME.fullmatch(identity.value) is not None
            entries.append(_Entry("whitelist", identity.value, identity.value, safe))
        elif identity.kind == BEDROCK_KIND:
            safe = BEDROCK_NAME.fullmatch(identity.value) is not None and prefix in FLOODGATE_PREFIXES
            entries.append(_Entry("fwhitelist", identity.value, prefix + identity.value, safe))
    return entries


def _parse_whitelist(text: str) -> list[str]:
    if NO_WHITELISTED in text:
        return []
    match = WHITELIST_LINE.search(text)
    if match is None:
        raise AdapterError("unexpected")
    return [name.strip() for name in match.group(1).split(",") if name.strip()]


def _ban_names(text: str) -> str:
    """The ban list without its header lines, so `There are 1 ban(s):` never matches a name."""
    return "\n".join(line for line in text.splitlines() if not BAN_HEADER.fullmatch(line.strip()))


def _is_banned(name: str, ban_text: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", ban_text, re.IGNORECASE) is not None


def _listed_state(entries: Sequence[_Entry], listed_lower: set[str]) -> ListState:
    flags = [entry.listed.lower() in listed_lower for entry in entries]
    if all(flags):
        return "allowed"
    if not any(flags):
        return "suspended"
    return "partial"


class MinecraftAdapter:
    capabilities = Capabilities(suspend=True, restore=True, read_state=True, removes_connected=True)

    def __init__(self, *, host: str, port: int, password: str, floodgate_prefix: str, kick_message: str) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._prefix = floodgate_prefix
        self._kick_message = kick_message

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[RconClient]:
        """One authenticated connection. A client failure leaves as AdapterError with the client's reason."""
        try:
            async with RconClient(self._host, self._port, self._password) as client:
                yield client
        except RconError as error:
            raise AdapterError(error.reason) from None

    def _observe_subject(self, subject: Subject, listed_lower: set[str], ban_text: str) -> Observed:
        entries = _entries(subject, self._prefix)
        if not entries:
            return Observed("error", "no_identity")
        if not all(entry.safe for entry in entries):
            return Observed("error", "bad_identity")
        if any(_is_banned(entry.listed, ban_text) for entry in entries):
            return Observed("banned_external")
        state = _listed_state(entries, listed_lower)
        if state == "partial":
            # Only some names are listed. Report the opposite of the desired state, so the reconcile loop applies the
            # desired state to every name, and the apply re-checks the whole set.
            return Observed("suspended" if subject.desired == "allowed" else "allowed", "partial")
        return Observed(state)

    async def observe(self, subjects: Sequence[Subject]) -> Mapping[str, Observed]:
        async with self._session() as client:
            listed = _parse_whitelist(await client.command("whitelist list"))
            ban_text = _ban_names(await client.command("banlist players"))
        listed_lower = {name.lower() for name in listed}
        return {subject.ref: self._observe_subject(subject, listed_lower, ban_text) for subject in subjects}

    async def apply(self, subject: Subject, desired: str) -> Observed:
        if desired not in ("allowed", "suspended"):
            raise ValueError("desired must be allowed or suspended")
        entries = _entries(subject, self._prefix)
        if not entries:
            return Observed("error", "no_identity")
        if not all(entry.safe for entry in entries):
            return Observed("error", "bad_identity")
        async with self._session() as client:
            for entry in entries:
                if desired == "allowed":
                    await client.command(f"{entry.command} add {entry.arg}")
                else:
                    await client.command(f"{entry.command} remove {entry.arg}")
                    await client.command(f"kick {entry.listed} {self._kick_message}")
            listed = _parse_whitelist(await client.command("whitelist list"))
        actual = _listed_state(entries, {name.lower() for name in listed})
        if actual != desired:
            log_event(log, "not_applied", service="minecraft", outcome="listing_disagrees", level=logging.WARNING)
            return Observed("error", "not_applied")
        return Observed(desired)

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
        known = {entry.listed.lower() for subject in subjects for entry in _entries(subject, self._prefix)}
        async with self._session() as client:
            listed = _parse_whitelist(await client.command("whitelist list"))
        return sum(1 for name in listed if name.lower() not in known)


def _port(raw: str) -> int | None:
    if not PORT_DIGITS.fullmatch(raw):
        return None
    value = int(raw)
    return value if 1 <= value <= 65535 else None


def create(ctx: AdapterContext) -> MinecraftAdapter | None:
    host = ctx.settings.get("host")
    password = ctx.settings.get("rcon_password")
    port = _port(ctx.settings.get("port", str(DEFAULT_PORT)))
    prefix = ctx.settings.get("floodgate_prefix", DEFAULT_FLOODGATE_PREFIX)
    if not (host and password and port is not None and prefix in FLOODGATE_PREFIXES):
        log_event(log, "adapter_not_configured", adapter="minecraft", service=ctx.slug)
        return None
    return MinecraftAdapter(
        host=host,
        port=port,
        password=password,
        floodgate_prefix=prefix,
        kick_message=ctx.settings.get("kick_message", DEFAULT_KICK_MESSAGE),
    )
