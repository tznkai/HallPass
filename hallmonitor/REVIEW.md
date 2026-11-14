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

- `src/hallmonitor/reconcile.py:73` `if observed.state in NEVER_APPLIED` — the branch that never applies: an observation of `unsupported`, `banned_external` or `error` is reported as it is, so a ban that hallmonitor did not place is never lifted
- `src/hallmonitor/reconcile.py:76` `result = await adapter.apply(subject, subject.desired)` — the only apply call in a cycle, reached only when the observation differs from the desired value
- `src/hallmonitor/adapters/fluxer.py:161` `if error.reason in SERVICE_WIDE_REASONS` — the per-subject containment in Fluxer's observe: an account's `http` or `unexpected` failure is that subject's error, and only `auth` or `unreachable` fail every Fluxer subject

## Fluxer never bans

- `src/hallmonitor/adapters/fluxer.py:147` `return Observed("banned_external")` — a disabled Fluxer account is reported and left alone whatever the desired value, because hallmonitor places no bans and lifts none
- `src/hallmonitor/adapters/fluxer.py:169` `await self._send("DELETE"` — the adapter's only mutating call: it ends the person's sessions, and no ban, enforcement or flags route is ever reached

## Adapter registry

- `src/hallmonitor/adapters/registry.py:23` `ADAPTERS: dict[str, AdapterPlugin] = {` — the one map from adapter name to plugin; an adapter name that is not in this map builds no adapter
- `src/hallmonitor/adapters/registry.py:30` `def build_adapter(name: str, ctx: AdapterContext)` — the single entry point; a plugin that returns None leaves its service without an adapter, so every subject of that service is reported as `no_adapter`

## RCON framing

- `src/hallmonitor/rcon.py:97` `if got_id == sentinel_id:` — the end of a response is the sentinel's reply, never the command's own packet, so a multi-packet answer is read in full before it is returned
- `src/hallmonitor/rcon.py:99` `if self._dialect == "minecraft" or data == SOURCE_TERMINATOR` — the dialect split: a Minecraft reply to the sentinel ends the read, while a Source echo of the empty sentinel is skipped until the terminator body arrives
- `src/hallmonitor/rcon.py:104` `if not sentinel_sent:` — the Minecraft sentinel goes out only after the first response packet for the command, so a vanilla server never receives the command and the sentinel in one read
- `src/hallmonitor/rcon.py:115` `if req_id == -1:` — the auth check: a type-2 reply with id -1 is a wrong password and raises `auth`; any other type-2 reply authenticates

## Minecraft never pardons

- `src/hallmonitor/adapters/minecraft.py:136` `if any(_is_banned(entry.listed, ban_text) for entry in entries):` — the ban branch of observe: a player on the ban list is checked before any whitelist state is read, so a ban placed by someone else is never lifted
- `src/hallmonitor/adapters/minecraft.py:137` `return Observed("banned_external")` — the observation that keeps the reconcile loop from applying anything to a banned player
- `src/hallmonitor/adapters/minecraft.py:170` `if actual != desired:` — the apply re-check: the whitelist is read again after the change, and any disagreement is `not_applied`, so a partial change is never reported as success
