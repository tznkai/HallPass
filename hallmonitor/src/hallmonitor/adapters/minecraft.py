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
WHITELIST_LINE = re.compile(r"whitelisted player(?:s|\(s\))?:\s*(.*)$")  # the plan's player(s), or plural or singular
ONLINE_LINE = re.compile(r"players online:\s*(.*)$")  # the plan's default `list` reply, unconfirmed until the live check
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
    bedrock: bool  # a Bedrock gamertag, whose listed name starts with the Floodgate prefix


def _entries(subject: Subject, prefix: str) -> list[_Entry]:
    """The names a subject has on this server. Other identity kinds are ignored."""
    entries: list[_Entry] = []
    for identity in subject.identities:
        if identity.kind == JAVA_KIND:
            safe = JAVA_NAME.fullmatch(identity.value) is not None
            entries.append(_Entry("whitelist", identity.value, identity.value, safe, False))
        elif identity.kind == BEDROCK_KIND:
            safe = BEDROCK_NAME.fullmatch(identity.value) is not None and prefix in FLOODGATE_PREFIXES
            entries.append(_Entry("fwhitelist", identity.value, prefix + identity.value, safe, True))
    return entries


def _parse_whitelist(text: str) -> list[str]:
    if NO_WHITELISTED in text:
        return []
    match = WHITELIST_LINE.search(text)
    if match is None:
        raise AdapterError("unexpected")
    return [name.strip() for name in match.group(1).split(",") if name.strip()]


def _online_names(text: str) -> set[str]:
    """The lower-cased names in the `list` reply, `There are N of a max of M players online: a, b`."""
    match = ONLINE_LINE.search(text)
    if match is None:
        raise AdapterError("unexpected")
    return {name.strip().lower() for name in match.group(1).split(",") if name.strip()}


# A `.` or `*` Floodgate prefix is one character outside the name class, so it marks a Bedrock name. A Java name must
# not match a Bedrock twin's entry, so a Java name counts only after a separator (see _before_name).
SEPARATOR_PREFIXES = frozenset({".", "*"})


def _before_name(prefix: str, bedrock: bool) -> str:
    """The lookbehind that must hold just before a listed name's first character for a ban entry to count. A name
    character before it never counts. With a `.` or `*` prefix, a Java name counts only when the character before it is
    not a name character and not the prefix, or when the prefix follows a name character (`operator.Tim`). A Bedrock
    name starts with the prefix, so it counts only after a line break, after the `ban(s):` header (with or without a
    space), or after a letter, digit or `_` then `.` or the prefix (`operator..Tim`, `operator.*Tim`)."""
    if prefix in SEPARATOR_PREFIXES and not bedrock:
        sep = re.escape(prefix)
        return rf"(?:(?<![A-Za-z0-9_{sep}])|(?<=[A-Za-z0-9_]{sep}))"
    if prefix in SEPARATOR_PREFIXES:
        sep = re.escape(prefix)
        return rf"(?:(?<=\n)|(?<=ban\(s\):)|(?<=ban\(s\): )|(?<=[A-Za-z0-9_][.{sep}]))"
    return r"(?<![A-Za-z0-9_])"


def _is_banned(entry: _Entry, prefix: str, ban_text: str) -> bool:
    """True when the ban list holds an entry for the name. A vanilla entry reads `NAME was banned by BANNER: REASON`.
    Paper 26.3 sends the whole reply on one line with the entries run together, so an entry is found anywhere in the
    reply. The name may not follow a name character, so `Xbob was banned by` is not a ban on Bob. The banner is one
    word, so a name that appears only as the banner or in the reason is not the banned player. The Java and Bedrock
    rules differ only for a `.` or `*` prefix, see _before_name."""
    pattern = _before_name(prefix, entry.bedrock) + re.escape(entry.listed) + r" was banned by \S+: "
    return re.search(pattern, ban_text, re.IGNORECASE | re.ASCII) is not None


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
        if any(_is_banned(entry, self._prefix, ban_text) for entry in entries):
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
            ban_text = await client.command("banlist players")
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
            online = _online_names(await client.command("list")) if desired == "suspended" else set()
        actual = _listed_state(entries, {name.lower() for name in listed})
        if actual != desired:
            log_event(log, "not_applied", service="minecraft", outcome="listing_disagrees", level=logging.WARNING)
            return Observed("error", "not_applied")
        if any(entry.listed.lower() in online for entry in entries):
            # The kick did not disconnect the player, so the hold is not in force: report it, never as suspended.
            log_event(log, "still_connected", service="minecraft", outcome="kick_ignored", level=logging.WARNING)
            return Observed("error", "still_connected")
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
