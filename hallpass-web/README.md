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

## Publication

This project starts private inside the monorepo. It is later published by `git subtree split`, so the
tracked files must hold no secrets, ids, hostnames or addresses. `tests/test_repo_hygiene.py` enforces
that, with a private denylist that lives outside the repository.
