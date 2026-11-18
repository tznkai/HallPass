<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Hallpass

Hallpass is a registry and authorization web app for neighbourhood services. It sits in front of
Authentik: residents sign in once, and hallpass decides which services each person may use. It
holds the desired state of who has access to what, and it does not hold any service credentials.

Hallmonitor, a separate project in this repository, reads that desired state and applies it to the
services. Hallpass never talks to a game server or to Fluxer's admin API.

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

## Caddy

Caddy sits in front of the web services and asks hallpass, through forward auth, whether each request may
pass. The hosts below are placeholders. They use `example.invalid`, not `example.com`, because the
repository hygiene test allows the bare `example.com` name but not its subdomains.

```caddy
hallpass.example.invalid {
    reverse_proxy 127.0.0.1:8080
}

calendar.example.invalid {
    forward_auth 127.0.0.1:8080 {
        uri /forward-auth?service=calendar
        copy_headers X-Hallpass-Person X-Hallpass-Type
    }
    reverse_proxy 127.0.0.1:9000 {
        header_up Cookie "hallpass_session=[^;]*;? *" ""
    }
}

feeds.example.invalid {
    log_skip /feed*
    forward_auth 127.0.0.1:8080 {
        uri /forward-auth?service=cal-feed
    }
    reverse_proxy 127.0.0.1:9001 {
        header_up Cookie "hallpass_session=[^;]*;? *" ""
    }
}
```

- `HALLPASS_COOKIE_DOMAIN` must be the shared parent domain of these hosts. The browser then sends the
  session cookie to every sibling host, and Caddy forwards its `Cookie` header to hallpass.
- Each service site strips the `hallpass_session` cookie from the request it sends upstream. The cookie
  is scoped to the parent domain, so without the strip `reverse_proxy` would hand every service a live,
  replayable hallpass session, admins included. The strip changes only that upstream request:
  `forward_auth` sends its own sub-request to hallpass with the `Cookie` header unchanged, so hallpass
  still sees the session.
- The slug in `uri` is fixed per site. Each protected site names one service slug, and that slug is the
  one the admin created on the services page. A slug is lower-case letters and digits, with single hyphens
  only between them, up to 64 characters and no underscores, because hallmonitor reads it from an
  environment variable name.
- Feed sites send no session. The feed token travels in the forwarded URI, so `log_skip` keeps the feed
  path out of Caddy's access log.
- The monitor listener binds `HALLPASS_MONITOR_BIND` (default `127.0.0.1:8081`) and requires
  `HALLPASS_MONITOR_TOKEN`, a secret of 32 characters or more. hallmonitor's `HALLPASS_MONITOR_TOKEN` must be
  the same value. The monitor listener must never appear in any Caddy site: only the hallpass web listener
  and the services themselves do. It answers 403 to any request that carries a forwarding header
  (`X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `Forwarded` or `Via`). Run hallmonitor on the
  same host as hallpass, and let it reach the monitor listener over loopback.

## Running

`python -m hallpass` starts both listeners, the public web app on `HALLPASS_BIND` and the monitor API on
`HALLPASS_MONITOR_BIND`, in one process. The first SIGINT or SIGTERM stops both cleanly, and a second one
forces them to exit. The exit codes are:

- 0: a clean stop on SIGINT or SIGTERM.
- 1: any other uncaught error.
- 2: a bad environment, such as a missing or invalid setting.
- 3: the database was refused (quarantined or unreadable).
- 4: a listener failed to start, and the other listener is stopped too.

Hallpass compares Minecraft Java names and Bedrock gamertags without case. If an existing database already has two
such identities on one service that differ only in case, the upgrade to schema version 6 stops with exit 1 and
leaves the database at version 5. Hallpass cannot start until then, so remove one identity of each pair from
the `external_identity` table with the `sqlite3` tool, then start again.

Before you retire a game service or remove an identity, lift its holds first, and clean up the game server
by hand if it needs it. hallmonitor stops managing that service and person pair and leaves its last applied
state in place.

## Publication

This project starts private inside the monorepo. It is later published by `git subtree split`, so the
tracked files must hold no secrets, ids, hostnames or addresses. `tests/test_repo_hygiene.py` enforces
that, with a private denylist that lives outside the repository.
