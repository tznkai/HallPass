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

## Publication

This project starts private inside the monorepo. It is later published by `git subtree split`, so the
tracked files must hold no secrets, ids, hostnames or addresses. `tests/test_repo_hygiene.py` enforces
that, with a private denylist that lives outside the repository.
