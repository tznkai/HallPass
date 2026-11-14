<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Hallmonitor

Last verified: 2026-11-13

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

Live scripts live in `tools/live/`. They are hand-run only, never run by CI. Results are recorded in
`docs/<name>-notes.md`, with placeholders in place of real hosts and ids.
