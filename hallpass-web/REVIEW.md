<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Review guide

This is the review guide for hallpass. Each `##` topic lists the spots a reviewer should read first, as
references to code. Each reference is written as ``- `path:line` `anchor` — reason``: the path and line
point at the code, the anchor is text that must appear on that line, and the reason says why the spot
matters. `tests/test_review_refs.py` checks every reference, so anchors must be updated in the same change
as the code they cite.

## Project layout

- `src/hallpass/__init__.py:3` `"""Hallpass:` — the package docstring names the project's role

## Enrollment binding

- `src/hallpass/auth.py:199` `async def resolve_sign_in` — the order of the binding decisions: a known sub, then an enrollment ref, then the admin bootstrap, then refusal
- `src/hallpass/registry.py:179` `enrollment_ref = NULL, invite_url = NULL` — binding clears the ref, so a second Authentik account that carries the same ref matches no person

## Sessions and CSRF

- `src/hallpass/auth.py:109` `def add_session_middleware` — the session cookie's Secure, SameSite=Lax and cookie domain settings
- `src/hallpass/auth.py:135` `async def require_csrf` — every POST checks the form token and the Origin header
- `src/hallpass/web/routes_auth.py:40` `def safe_next` — the only check on where a sign-in returns the person to, so it must refuse off-site URLs

## Admin check

- `src/hallpass/auth.py:232` `async def require_admin` — the one gate for every admin page and action; it tests the Authentik sub against the configured admins

## Fluxer address

- `src/hallpass/registry.py:336` `def fluxer_address` — the order is the override, then the person's own email, then a parent's plus-address; changing it changes which address Fluxer uses
