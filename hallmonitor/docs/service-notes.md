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
- **Sessions.**
  - `GET /v1/admin/users/{id}/sessions` → `{"sessions": [...]}`. The list also holds ended sessions: an entry with
    `deleted_at` set has been terminated, and only entries with `deleted_at` null are active.
  - `DELETE /v1/admin/users/{id}/sessions` → 200 `{"terminated_count": <n>}`. Observed: 2 active before, 2
    terminated, 0 active after, and the test client was signed out at once. It needs `user:update_flags`.
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
- **SSO identity lookup.**
  - `GET /v1/admin/users/{id}/sso-identities` → `{"sso_identities": [{provider_id, subject, user_id, claimed_at}]}`.
  - `GET /v1/admin/sso-identities?provider_id=<provider_id>&subject=<sub>` → `{"sso_identity": {...} | null}`, and
    returned the right Fluxer user. Both query parameters are required (subject alone → 400). It needs `user:lookup`.
- **ACLs.** `GET /v1/admin/api-keys` → 403 for this key, so the key's own ACL list is not visible through it.
  With the design change below the key needs `user:lookup`, `user:list_sessions` and `user:update_flags`
  (Fluxer files session termination under `update_flags`).

## Fluxer SSO

Run 2026-11-13: Fluxer SSO against an Authentik OIDC provider over HTTPS, with "Require SSO" off.

- **Instance identity mode:** email (`features.account_identity` in `<instance>/.well-known/fluxer`).
- **Issuer requirements.** Fluxer refuses an `http://` issuer, and refuses a host that resolves to a private
  address unless `FLUXER_SSO_ALLOW_PRIVATE_ADDRESSES=true` is set on the Fluxer server.
  The callback is `<fluxer web app>/auth/sso/callback`.
- **Provider settings.** Subject mode "Based on the User's UUID"; an `email` scope mapping returning the
  `fluxer_email` attribute with `email_verified: True`; the Authentik application bound to a `fluxer` group.
- **Plus-address.** A first SSO sign-in with a plus-address presented as verified created a new account, whose
  email is the plus-address and is verified. On an instance with approval-mode registration the new account
  waits for approval like any other sign-up. Tested with "Allowed Email Domains" empty.
- **Subject binding.** Fluxer binds the account to `sub`, and the stored `subject` equals Authentik's bare uuid.
  A later sign-in with a known `sub` goes straight to that account and ignores the email.
- **Linking an existing account.** A new `sub` whose verified email matches an existing account with a verified
  email signed straight into that account and bound it. Per the source it refuses instead
  (`SSO_ACCOUNT_LINK_REQUIRES_SIGN_IN`) when that account's email is unverified or the instance has email off, and
  refuses when the account is already bound to another `sub`.
- **Email sync.** Changing the provider email later did not change the account while "Require SSO" was off. Per the
  source, sync runs only with enforcement on, and never takes an email another account holds.
- **Group gate.** A user outside the `fluxer` group got Authentik's "Request has been denied" and never reached
  Fluxer.
- **Require SSO** (`sso_enforced`, from the source, not switched on here): refuses every local sign-in (password,
  passkey, MFA ticket) for every account, admins included. With Authentik down, nobody can sign in.

## Design changes

- **Fluxer holds do not ban.** A ban always carries a rule-violation decision and notifies the user, which is wrong
  for a parental or admin hold. A Fluxer hold instead:
  1. removes the person from Authentik's Fluxer group (hallpass already syncs this in its Authentik sync);
  2. relies on Fluxer "Require SSO", so the person cannot sign in again while outside the group;
  3. ends the person's Fluxer sessions through `DELETE /v1/admin/users/{id}/sessions` (hallmonitor).

  Restoring needs no Fluxer call: hallpass puts the person back in the group and they sign in again. Hallmonitor
  reports `suspended` once the sessions are ended and `active` when no hold applies; it never sets or reads the
  DISABLED bit as its own state. The key drops `user:temp_ban` and `enforcement:view`, and keeps
  `user:update_flags` only because session termination requires it.
- **Fluxer account lookup by SSO subject.** Hallmonitor finds a person's Fluxer account from their Authentik uuid
  with `GET /v1/admin/sso-identities?provider_id=&subject=`. The provider id is configuration (read once from any
  bound account's identity list); a null result means the person has not signed in to Fluxer yet.

## Minecraft

## Palworld

## Valheim

## The Ship

## Abiotic Factor
