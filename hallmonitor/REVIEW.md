<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Review guide

This is the review guide for hallmonitor. Each `##` topic lists the spots a reviewer should read first, as
references to code. Each reference is written as ``- `path:line` `anchor` — reason``: the path and line
point at the code, the anchor is text that must appear on that line, and the reason says why the spot
matters. `tests/test_review_refs.py` checks every reference, so anchors must be updated in the same change
as the code they cite.

## Project layout

- `src/hallmonitor/__init__.py:3` `"""Hallmonitor:` — the package docstring names the project's role

## Reconcile decisions

- `src/hallmonitor/reconcile.py:80` `if observed.state in NEVER_APPLIED` — the branch that never applies: an observation of `unsupported`, `banned_external` or `error` is reported as it is, so a ban that hallmonitor did not place is never lifted
- `src/hallmonitor/reconcile.py:83` `result = await adapter.apply(subject, subject.desired)` — the only apply call in a cycle, reached only when the observation differs from the desired value
- `src/hallmonitor/reconcile.py:105` `if blocked is not None and _needs_apply(one, subject):` — after an apply fails with a service-wide reason (`auth`, `timeout` or `unreachable`), the service's remaining applies are reported with that reason and never called
- `src/hallmonitor/adapters/fluxer.py:160` `if error.reason in SERVICE_WIDE_REASONS` — the per-subject containment in Fluxer's observe: an account's `http` or `unexpected` failure is that subject's error, and only `auth` or `unreachable` fail every Fluxer subject

## Fluxer never bans

- `src/hallmonitor/adapters/fluxer.py:146` `return Observed("banned_external")` — a disabled Fluxer account is reported and left alone whatever the desired value, because hallmonitor places no bans and lifts none
- `src/hallmonitor/adapters/fluxer.py:168` `response = await self._send("DELETE"` — the adapter's only mutating call: it ends the person's sessions, and no ban, enforcement or flags route is ever reached

## Adapter registry

- `src/hallmonitor/adapters/registry.py:23` `ADAPTERS: dict[str, AdapterPlugin] = {` — the one map from adapter name to plugin; an adapter name that is not in this map builds no adapter
- `src/hallmonitor/adapters/registry.py:33` `def build_adapter(name: str, ctx: AdapterContext)` — the single entry point; a plugin that returns None leaves its service without an adapter, so every subject of that service is reported as `no_adapter`

## RCON framing

- `src/hallmonitor/rcon.py:97` `if got_id == sentinel_id:` — the end of a response is the sentinel's reply, never the command's own packet, so a multi-packet answer is read in full before it is returned
- `src/hallmonitor/rcon.py:99` `if self._dialect == "minecraft" or data == SOURCE_TERMINATOR` — the dialect split: a Minecraft reply to the sentinel ends the read, while a Source echo of the empty sentinel is skipped until the terminator body arrives
- `src/hallmonitor/rcon.py:104` `if not sentinel_sent:` — the Minecraft sentinel goes out only after the first response packet for the command, so a vanilla server never receives the command and the sentinel in one read
- `src/hallmonitor/rcon.py:115` `if req_id == -1:` — the auth check: a type-2 reply with id -1 is a wrong password and raises `auth`; any other type-2 reply authenticates

## Minecraft never pardons

- `src/hallmonitor/adapters/minecraft.py:136` `if any(_is_banned(entry.listed, ban_text) for entry in entries):` — the ban branch of observe: a player on the ban list is checked before any whitelist state is read, so a ban placed by someone else is never lifted
- `src/hallmonitor/adapters/minecraft.py:137` `return Observed("banned_external")` — the observation that keeps the reconcile loop from applying anything to a banned player
- `src/hallmonitor/adapters/minecraft.py:170` `if actual != desired:` — the apply re-check: the whitelist is read again after the change, and any disagreement is `not_applied`, so a partial change is never reported as success

## Console command arguments

- `src/hallmonitor/adapters/minecraft.py:45` `JAVA_NAME = re.compile(` — the Java name pattern: 1 to 16 letters, digits or underscores, so an entity selector such as `@a` never reaches `whitelist` or `kick`
- `src/hallmonitor/adapters/minecraft.py:46` `BEDROCK_NAME = re.compile(` — the Bedrock gamertag pattern, the same class for now; spaces stay refused until the live check settles how Floodgate passes them
- `src/hallmonitor/adapters/minecraft.py:48` `FLOODGATE_PREFIXES = frozenset(` — the prefix allowlist: one character from `.`, `*` or `_`, or empty, since the prefix is prepended to every Bedrock name
- `src/hallmonitor/adapters/minecraft.py:69` `safe = BEDROCK_NAME.fullmatch(identity.value)` — a Bedrock name is safe only when it matches the pattern and the configured prefix is allowlisted
- `src/hallmonitor/adapters/minecraft.py:158` `if not all(entry.safe for entry in entries):` — the gate in apply: an unsafe name is `bad_identity` before any add, remove or kick is sent
- `src/hallmonitor/adapters/minecraft.py:198` `prefix in FLOODGATE_PREFIXES):` — the adapter is not configured when the prefix is outside the allowlist, so no Bedrock command is ever built with a bad prefix

