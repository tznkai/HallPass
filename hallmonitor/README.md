<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Hallmonitor

Hallmonitor reconciles service access with hallpass. It reads hallpass's desired state and applies it to
Fluxer and to the game servers, so that each service matches what hallpass says.

Hallmonitor's properties, by design:

- It has no inbound ports. It only makes outbound calls to hallpass and to the services.
- It holds the service credentials, the admin keys for each service.
- It never receives names, contacts or parent links. Hallpass sends it only the identifiers and the access
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
`src/hallmonitor/adapters/` and one line in `src/hallmonitor/adapters/registry.py`. Nothing else in the loop
changes.

### The plugin

The module exposes `create(ctx)`. `ctx` is an `AdapterContext` (`adapters/context.py`) holding the service slug,
its settings, the config, the database, the shared HTTP client and the clock. `create` returns the adapter, or None
when a required setting is missing or not valid. The service's subjects then report `no_adapter`, so log
`adapter_not_configured` and return None; never raise. Register it by name:

```
"mygame": AdapterPlugin(mygame.create),
```

That name is what a hallpass service picks as its adapter. `adapters/manual.py` is the smallest complete example.

### Settings

Each service's settings come from `HM_SERVICE_<SLUG>__<KEY>` environment variables. The slug is the hallpass service
slug, upper-cased, with `-` written as `_`. The key reaches the adapter lower-cased: `HM_SERVICE_MY_GAME__PTERO_URL`
becomes `ctx.settings["ptero_url"]` for the service `my-game`. An empty value is no setting at all. A key containing
`password`, `key` or `token` is treated as a secret and removed from the logs. Document every key in `.env.example`.

### The protocol

`adapters/base.py` defines it. An adapter has a `capabilities` attribute and three async methods:

- `observe(subjects)` returns an `Observed` for each subject, keyed by `subject.ref`. A subject with no entry is
  reported as `error`. Read the service once and answer for every subject from that.
- `apply(subject, desired)` makes the service match `desired`, which is `allowed` or `suspended`, and returns what
  is true afterwards. The loop calls it only when the observation differs from what hallpass wants.
- `unmanaged_count(subjects)` returns how many entries in the service's own list name no subject, or None when the
  service has no such list.

An `Observed` has a `state` and an optional short `detail` token, such as `pending_restart` or `bad_identity`. The
states are:

| State | Meaning | Loop applies? |
|---|---|---|
| `allowed` | The subject can use the service. | When `suspended` is wanted |
| `suspended` | The subject is kept out. | When `allowed` is wanted |
| `unsupported` | Hallmonitor cannot act here (manual services). | Never |
| `banned_external` | Kept out by something hallmonitor did not place. | Never |
| `error` | The state is unknown, or a change failed. | Never |

When the service itself cannot be asked, raise `AdapterError(reason)`. The reasons `auth`, `timeout` and
`unreachable` are service-wide, and they stop the rest of that service's pass. Any other reason stays with its
subject.

`Capabilities` records what the adapter can do: `suspend`, `restore`, `read_state`, and `removes_connected` for
whether a suspension disconnects a player who is online. It is a declaration: the loop does not read it, so keep it
true for what the adapter does.

### Rules every adapter follows

- **Prefer the allowlist.** Where a game has an allowlist (a whitelist or permitted list), control that list. A
  ban is the fallback, for games with no allowlist.
- **Ban ownership.** An adapter lifts only bans it placed. Record every ban you place with `own.py`
  (`record_own`, and `forget_own` once it is lifted). A ban with no record is `banned_external`, and nothing lifts it.
- **Identity allowlists.** Before an identity reaches a command or a file line, check it against a strict pattern,
  such as `adapters/steam.py`'s `valid_steam64`. A subject with no identity of the right kind is
  `error`/`no_identity`; one with an unsafe value is `error`/`bad_identity`.
- **Never empty an allowlist.** If a change would leave the list with no entries and the game reads an empty list
  as open to everyone, write nothing and report `error`/`would_open_server`.
- **Logging.** Log only through `log_event`, with the slug, event types and outcomes. Never log identities, names,
  hosts, paths or messages.
- **Tests.** Test against a fake of the service, not a live one. Cover the five states, `create` returning None for
  each missing or invalid setting, every identity check, and the ban-ownership rule. A live check goes in
  `tools/live/`, run by hand, and its results go in `docs/service-notes.md`.

The existing adapters show the patterns: `minecraft.py` (RCON whitelist), `palworld.py` (REST bans with a ban file
read back), `valheim.py` (a permitted list edited through `pterodactyl.py`) and `source_rcon.py` (RCON bans).

## Publication

This project starts private inside the monorepo. It is later published by `git subtree split`, so the
tracked files must hold no secrets, ids, hostnames or addresses. `tests/test_repo_hygiene.py` enforces
that, with a private denylist that lives outside the repository.
