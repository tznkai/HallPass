<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Hallpass

Last verified: 2026-11-13

## Purpose

Hallpass is the registry and authorization web app (FastAPI) in front of Authentik. It records who may use
which neighbourhood service, and it publishes that desired state for hallmonitor to apply. It holds no
service credentials. Hallmonitor is the sibling project in this monorepo and reads hallpass's state over
HTTP only.

## Checks

Run from this directory:

- Setup: `uv venv -p 3.12 .venv` then `uv pip install --python .venv/bin/python -e '.[dev]'`
- Tests: `.venv/bin/python -m pytest -q -W error` (warnings are errors)
- Lint: `.venv/bin/ruff check src tests tools`
- Licensing: `.venv/bin/reuse --root . lint`

CI runs exactly these three checks.

## Boundaries

- No imports from sibling projects. `tests/test_repo_hygiene.py` enforces this for `fluxer_panel`,
  `fluxer_client`, the source bot and `hallmonitor`.
- Hallpass holds no service credentials. Service admin keys live only in hallmonitor.
- Hallmonitor holds no names, contacts or parent links. Hallpass never sends them to it.
- Monitor API types are duplicated, never shared. Each project keeps its own copy of any type that crosses the
  boundary.

## Monitor API contract

`tests/contract/monitor_v1.json`, with its `tests/contract/monitor_v1.json.license` sidecar, is the recorded example
of the monitor API bodies. It is byte-identical in hallpass and hallmonitor. A change to it must land in both projects
in the same commit, together with the matching change to each project's wire types, so the two copies never drift.
`tests/test_monitor_api.py` reads it here, and hallmonitor's `tests/test_contract.py` reads its copy.

## Logging

Log only through `log_event`, with ids, event types and outcomes. Never log names, contacts, addresses, hosts,
URLs, tokens or message content.

## Secrets and publication

- Never read or print any `.env` file. `.env.example` is the only tracked env file.
- This directory is published by `git subtree split`, so tracked files hold no secrets, ids, hostnames, IPs or
  instance details. Test fixtures use fake values (`"1001"`, `"https://hallpass.invalid"`), and a SteamID64 is
  built at runtime from parts.
- The private denylist is `docs/private/hallpass/denylist.txt` in the main checkout. It is gitignored and absent
  in the published split, so the check skips there. Denylist findings name the path and line only.
- Private material (real network layout, deployment notes) lives in the main checkout's `docs/private/hallpass/`.
  Never copy it into this directory.

## REVIEW.md

`REVIEW.md` lists the security- and privacy-sensitive spots as `path:line` references with the anchor text
expected on that line. `tests/test_review_refs.py` checks every reference. A change that moves or edits cited
code updates `REVIEW.md` in the same change.

## Live work

Live scripts live in `tools/live/`. They are hand-run only, never run by CI. Results are recorded in
`docs/<name>-notes.md`, with placeholders in place of real hosts and ids.
