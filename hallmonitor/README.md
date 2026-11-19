<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# HallMonitor

HallMonitor reconciles service access with the HallPass Console, the other component of HallPass. It reads the
Console's desired state and applies it to Fluxer and to the game servers, so that each service matches what the
Console says. HallPass requires Authentik, which the Console uses for sign-in.

HallMonitor's properties, by design:

- It has no inbound ports. It only makes outbound calls to the Console and to the services.
- It holds the service credentials, the admin keys for each service.
- It never receives names, contacts or parent links. HallPass Console sends it only the identifiers and the access
  decisions that a service needs.

## Setup

Run these from this directory:

```
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
```

The three checks, the same ones CI runs:

```
.venv/bin/python -m pytest -q -W error
.venv/bin/ruff check src tests tools
.venv/bin/reuse --root . lint
```

## Adding an adapter

An adapter connects one kind of service to the reconcile loop. Adding one takes one module in
`src/hallmonitor/adapters/` and one line in `src/hallmonitor/adapters/registry.py`. The loop itself does not change.
The same change also updates `REVIEW.md` (see the last rule below).

### The design is opinionated

HallMonitor prefers allowlists to ban lists. Where a game has an allowlist (a whitelist or permitted list), the adapter
controls it: the Console decides who is on it, and a hold takes the person off. HallMonitor places bans only on a game
that has no allowlist, and lifts only the bans it placed itself. Where it reads a game's ban list, a ban it did not
place is left alone and reported as `banned_external`. The existing adapters, the rules below and the tests all assume
this. A ban-first approach, such as banning on a game that has an allowlist, or lifting bans that HallMonitor did not
place, is not supported as a setting and needs changes to the adapter, its tests and `REVIEW.md`.

### The plugin

The module exposes `create(ctx)`. `ctx` is an `AdapterContext` (`adapters/context.py`) holding the service slug,
its settings, the config, the database, the shared HTTP client and the clock. `create` returns the adapter. When a
required setting is missing or not valid, it logs `adapter_not_configured` and returns None, and the service's
subjects then report `no_adapter`. It never raises: `create` runs outside any per-service guard, so an exception there
fails the whole cycle for every service. Register it by name:

```
"mygame": AdapterPlugin(mygame.create),
```

That name is what a service in the Console picks as its adapter. `adapters/manual.py` is the smallest complete example.

### Settings

Each service's settings come from `HM_SERVICE_<SLUG>__<KEY>` environment variables. The slug is the Console service
slug, upper-cased, with `-` written as `_`. The key reaches the adapter lower-cased: `HM_SERVICE_MY_GAME__PTERO_URL`
becomes `ctx.settings["ptero_url"]` for the service `my-game`. A key is lower-case letters and digits joined by single
underscores; a malformed name or slug stops startup with a configuration error. An empty value is no setting at all.
The value of a key whose name contains `password`, `key` or `token` is redacted from the logs, so give every secret
such a name (for example `rcon_password` or `ptero_key`). Document every key in `.env.example`.

### The protocol

`adapters/base.py` defines it. An adapter has a `capabilities` attribute and three async methods:

- `observe(subjects)` returns an `Observed` for each subject, keyed by `subject.ref`. A subject with no entry is
  reported as `error`. Read the service once and answer for every subject from that.
- `apply(subject, desired)` makes the service match `desired`, which is `allowed` or `suspended`, and returns what
  is true afterwards. The loop calls it only when the observation differs from what the Console wants.
- `unmanaged_count(subjects)` returns how many entries in the service's own list name no subject, or None when the
  service has no such list.

An `Observed` has a `state` and an optional short `detail` token, such as `pending_restart` or `bad_identity`. The
states are:

| State | Meaning | Loop applies? |
|---|---|---|
| `allowed` | The subject can use the service. | When `suspended` is wanted |
| `suspended` | The subject is kept out. | When `allowed` is wanted |
| `unsupported` | HallMonitor cannot act here (manual services). | Never |
| `banned_external` | Kept out by something HallMonitor did not place. | Never |
| `error` | The state is unknown, or a change failed. | Never |

