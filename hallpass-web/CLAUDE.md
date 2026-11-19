<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Hallpass

Last verified: 2026-11-18

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

## Process

`python -m hallpass` runs two uvicorn listeners in one process: the public web app (`HALLPASS_BIND`, trusts
forwarded headers from 127.0.0.1 only) and the monitor API (`HALLPASS_MONITOR_BIND`, default `127.0.0.1:8081`).
The monitor listener is never proxied: it refuses any request carrying a proxy header, then checks the bearer
`HALLPASS_MONITOR_TOKEN` (32+ characters). Config refuses a monitor bind that overlaps the public port.

Exit codes (`src/hallpass/__main__.py`, covered by `tests/test_main.py`): 0 clean stop on SIGINT/SIGTERM, 2 bad
environment, 3 database refused (quarantined or unreadable), 4 a listener failed to start (the other is stopped
too), 1 any other uncaught error. A second stop signal forces both listeners to exit.

Migrations are `src/hallpass/migrations/NNNN_*.sql`, applied by `PRAGMA user_version`. Versions must be contiguous
from 1 (startup raises otherwise); add a new file, never edit a shipped one. The set currently runs 0001 to 0011.

## Monitor API contract

Routes: `GET /api/monitor/v1/desired` and `POST /api/monitor/v1/reports` (204), in `src/hallpass/monitor_api.py`.
A desired value's generation goes up when the recomputed value changes, and when a pair is sent again after it was
dropped. A generation never goes down, so a report carrying an old generation is never confirmed.

`tests/contract/monitor_v1.json`, with its `tests/contract/monitor_v1.json.license` sidecar, is the recorded example
of the monitor API bodies. It is byte-identical in hallpass and hallmonitor. A change to it must land in both projects
in the same commit, together with the matching change to each project's wire types, so the two copies never drift.
`tests/test_monitor_api.py` reads it here, and hallmonitor's `tests/test_contract.py` reads its copy.

## Logging

Log only through `log_event`, with ids, event types and outcomes. Never log names, contacts, addresses, hosts,
URLs, tokens or message content.

## Sessions and sign-out

- `POST /auth/backchannel-logout` is public and takes no session and no CSRF token: the signed logout token is the only
  authority. Its body is capped at 16384 bytes, and every failure is a 400 that records nothing.
- Every request checks `revocation.session_ended`: a session ends when its sid was revoked, when its person was revoked
  after it started, or when it started more than `HALLPASS_SESSION_MAX_AGE_SECONDS` ago. Pages and forward auth both
  check. A session with no start time (made before schema 10) ends at its first request, once.
- Sign-out (`POST /logout`) records the sid locally, ends the session, and sends the browser to Authentik's
  end-session endpoint, so the Authentik session ends too. A sign-in that starts at `/login` ends any session the
  browser already holds.

## Children and terms

- A child never accepts the terms. A child is covered while a linked, unlocked parent has accepted the current terms,
  and a child is never sent to `/terms`.
- Only grants by linked, unlocked parents count toward a child's approval.
- An admin's type change moves the status in the same transaction: a person made a child waits for a parent's grant,
  and an awaiting child made an adult becomes active.
- Unlinking a parent turns that parent's holds on the child into administrator holds in the same transaction,
  so the child stays held and the former parent is no longer named or counted.

## Manual restores

- A service marked manually suspended keeps its mark as a pending restore when a parent's lift leaves it with no hold,
  and the administrator is emailed once. The mark stays until the administrator unmarks it or a new hold lands.

## Shared identities

- An identity that two or more people hold on one service is paused while any holder is suspended. An allowed holder
  is not sent it, and a suspended holder is sent it so hallmonitor removes it there. The SSO subject is never shared.
- A person holds at most one Steam id and one Fluxer id per service. The registry refuses a second, and stored
  duplicates are flagged on the admin pages. Java names and Bedrock gamertags may repeat.

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

Live scripts live in `tools/live/` (`authentik_check.py`, `authentik_sessions_check.py`). They are hand-run only,
never run by CI. Results are recorded in `docs/authentik-notes.md`, with placeholders in place of real hosts and
ids. A behaviour the code relies on that no live run has confirmed yet is written down there as pending.
