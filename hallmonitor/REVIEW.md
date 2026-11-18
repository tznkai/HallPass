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
- `src/hallmonitor/adapters/registry.py:34` `def build_adapter(name: str, ctx: AdapterContext)` — the single entry point; a plugin that returns None leaves its service without an adapter, so every subject of that service is reported as `no_adapter`

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
- `src/hallmonitor/adapters/source_rcon.py:134` `return Observed("suspended") if await self._owned(steam_id) else Observed("banned_external")` — the re-check before a Source ban: a moderator ban that appeared after observe is never taken over, because apply reads the list before it runs `banid`
- `src/hallmonitor/adapters/source_rcon.py:147` `if not await self._owned(steam_id):` — the removeid gate: a restore runs no `removeid` unless the ban is recorded as ours
- `src/hallmonitor/adapters/palworld.py:135` `if self._in_flight(record, now):` — a Palworld ban placed within the grace window that the ban list does not show yet reads as `suspended`, so its record is kept and no second ban is sent

The Palworld forget step relies on the ban-list file reflecting a ban within the grace window; the live check
confirms that the file shows a new ban inside that window, before any record is forgotten. The live check showed the
line on the first read after the REST ban (see `docs/service-notes.md` `## Palworld`). The window has two accepted
costs: inside it a moderator's ban on the same id counts as ours and is lifted on restore, and a moderator's unban is
reported as suspended and not re-enforced until the window ends. The `source_rcon` adapter has no window: its console
list is re-read after `writeid`, so an id the list does not show is forgotten at once. The `source_panel` adapter has
the Palworld-style grace window, with the same accepted costs: inside it a moderator's ban on the same id counts as
ours and is lifted on restore, and a moderator's unban is reported as suspended until the window ends. A third cost
is accepted for `source_panel`: if `writeid` keeps failing for the whole window after an accepted `banid`, the
record is forgotten while the console may still hold the ban in memory, so a later restore sends no `removeid`
and the ban lasts until the server restarts (the file never had it, so the restart clears it).

The Valheim file has no compare-and-swap in Pterodactyl, so a write replaces the whole file with the text read just before it. A moderator's edit saved between that read and that write would be overwritten. The window holds one read and one write with only a local database call between them, and the race is accepted on that basis.

## Valheim permitted list

- `src/hallmonitor/adapters/valheim.py:263` `if not _has_entry(remaining):` — the empty-list guard: a suspension that would leave no entry in the permitted list writes nothing and reports `would_open_server`, because an empty permitted list lets anyone join
- `src/hallmonitor/adapters/valheim.py:166` `await self._files.write(self._permitted, text)` — the only write in the adapter, and it targets the permitted list, so the ban list is read but never written
- `src/hallmonitor/adapters/valheim.py:244` `await self._set_record(steam_id, ref, now)` — the record is placed before the write, and a write that fails puts back the subject's earlier pending record, or forgets the new one when there was none, so a failed write never erases a change that is still pending; if putting the record back fails too, that is logged (`record_restore_failed`) and the write's own reason is still raised, so a service-wide failure still stops the pass
- `src/hallmonitor/adapters/valheim.py:269` `if steam_id in await self._banned_ids():` — the ban check before any add: an id the ban list names is `banned_external` and is never added to the permitted list

## Restart detection

- `src/hallmonitor/adapters/valheim.py:138` `if uptime is None:` — a stopped server counts as not yet restarted, so a change is reported `pending_restart` until the server runs again
- `src/hallmonitor/adapters/valheim.py:143` `return uptime >= (now - placed_at).total_seconds() - 1` — the restart test: the server has run continuously since the change when its uptime reaches the time since the change, less a one-second margin for the second-precision placed_at, so a change is never called restarted by mistake
- `src/hallmonitor/adapters/valheim.py:181` `uptime = await self._files.uptime_seconds()` — the uptime is read at most once per observe or apply call, and not at all with live reload, so every change in one call is compared with the same reading; a change that was just written reports `pending_restart` without a read, so an uptime failure cannot turn a done change into an error
- `src/hallmonitor/adapters/valheim.py:203` `if ref == (OWN_ADD if listed else OWN_REMOVE) and pending(placed_at):` — the detail rule: `pending_restart` shows only while the record matches the file and its change has not reached a restarted server; otherwise the record is forgotten

## Pterodactyl client

- `src/hallmonitor/pterodactyl.py:39` `if status in (401, 403):` — a wrong or under-permissioned key is `auth`, which fails every subject of the service
- `src/hallmonitor/pterodactyl.py:41` `if status == 404:` — a missing ban-list file is `not_found`, an error and never an empty ban list
- `src/hallmonitor/pterodactyl.py:45` `if not 200 <= status < 300:` — any other non-2xx status is `http`, so an error body is never read as a file's text
- `src/hallmonitor/pterodactyl.py:78` `raise PteroError("unreachable") from None` — a transport failure is `unreachable`, the service-wide reason that stops the service's remaining applies
- `src/hallmonitor/pterodactyl.py:43` `if offline_is_unreachable and status in COMMAND_UNREACHABLE_STATUSES:` — a console command against an offline server (409 or 502) is `unreachable`, so the service's remaining applies stop, and no other route maps those statuses this way

## Source panel (The Ship)

- `src/hallmonitor/adapters/source_panel.py:48` `BAN_LINE = re.compile(` — the only form of a ban line: `banid 0` and a Source ID, once stripped, so a timed, commented or junk line is never a ban
- `src/hallmonitor/adapters/source_panel.py:100` `failure = identity_error(subject, self._universe)` — the identity gate in observe: a malformed steam_id is `bad_identity` before any file line is compared
- `src/hallmonitor/adapters/source_panel.py:160` `failure = identity_error(subject, self._universe)` — the identity gate in apply: nothing is recorded and no console command is built from a malformed id
- `src/hallmonitor/adapters/source_panel.py:109` `return Observed("suspended") if record is not None else Observed("banned_external")` — a listed Source ban without an own record is `banned_external`, so the reconcile loop never applies to it
- `src/hallmonitor/adapters/source_panel.py:124` `if sid in await self._listed():` — the re-read before a Source ban: a moderator's ban that appeared after observe is never taken over
- `src/hallmonitor/adapters/source_panel.py:128` `await self._db.run(lambda c: own.record_own(c, self._slug, steam_id, OWN_BAN, now))` — the record is placed before the first console command, so a ban the adapter sends is always recorded as ours
- `src/hallmonitor/adapters/source_panel.py:144` `if not await self._owned(steam_id):` — the never-lift rule: a restore sends no `removeid` and no `writeid` unless hallmonitor recorded the ban
- `src/hallmonitor/adapters/source_panel.py:112` `if own.within_grace(record[1], now, self._grace):` — the file-lag window: an own record the file does not show yet is `suspended` inside the grace window, and forgotten after it
- `src/hallmonitor/adapters/source_panel.py:132` `await self._forget(steam_id)   # banid was refused, so no ban was placed and no record is kept for it` — the forget is only for a refused `banid`: once `banid` is accepted the record is kept, so a ban the console holds keeps its record at least until the grace window ends (see the accepted costs above)
