<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Service notes

Live-check results for each service hallmonitor touches. Hosts, ids and keys are placeholders such as
`<fluxer-api>` and `<id>`. Source references are to the public Fluxer repository's main branch as read on
2026-11-13; the instance under test may run a slightly different build.

## Fluxer Admin API

Run 2026-11-13 with `tools/live/fluxer_check.py` against a dedicated test account.

- **Base and auth.** `<fluxer-api>/v1/admin/...`, where `<fluxer-api>` is `endpoints.api_public` from
  `<instance>/.well-known/fluxer` without the trailing `/v1`. Headers: `Authorization: Admin <key>` and
  `X-Audit-Log-Reason: <text>` (the audit header is required on enforcement routes).
- **Lookup.** `GET /v1/admin/users/{id}` → 200 with a wrapped body, `{"users": [<user>]}`.
  - `flags` is a **string** holding a 64-bit integer. Parse it with `int()`, never as a float.
  - DISABLED is `1 << 38`. `1 << 39` is `HAS_SESSION_STARTED` and is normal on an account that has signed in.
  - `temp_banned_until` and `enforcement_decision_id` are null on an account with no suspension.
  - The user object also carries `email`, `date_of_birth`, `last_active_ip`, `last_active_location` and more.
    Hallmonitor reads only `id` and `flags` from it and never logs or stores any other field.
- **Sessions.** `GET /v1/admin/users/{id}/sessions` → count. `DELETE /v1/admin/users/{id}/sessions` → 200, and the
  test account's open client was signed out at once. A recount straight after the delete still returned 1; whether
  that was a lagging list or a fresh session from the client is open.
- **Suspension needs an enforcement decision.** `PUT /v1/admin/users/{id}/ban` with
  `{"duration_hours": 0, "decision": {}}` → 400. In the source the body is
  `{duration_hours, decision: {reason_code, source, automation, statement?, ...}}`, where:
  - `reason_code` must be a rule-violation code (spam, harassment, ban evasion and so on). The neutral codes
    (user requested, other, inactivity) are excluded;
  - `source` is `report` or `own_review`, and `automation` is `none` or `flagged`;
  - the decision is recorded on the account and sends the user an enforcement notice by email and system DM.
- **No neutral disable.** The flags endpoint refuses to add DISABLED (`FLAG_REQUIRES_ENFORCEMENT_ROUTE`), so the
  ban route is the only way to set it.
- **Lift.** Not exercised. In the source a decision ends with
  `POST /v1/admin/enforcement-decisions/{id}/end` and `{"outcome": "lifted" | "reversed", "statement"?}`.
- **ACLs.** `GET /v1/admin/api-keys` → 403 for this key, so the key's own ACL list is not visible through it.
  With the design change below the key needs user lookup and session list/terminate only.

## Fluxer SSO

Pending: configure Fluxer SSO against Authentik and sign in with a plus-address presented as verified.

Known from the source:

- `sso_enforced` ("Require SSO") refuses every local sign-in (password, passkey, MFA ticket) for every account,
  admins included, while SSO is enabled. With Authentik down, nobody can sign in.
- With enforcement on, a verified provider email is synced onto the Fluxer account unless another account already
  holds that email.

## Design changes

- **Fluxer holds do not ban.** A ban always carries a rule-violation decision and notifies the user, which is wrong
  for a parental or admin hold. A Fluxer hold instead:
  1. removes the person from Authentik's Fluxer group (hallpass already syncs this in its Authentik sync);
  2. relies on Fluxer "Require SSO", so the person cannot sign in again while outside the group;
  3. ends the person's Fluxer sessions through `DELETE /v1/admin/users/{id}/sessions` (hallmonitor).

  Restoring needs no Fluxer call: hallpass puts the person back in the group and they sign in again. Hallmonitor
  reports `suspended` once the sessions are ended and `active` when no hold applies; it never sets or reads the
  DISABLED bit as its own state. The key drops `user:temp_ban`, `user:update_flags` and `enforcement:view`.

## Minecraft

## Palworld

## Valheim

## The Ship

## Abiotic Factor
