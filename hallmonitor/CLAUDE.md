<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Hallmonitor

Last verified: 2026-11-18

## Purpose

Hallmonitor reconciles service access with hallpass. It reads hallpass's desired state and applies it to Fluxer
and to the game servers, so that each service matches the decisions hallpass holds. It has no inbound ports and
makes outbound calls only. It holds the service credentials, the admin keys for each service. Hallpass is the
sibling project in this monorepo, and hallmonitor never imports it.

## Checks

Run from this directory:

- Setup: `uv venv -p 3.12 .venv` then `uv pip install --python .venv/bin/python -e '.[dev]'`
- Tests: `.venv/bin/python -m pytest -q -W error` (warnings are errors)
- Lint: `.venv/bin/ruff check src tests tools`
- Licensing: `.venv/bin/reuse --root . lint`

CI runs exactly these three checks.

## Boundaries

- No imports from sibling projects. `tests/test_repo_hygiene.py` enforces this for `fluxer_panel`,
  `fluxer_client`, the source bot and `hallpass`.
- Hallmonitor holds no names, contacts or parent links. Hallpass sends it only identifiers and access decisions
  that a service needs.
- Hallpass holds no service credentials. Hallmonitor is where they live.
- Monitor API types are duplicated, never shared. Each project keeps its own copy of any type that crosses the
  boundary.

## Process

`python -m hallmonitor` runs the reconcile loop (`src/hallmonitor/reconcile.py`). Exit 2 for a bad environment, 3
for a refused database. Each cycle fetches desired state, observes each service, applies only where the observation
differs from the desired value, and posts reports back. Migrations in `src/hallmonitor/migrations/` follow hallpass's
rule: contiguous `NNNN_*.sql` versions, add a file and never edit a shipped one.

## Adapters

- **Opinionated: allowlist first.** Where a game has an allowlist, the adapter controls it; a ban list is the
  fallback for games without one. A ban-first approach is not a setting and needs adapter changes (README).
- `src/hallmonitor/adapters/registry.py` is the one adapter-name-to-plugin map (`manual`, `fluxer`, `minecraft`,
  `palworld`, `valheim`, `source_rcon`, `source_panel`). Adding an adapter is one module plus one line there. A plugin
  returns None when its service is unconfigured, and its subjects report `no_adapter`.
- The protocol is `adapters/base.py`: `observe`, `apply`, `unmanaged_count`, observed states `allowed`, `suspended`,
  `unsupported`, `banned_external`, `error`. The loop never applies to `unsupported`, `banned_external` or `error`.
- A failure with a service-wide reason (`auth`, `timeout`, `unreachable`) stops that service's remaining applies; any
  other failure stays with its subject.
- **Ban ownership.** Hallmonitor lifts only bans it placed. Every ban it places is recorded in `own.py`
  (`own_suspension`); a listed ban with no own record is `banned_external`, and restore never unbans or `removeid`s
  without that record. Valheim is the exception: it never writes the ban list, and its own records only time its
  restart rule. They do not decide ownership, because the permitted list is hallpass's to control, like the Minecraft
  whitelist. Fluxer places and lifts no bans at all: its only mutation is ending sessions, and a disabled account is
  `banned_external`.
- **Identity allowlists.** Every identity passes an allowlist before any command or file line is built from it:
  Minecraft Java/Bedrock name patterns and the Floodgate prefix set, and a canonical SteamID64 (`adapters/steam.py`)
  for Palworld, Valheim and Source. An unsafe value is `bad_identity`. Never widen these without a live check.
- Valheim edits the permitted list through `pterodactyl.py`, which has no compare-and-swap (the race is accepted in
  `REVIEW.md`). It never writes the ban list, and a suspension that would leave the list empty writes nothing. Unless
  `live_reload` is on, a change is `pending_restart` until the server's uptime covers it.

## Monitor API contract

`tests/contract/monitor_v1.json`, with its `tests/contract/monitor_v1.json.license` sidecar, is the recorded example
of the monitor API bodies. It is byte-identical in hallmonitor and hallpass. A change to it must land in both projects
in the same commit, together with the matching change to each project's wire types, so the two copies never drift.
`tests/test_contract.py` reads it here, and hallpass's `tests/test_monitor_api.py` reads its copy.

## Logging

Log only through `log_event`, with ids, event types and outcomes. Never log names, contacts, addresses, hosts,
URLs, tokens or message content.

## Secrets and publication

- Never read or print any `.env` file. `.env.example` is the only tracked env file.
- This directory is published by `git subtree split`, so tracked files hold no secrets, ids, hostnames, IPs or
  instance details. Test fixtures use fake values (`"1001"`, `"https://fluxer.invalid"`), and a SteamID64 is
  built at runtime from parts.
- The private denylist is `docs/private/hallmonitor/denylist.txt` in the main checkout. It is gitignored and absent
  in the published split, so the check skips there. Denylist findings name the path and line only.
- Private material lives in the main checkout's `docs/private/`, in two places. Hallmonitor's own material (its
  denylist, and its service-key environment notes) lives in `docs/private/hallmonitor/`. Shared deployment notes,
  including the Fluxer SSO setup, live in `docs/private/hallpass/`. Never copy either into this directory.

## REVIEW.md

`REVIEW.md` lists the security- and privacy-sensitive spots as `path:line` references with the anchor text
expected on that line. `tests/test_review_refs.py` checks every reference. A change that moves or edits cited
code updates `REVIEW.md` in the same change.

## Live work

Live scripts live in `tools/live/` (Fluxer, Minecraft, Palworld, Pterodactyl files, The Ship). They are hand-run only,
never run by CI. Results are recorded in `docs/service-notes.md`, with placeholders in place of real hosts and ids.
Fluxer, Palworld, Valheim, the Pterodactyl permissions and The Ship (`source_panel`, checked 2026-11-18) are recorded.
The Minecraft section records the RCON setup, the server versions and the Java and Bedrock holds working end to end
(2026-11-18), and lists its other items (framing, parsing, the kick check, name patterns) as pending. Abiotic Factor is
manual for now (adapter deferred). The Ship's 204 check for the subuser's console route was recorded on 2026-11-18.
Until a service's check is recorded, that adapter's framing and parsing are unconfirmed. Writing a file through
Pterodactyl needs `file.create`, not `file.update`.
