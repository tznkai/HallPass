<!--
SPDX-FileCopyrightText: 2026 K. Chen
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Authentik live-check notes

Results of `tools/live/authentik_check.py` against a test Authentik instance, run 2026-11-13.
Hosts, ids and tokens are replaced by placeholders such as `<authentik>`, `<pk>` and `<uuid>`.

- **Authentik version:** 2026.8.5 (the design requires 2025.2.3 or later).
- **API token:** a non-expiring API-intent token sent as `Authorization: Bearer <token>`.
- **Enrollment flow `<flow-slug>`:** Invitation stage (no continuing without an invitation) → Prompt stage
  (username, password, password repeat) → User Write stage (always create) → User Login stage.
- **OIDC provider:** confidential client, subject mode "Based on the User's UUID", RS256.

## user_without_email

`POST /api/v3/core/users/` with `username` and `name` and no `email` → 201. The response carries an integer
`pk` and a `uuid`. Authentik accepts users without an email.

## invitation

`POST /api/v3/stages/invitation/invitations/` with `name`, `single_use: true`, `flow: <flow pk>` and
`fixed_data: {"attributes.hallpass_enrollment": "<random>"}` → 201. The invite URL has the form
`<authentik>/if/flow/<flow-slug>/?itoken=<invitation pk>`. The flow pk comes from
`GET /api/v3/flows/instances/?slug=<flow-slug>`.

## invitation_binding

After a person enrolled through the invite URL without giving an email:

- The dotted `fixed_data` key landed at `user.attributes.hallpass_enrollment`.
- `GET /api/v3/core/users/?attributes={"hallpass_enrollment": "<random>"}` (JSON in the query) → 200 and finds
  the user, so the attributes filter works.
- The invitation was gone afterwards (deleted once used): single use holds.

## uuid_lookup

`GET /api/v3/core/users/?uuid=<uuid>` → 200. The filter works and returns the user with its integer `pk`.

## deactivate_sessions

- `PATCH /api/v3/core/users/<pk>/` with `{"is_active": false}` → 200.
- **Deactivation ends the user's sessions by itself.** After the PATCH the enrolled user (who had signed in
  during enrolment) had no sessions left. Authentik's source confirms it: a `post_save` handler on `User`
  deletes all sessions of a user saved with `is_active` false.
- **`GET /api/v3/core/authenticated_sessions/?user=<pk>` ignores the `user` parameter.** It returns 200 with
  every user's sessions. The list endpoint filters only on `user__username`, `session__last_ip` and
  `session__last_user_agent`.
- **Per-user session revocation exists as a bulk action:**
  `DELETE /api/v3/core/authenticated_sessions/bulk_delete/?user_pks=<pk>` → 200 `{"deleted": <n>}`. It needs
  the `authentik_core.delete_authenticatedsession` permission. Seen in the 2026.8.5 source, not exercised by the
  script.

## group_gate

- Create group → 201. `POST /api/v3/core/groups/<group uuid>/add_user/` with `{"pk": <pk>}` → 204, and
  `remove_user/` → 204.
- With the group bound to a test application, sign-in to that application as a non-member was refused.
- The provider needs `grant_types` set. On 2026.x the model default is an empty list, which refuses every
  authorization request with `invalid_request`. The admin UI fills it in; a blueprint or API create must set
  `grant_types: [authorization_code, refresh_token]` itself.

## attributes_patch

`PATCH` with `attributes={"a": 1}` then `attributes={"b": 2}` → 200, 200, leaving only `b`. **PATCH replaces the
whole `attributes` dict; it does not merge.** Updating one key means GET, merge locally, then PATCH the full
dict.

## scope_mapping

With scope mapping `hallpass`:

```python
return {"hallpass_enrollment": request.user.attributes.get("hallpass_enrollment")}
```

the provider preview for the enrolled user showed:

- `sub` equal to the user's bare `uuid` (8-4-4-4-12 hex, no prefix or hash);
- a `hallpass_enrollment` claim with the enrolment value.

The Fluxer `email` mapping is checked with the Fluxer SSO run (hallmonitor `docs/service-notes.md`).

## token_lifetime

`GET /api/v3/core/tokens/?identifier=<identifier>` → `expiring` false. An `expires` timestamp is still present
but does not apply while `expiring` is false.

## Design changes

- **Ending a user's sessions.** The plan's `end_sessions(pk)` listed `?user=<pk>` and deleted each result.
  Against real Authentik that lists every user's sessions, so it would sign everyone out. It changes to a single
  `DELETE /api/v3/core/authenticated_sessions/bulk_delete/?user_pks=<pk>`, returning `deleted`. Because
  deactivation already deletes sessions, the call after `set_active(False)` is a safety net and normally deletes
  0. The Phase 2 fake models `bulk_delete` and ignores `?user=` as real Authentik does.