When the service itself cannot be asked, raise `AdapterError(reason)`, with a short reason token. Where it is raised
matters:

- **From `observe`**, any `AdapterError` fails every subject of the service with that reason. A transport error
  becomes `unreachable`, and any other exception `adapter_failed`. To fail one subject only, return
  `Observed("error", reason)` for it and carry on, as `fluxer.py` does.
- **From `apply`**, the reasons `auth`, `timeout` and `unreachable` are service-wide: they stop that service's
  remaining applies, and each later subject that needed an apply gets that reason. Any other reason stays with its
  subject.

`Capabilities` records what the adapter can do: `suspend`, `restore`, `read_state`, and `removes_connected` for
whether a suspension disconnects a player who is online. It is a declaration: the loop does not read it, so keep it
true for what the adapter does.

### Rules every adapter follows

- **Prefer the allowlist.** Where a game has an allowlist (a whitelist or permitted list), control that list. A
  ban is the fallback, for games with no allowlist.
- **Ban ownership.** An adapter lifts only bans it placed. Record every ban you place with `own.py`
  (`record_own`, and `forget_own` once it is lifted). A ban with no record is `banned_external`, and nothing lifts it.
- **A subject with several identities.** A subject may hold more than one identity on a service, such as a Java and a
  Bedrock name in Minecraft. A ban on one does not shield the others from a hold: the subject is `banned_external`
  only when every identity is banned, and otherwise the hold or allowance reaches the unbanned identities, reported with
  the detail `partly_banned`.
- **Allowlist (permitted-list) mode.** An allowlist is the Console's to control, so the adapter adds and removes its
  subjects' entries with no ownership record (`minecraft.py`, `valheim.py`). The game's own ban list stays read-only:
  an id on it is `banned_external`, and the adapter never writes it. Entries that name no subject are left alone and
  counted by `unmanaged_count`. Own records may still time a change: when the game reads the file only at start,
  Valheim records each change and reports `pending_restart` until the server's uptime shows a restart since then.
- **Identity allowlists.** Before an identity reaches a command or a file line, check it against a strict pattern,
  such as `adapters/steam.py`'s `valid_steam64`. A subject with no identity of the right kind is
  `error`/`no_identity`; one with an unsafe value is `error`/`bad_identity`.
- **Never empty an allowlist.** If a change would leave the list with no entries and the game reads an empty list
  as open to everyone, write nothing and report `error`/`would_open_server`.
- **Logging.** Log only through `log_event`, with the slug, event types and outcomes. Never log identities, names,
  contacts, addresses, hosts, URLs, paths, tokens or message content.
- **Tests.** Test against a fake of the service, not a live one. Cover the five states, `create` returning None for
  each missing or invalid setting, every identity check, and the ban-ownership rule. A live check goes in
  `tools/live/`, run by hand, and its results go in `docs/service-notes.md`.
- **REVIEW.md.** `REVIEW.md` lists the security-sensitive lines as `path:line` references, and
  `tests/test_review_refs.py` checks every one. In the same change, re-point the registry references that the new
  line moves, and add entries for the new adapter's identity checks, its ban or ownership branch and any empty-list
  guard.

The existing adapters show the patterns: `minecraft.py` (RCON whitelist), `palworld.py` (REST bans with a ban file
read back), `valheim.py` (a permitted list edited through `pterodactyl.py`), `source_rcon.py` (RCON bans),
`source_panel.py` (console bans through Pterodactyl, with the ban file read back) and `fluxer.py` (an HTTP admin API
that places no bans and keeps one subject's failure to that subject inside `observe`).

## Publication

This project starts private inside the monorepo. It is later published by `git subtree split`, so the
tracked files must hold no secrets, ids, hostnames or addresses. `tests/test_repo_hygiene.py` enforces
that, with a private denylist that lives outside the repository.