## Ban ownership

- `src/hallmonitor/adapters/palworld.py:95` `own.own_ref(c, self._slug, steam_id)` — the ownership lookup for a Palworld ban: a ban counts as ours only when its steam id has an own record
- `src/hallmonitor/adapters/palworld.py:139` `return Observed("suspended") if await self._owned(steam_id) else Observed("banned_external")` — a listed Palworld ban without an own record is `banned_external`, so the reconcile loop never applies to it
- `src/hallmonitor/adapters/palworld.py:159` `if not await self._owned(steam_id):` — the unban gate: a restore sends no unban unless hallmonitor recorded the ban, so a moderator's ban is never lifted
- `src/hallmonitor/adapters/source_rcon.py:133` `return Observed("suspended") if await self._owned(steam_id) else Observed("banned_external")` — the re-check before a Source ban: a moderator ban that appeared after observe is never taken over, because apply reads the list before it runs `banid`
- `src/hallmonitor/adapters/source_rcon.py:145` `if not await self._owned(steam_id):` — the removeid gate: a restore runs no `removeid` unless the ban is recorded as ours
- `src/hallmonitor/adapters/palworld.py:135` `if self._in_flight(record, now):` — a Palworld ban placed within the grace window that the ban list does not show yet reads as `suspended`, so its record is kept and no second ban is sent
- `src/hallmonitor/adapters/valheim.py:149` `if record is not None and record[0] == OWN_BAN:` — the Valheim ownership check: a listed id counts as ours only with a `ban` record, so a `restore` record never makes a line ours
- `src/hallmonitor/adapters/valheim.py:183` `if record is None or record[0] != OWN_BAN:` — the Valheim restore gate: a line with no `ban` record is never removed from the ban-list file

The Palworld forget step relies on the ban-list file reflecting a ban within the grace window; the live check confirms that the file shows a new ban inside that window, before any record is forgotten. The live check showed the line on the first read after the REST ban (see `docs/service-notes.md` `## Palworld`). The window has two accepted costs: inside it a moderator's ban on the same id counts as ours and is lifted on restore, and a moderator's unban is reported as suspended and not re-enforced until the window ends. The Source adapter has no window: its console list is re-read after `writeid`, so an id the list does not show is forgotten at once.

The Valheim file has no compare-and-swap in Pterodactyl, so a write replaces the whole file with the text read just before it. A moderator's edit saved between that read and that write would be overwritten. The window holds one read and one write with only a local database call between them, and the race is accepted on that basis.

## Restart detection

- `src/hallmonitor/adapters/valheim.py:89` `if uptime is None:` — a stopped server counts as not yet restarted, so a change is reported `pending_restart` until the server runs again
- `src/hallmonitor/adapters/valheim.py:94` `return uptime >= (now - placed_at).total_seconds() - 1` — the restart test: the server has run continuously since the change when its uptime reaches the time since the change, less a one-second margin for the second-precision placed_at, so a change is never called restarted by mistake
- `src/hallmonitor/adapters/valheim.py:127` `uptime = await self._files.uptime_seconds()` — the uptime is read once per observe or apply call, and not at all with live reload, so every change in one call is compared with the same reading
- `src/hallmonitor/adapters/valheim.py:150` `return Observed("suspended", PENDING if pending(record[1]) else None)` — the detail rule: `pending_restart` shows only while the change is pending, and a restart since the change clears it

## Pterodactyl client

- `src/hallmonitor/pterodactyl.py:34` `if status in (401, 403):` — a wrong or under-permissioned key is `auth`, which fails every subject of the service
- `src/hallmonitor/pterodactyl.py:36` `if status == 404:` — a missing ban-list file is `not_found`, an error and never an empty ban list
- `src/hallmonitor/pterodactyl.py:38` `if not 200 <= status < 300:` — any other non-2xx status is `http`, so an error body is never read as a file's text
- `src/hallmonitor/pterodactyl.py:70` `raise PteroError("unreachable") from None` — a transport failure is `unreachable`, the service-wide reason that stops the service's remaining applies
