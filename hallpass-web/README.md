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
    reverse_proxy 127.0.0.1:9000
}

feeds.example.invalid {
    log_skip /feed*
    forward_auth 127.0.0.1:8080 {
        uri /forward-auth?service=cal-feed
    }
    reverse_proxy 127.0.0.1:9001
}
```

- `HALLPASS_COOKIE_DOMAIN` must be the shared parent domain of these hosts. The browser then sends the
  session cookie to every sibling host, and Caddy forwards its `Cookie` header to hallpass.
- The slug in `uri` is fixed per site. Each protected site names one service slug, and that slug is the
  one the admin created on the services page.
- Feed sites send no session. The feed token travels in the forwarded URI, so `log_skip` keeps the feed
  path out of Caddy's access log.
- The monitor API listener (added in a later phase) must never be proxied. Only the hallpass web listener
  and the services themselves appear in a Caddy site.

## Publication

This project starts private inside the monorepo. It is later published by `git subtree split`, so the
tracked files must hold no secrets, ids, hostnames or addresses. `tests/test_repo_hygiene.py` enforces
that, with a private denylist that lives outside the repository.
