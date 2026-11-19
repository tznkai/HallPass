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

## Phase 8: sign-out and back-channel logout

Research on Authentik 2026.8.5, from its source and documentation, then checked live on 2026-11-18 with test plan
Part H. H1, H2, H4, H5 and H7 passed; H3 passed after the Logout URI was corrected; H6 passed and showed the
research wrong on retries. Each item says what the live run showed.

- **End-session redirect.** The discovery document's `end_session_endpoint` takes `id_token_hint` and
  `post_logout_redirect_uri`. `post_logout_redirect_uri` needs the hint, and it must match a redirect URI of type
  Logout. Confirmed (H1): a sign-out at hallpass returns the browser to `<public_url>/signed-out`.
- **Invalidation flow.** The end-session request runs the provider's invalidation flow, and that ends the Authentik
  session only when the flow has a User Logout stage. Confirmed (H1, on `default-invalidation-flow`): after the
  sign-out, a visit to hallpass asks for sign-in again and Authentik asks for the password.
- **Back-channel on sign-out at Authentik.** Confirmed (H2): a sign-out at Authentik sends one logout token to the
  Logout URI, and hallpass ends that session on the next request.
- **Back-channel on admin session deletion.** Confirmed (H3): deleting a user's sessions in Authentik (Directory,
  Users, the user's Sessions tab) sends logout tokens, and both of that person's browsers had to sign in again. Not
  shown: one token per session. The log count of `outcome=ok` lines was not taken.
- **Back-channel on user deactivation.** Confirmed (H4): deactivating a user ends their hallpass session on the next
  page. The run did not show whether the token carried a `sid` or the subject alone.
- **Issuer and keys.** Confirmed (H2, H3): live logout tokens passed hallpass's checks (`outcome=ok`), so the `iss`
  matched the discovery `issuer`, the `kid` was in the `jwks_uri` document, and the token was signed RS256.
- **`sid` match.** Shown (H2): a logout token ended the session it named. Shown (H1): another person's session in a
  second browser stayed signed in after a sign-out at hallpass; whether a logout token arrived in H1 was not recorded.
  Not shown directly: that the token carried a `sid`, or that the `sid` matched the one in that session's ID token. The
  run cannot tell a sid token from a subject-only token, because the surviving session belonged to another person. What
  would show it: a `revoked_session` row holding that session's `sid` after the logout, or a second session of the same
  person surviving a logout token that named only one session.
- **Cookie size.** Seen (every sign-in in Part H): sign-in did not loop. The cookie size was not measured. By design the
  cookie holds a handle to the ID token kept on the server, not the token.
- **Default invalidation flow.** The provider's default invalidation flow, `default-provider-invalidation-flow`, has no
  User Logout stage (research only; the run used `default-invalidation-flow`, which works (H1), and did not try the
  provider default).
- **Retry after a failed delivery.** Wrong in the research (H6): Authentik retries a delivery that fails. With a wrong
  Logout URI answering 404, the worker log showed the send task failing and being queued again several times, about
  30 seconds to 2 minutes apart. Once the Logout URI was put back, the next retry reached hallpass (`outcome=ok`) and
  the session ended on the next request. Observed: a 404 was retried, more than once. Inferred, not observed: a 400 from
  hallpass is retried the same way, since Authentik's task raises on any non-2xx response. Not observed: how many
  retries there are, how long they continue, and whether a retry resends the same token or a rebuilt one. That last
  point matters. A resent token can lapse at `exp`, after which the `exp` + 60 s check in `logout.py` refuses it. A
  rebuilt token carries a later `iat`, which weakens the guarantee at `revocation.py:40` only if the person signs in
  again within the retry period.
- **Logout URI typo.** Seen live (H3, first attempt): the post-logout redirect URI `/signed-out` was entered as the
  Logout URI, with a trailing full stop (`<public_url>/signed-out.`). The worker's `POST /signed-out.` was answered
  404, and nothing was revoked until the Logout URI was corrected to `/auth/backchannel-logout`. Copy the URI exactly;
  the hallpass access log shows the worker's `POST` and the path it used.
