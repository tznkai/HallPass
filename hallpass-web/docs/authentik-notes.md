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
  the `authentik_core.delete_authenticatedsession` permission.
- **bulk_delete is per-user (verified live, `tools/live/authentik_sessions_check.py`).** A throwaway user signed
  in once, giving 1 session, while 3 other users had sessions. `bulk_delete/?user_pks=<pk>` → 200
  `{"deleted": 1}`. Afterwards the test user had 0 sessions, and every other user's session count was unchanged
  (checked user by user: none dropped, none rose). The returned `deleted` matched the count removed.

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

## Phase 2 sign-in

Hallpass ran against the test Authentik at `<hallpass>` behind the reverse proxy, with the strict redirect URI
`<hallpass>/auth/callback` on the hallpass OIDC provider.

- **Admin bootstrap:** signing in as the Authentik admin, whose `sub` is listed in `HALLPASS_ADMIN_SUBS`, made that
  person the hallpass admin.
- **Invite:** the admin created a child with no email. The page showed an invite URL of the form
  `<authentik>/if/flow/<flow-slug>/?itoken=<pk>`.
- **Enrolment:** in a private window, the invite URL led through username and password to a new Authentik
  account. The flow then ended on Authentik's own application library (`<authentik>/if/user/#library`), not on
  hallpass, because the invite URL carries no `next`.
- **First sign-in and binding:** opening `<hallpass>/` in the same window signed the child in to hallpass. The
  admin's people page then showed the child's username, so the enrolment value bound the record to the account.
- **Single use:** opening the invite URL a second time was refused.

## Design changes

- **Ending a user's sessions.** The plan's `end_sessions(pk)` listed `?user=<pk>` and deleted each result.
  Against real Authentik that lists every user's sessions, so it would sign everyone out. It changes to a single
  `DELETE /api/v3/core/authenticated_sessions/bulk_delete/?user_pks=<pk>`, returning `deleted`. Because
  deactivation already deletes sessions, the call after `set_active(False)` is a safety net and normally deletes
  0. The Phase 2 fake models `bulk_delete` and ignores `?user=` as real Authentik does.
- **Guarding the bulk delete.** An empty or missing `user_pks` must never reach Authentik as "all users". Phase 2's
  `end_sessions` must (a) refuse to call `bulk_delete` with an empty or missing pk, and (b) Phase 2's fake
  Authentik must reject a missing or empty `user_pks` with 400 rather than treat it as all. The live run confirmed
  that a given pk scopes the delete to that user. What Authentik does with an empty parameter was deliberately
  not tried, so the guard stays.

## Phase 8: sign-out and back-channel logout (pending live checks)

Research on Authentik 2026.8.5, from its source and documentation. None of it has run live yet. The code relies on each
item below, so each stays pending until a live run confirms it. Record the result under the item, with placeholders.

- **End-session redirect.** The discovery document's `end_session_endpoint` takes `id_token_hint` and
  `post_logout_redirect_uri`. `post_logout_redirect_uri` needs the hint, and it must match a redirect URI of type
  Logout. Pending: a sign-out at hallpass returns the browser to `<public_url>/signed-out`.
- **Invalidation flow.** The end-session request runs the provider's invalidation flow, and that ends the Authentik
  session only when the flow has a User Logout stage. Pending: after the sign-out, a visit to hallpass asks for
  sign-in again, and the Authentik session cookie is gone.
- **Back-channel on sign-out at Authentik.** Pending: a sign-out at Authentik sends one logout token to the Logout URI,
  and hallpass ends that session only.
- **Back-channel on admin session deletion.** Pending: deleting a user's session in Authentik sends a logout token with
  that session's `sid`.
- **Back-channel on user deactivation.** Pending: deactivating a user sends a logout token with the subject alone, and
  hallpass ends that person's sessions started before it.
- **Issuer and keys.** Pending: the `iss` of a live logout token equals the discovery `issuer`, and its `kid` is in the
  document at `jwks_uri`. The token is signed RS256.
- **`sid` match.** Pending: the `sid` in a logout token equals the `sid` in the ID token of the same session, which is
  the SHA-256 hex of Authentik's session key.
- **Cookie size.** Pending: the session cookie, which now carries the stored ID token, stays under the browser's 4096
  byte limit with the real token. A cookie that is too large is dropped, and sign-in would then loop.
