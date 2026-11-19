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

- `src/hallpass/auth.py:281` `async def resolve_sign_in` — the order of the binding decisions: a known sub, then an enrollment ref, then the admin bootstrap, then refusal
- `src/hallpass/registry.py:204` `enrollment_ref = NULL, invite_url = NULL` — binding clears the ref, so a second Authentik account that carries the same ref matches no person

## Sessions and CSRF

- `src/hallpass/auth.py:177` `def add_session_middleware` — the session cookie's Secure, SameSite=Lax and cookie domain settings
- `src/hallpass/auth.py:181` `domain=config.cookie_domain)` — known risk: the session cookie is parent-domain scoped, so a service behind Caddy's `reverse_proxy` receives it and could replay it against hallpass, admins included, unless the site strips `hallpass_session` from its upstream request as the README Caddy example does
- `src/hallpass/auth.py:203` `async def require_csrf` — every POST checks the form token and the Origin header
- `src/hallpass/web/routes_auth.py:48` `def safe_next` — the only check on where a sign-in returns the person to, so it
  must refuse off-site URLs
- `src/hallpass/auth.py:315` `revoked = await deps.db.run(lambda c: revocation.session_ended(c, sid=sid` — the check
  every page makes: a session whose sid was revoked, whose person was revoked after it started, or that started
  more than the max age ago, is ended here and the person is sent to sign-in
- `src/hallpass/web/routes_auth.py:77` `end_session(request)` — a sign-in that starts ends the session the browser
  already holds, so a request to /login never renews an existing session's cookie
- `src/hallpass/auth.py:223` `request.session["id_token_handle"] = handle` — the cookie holds only the handle of the ID token kept on the server, never the token, so it carries no name, username or groups

## Admin check

- `src/hallpass/auth.py:344` `async def require_admin` — the one gate for every admin page and action; it tests the Authentik sub against the configured admins

## Fluxer address

- `src/hallpass/registry.py:427` `def fluxer_address` — the order is the override, then the person's own email, then a parent's plus-address; changing it changes which address Fluxer uses

## Authentik calls

- `src/hallpass/authentik.py:129` `isinstance(pk, bool) or not isinstance(pk, int) or pk <= 0` — the end_sessions guard: a missing, zero, negative or boolean pk is refused before any request, so it can never reach Authentik as a bulk delete that could end every user's sessions

## Terms gate

- `src/hallpass/terms.py:100` `def services_blocked(state: TermsState) -> bool:` — the rule for when a person may use services: no version, no acceptance, or an unaccepted blocking version
- `src/hallpass/terms.py:107` `def pages_blocked(state: TermsState) -> bool:` — the page gate, which currently follows the same rule as services; the two names let each caller say what it gates
- `src/hallpass/auth.py:332` `blocked = await _deps(request).db.run(lambda c: terms.pages_blocked_for(c, person))` — the current_person redirect to /terms; every page that depends on it is gated, and the terms routes are not. A child is never sent there
- `src/hallpass/terms.py:117` `return not any(` — a child is covered only by a linked, unlocked parent who passes the parent rule; a child's own acceptance is never read for the gate
- `src/hallpass/web/routes_auth.py:111` `if person.account_type != "child" and not state.accepted_current:` — the sign-
  in redirect to /terms: a child is never sent there, and an adult or parent keeps the rule
- `src/hallpass/web/routes_terms.py:49` `if person.account_type == "child":` — a child's POST to /terms/accept is refused with 403 before anything is written

## Status transitions

- `src/hallpass/grants.py:110` `if not _is_linked(conn, child_id, parent_id):` — the linked-parent check: a person who is not a linked parent of the child cannot grant, and this check runs first, before any lookup or status change, so a missing child and an unlinked one are refused alike
- `src/hallpass/grants.py:54` `if person.status == "locked":` — a locked person's terms acceptance is refused inside the write's transaction, so a lock that lands after the sign-in check cannot leave an acceptance behind
- `src/hallpass/grants.py:56` `if person.account_type == "child":` — a child's acceptance is refused before any write, because a child's terms are covered by a parent, not by their own acceptance
- `src/hallpass/grants.py:147` `if person.account_type != "child" or person.status != "invited":` — the sign-in settle reads the status inside its own transaction and moves only an invited child, so a lock is never overwritten
- `src/hallpass/grants.py:125` `raise GrantRefused("already_granted")` — the second approval of an invited, not yet enrolled child is refused while a grant by a linked parent counts, so the approval is never recorded twice; an enrolled child is approved again and made active instead
- `src/hallpass/grants.py:86` `AND p.status != 'locked'` — a grant by a locked parent counts for nothing, so it neither settles a child at sign-in nor blocks another parent's approval
- `src/hallpass/grants.py:176` `if to_child == (old_type == "child"):` — a change between adult and parent moves no status; an active person made a child waits for a parent's grant, and an awaiting child made an adult becomes active
- `src/hallpass/web/routes_admin.py:274` `grants.change_account_type(conn, person_id` — the admin's type change moves the status in the same transaction as the account type, so the person is never left active as a child

## Entitlement resolution

- `src/hallpass/entitlements.py:28` `def resolve(rows` — the rule order: a person-level row decides first, then a group grant, then a type grant, and otherwise the person is not entitled
- `src/hallpass/entitlements.py:33` `return row.effect == "grant"` — a person-level deny returns before any group or type grant is read, so it overrides them

## Access decision

- `src/hallpass/access.py:48` `def evaluate(conn` — the one per-person, per-service decision that forward auth, the personal page and the desired-state computation all share
- `src/hallpass/access.py:60` `Access(desired=` — the service is suspended whenever any reason applies, so a retired, unentitled, inactive, blocked or held person is never allowed

## Forward auth

- `src/hallpass/forward_auth.py:120` `if found.kind == "feed":` — the feed-token branch: the token is read from the forwarded URI, and the answer is 200 or 403, never a redirect
- `src/hallpass/forward_auth.py:135` `status_code=302` — the no-session branch, which sends the person to sign-in and is the only redirect the endpoint returns
- `src/hallpass/forward_auth.py:129` `if person_id is not None and await deps` — a revoked session, or one past its
  absolute max age, gets the same redirect as a missing one, and it is cleared, so forward auth never trusts an ended
  sid

## Feed tokens

- `src/hallpass/tokens.py:76` `def hash_token(raw` — the raw token is 192 random bits, and only its SHA-256 hex is ever stored
- `src/hallpass/tokens.py:77` `hashlib.sha256(raw.encode())` — the hash that feed lookups compare against, so a database read yields no usable token
- `src/hallpass/tokens.py:86` `UPDATE token SET revoked_at` — issuing a new feed token revokes the person's previous one for the same service, so regenerating cuts off the old URL

## Conflict engine

- `src/hallpass/conflict.py:47` `previous.actor_id != proposed.actor_id` — the reversal test: the previous overlapping action must be by a different parent and be the opposite kind (place vs lift), and admin and system actions never reach this history
- `src/hallpass/conflict.py:52` `now - window < e.at <= now` — the window check: a second reversal locks the child down only when an earlier reversal falls in (now - window, now], so an edge exactly at the window is outside
- `src/hallpass/holds.py:133` `AND id > ?` — the fresh start after a resolved review: only parent actions logged after the child's latest `review_resolved` row (by id) are history, so earlier reversals do not count
- `src/hallpass/holds.py:289` `if not verdict.lockdown or _open_review(conn, child.id):` — a further reversal while a review is open is logged, but opens no second review and no second set of lockdown holds

## Hold permissions

- `src/hallpass/holds.py:387` `raise HoldRefused("strict_not_yours"` — the strict refusal: a parent with no hold of their own cannot lift while another parent's hold exists, and the refusal names those parents
- `src/hallpass/holds.py:390` `if any(hold.kind in ("admin", "lockdown") for hold in held):` — a parent asking to lift a service held only by admin or lockdown holds is refused, so a parent never removes those
- `src/hallpass/holds.py:217` `def _pend_manual_restores` — a parent's lift that leaves a marked manual service with no hold keeps the mark as a pending restore and mails the administrator once; the mark is kept, not cleared, so the suspension done by hand is never forgotten. A lockdown that the same lift causes is decided first, so it holds the service and no restore is pended
- `src/hallpass/holds.py:474` `DELETE FROM hold WHERE review_id = ? AND kind = 'lockdown'` — the only place lockdown holds are deleted, shared by a resolution with the lift option and by the administrator's later lift
- `src/hallpass/web/routes_admin.py:970` `@router.post("/reviews/{review_id}/lift"` — the administrator's lift of a resolved review's lockdown, the only route that lifts one after its resolution; it is gated by `require_admin` and CSRF
- `src/hallpass/holds.py:487` `raise ValueError("review is still open")` — a lockdown cannot be lifted while its review is open
- `src/hallpass/web/routes_family.py:103` `def hold_return(back` — the open-redirect guard for a hold change: the form's `back` field is honoured only as the fixed value `children`, and any other value goes to the child's page, so no address from the request is ever redirected to

## Outbox

- `src/hallpass/mail.py:106` `except Exception as exc:` — the failure branch of `deliver_due`: a failed send is caught here, so it never reaches the decision that queued the mail
- `src/hallpass/mail.py:108` `next_attempt = _iso(now + _backoff(attempts))` — the retry schedule: each failure pushes the next attempt out by the capped backoff, and the row stays in the outbox

## Authentik sync

- `src/hallpass/web/routes_terms.py:74` `affected = await deps.db.run(lambda c: authentik_sync.affected_by(c, person.id))` — a parent's acceptance can cover linked children, so the parent and each linked child are synced after it, and a child's Fluxer membership follows the parent's acceptance
- `src/hallpass/authentik_sync.py:34` `def wanted(conn` — the single definition of what Authentik should hold for a person: group membership, the fluxer_email attribute and the active flag
- `src/hallpass/authentik_sync.py:43` `access.evaluate(conn, person, fluxer_service).desired == "allowed"` — the membership rule's entitlement test: a held, unentitled or retired-service person is not a member of the Fluxer group
- `src/hallpass/authentik_sync.py:94` `steps.append(("remove_from_group"` — the restrictive steps run first, so a failed attribute call can never leave a hold or a lock unenforced; each step's failure is logged and the others still run

## Monitor API

- `src/hallpass/monitor_api.py:174` `if any(name in request.headers for name in PROXY_HEADERS):` — the proxy-header refusal: a request carrying a forwarded or Via header gets 403 before its token is read, so nothing that came through the public proxy reaches the monitor routes
- `src/hallpass/monitor_api.py:178` `if not hmac.compare_digest(presented, expected):` — the bearer token check, compared in constant time on the encoded bytes, so a wrong token is refused as 401 whatever its prefix
- `src/hallpass/monitor_api.py:133` `return stored is not None and generation <= stored[1]` — the report gate: a report for a pair that is not sent, or whose generation is above the stored one, is refused; one at or below the stored generation is stored, and shows as confirmed only at the stored generation

## Shared identities

- `src/hallpass/desired.py:151` `if kind == SSO_SUBJECT_KIND or subject.desired == "suspended"` — decides which identities hallmonitor is sent: an allowed holder never gets a paused shared identity, so a suspension of any holder cannot leave it whitelisted for another

## Generations

- `src/hallpass/desired.py:214` `generation = _store_generation(conn` — the only route by which `refresh_with_pauses` (and so `refresh`) stores a generation: every subject that is sent to hallmonitor passes through it on each refresh, and a subject with only paused identities is not sent and is not stored
- `src/hallpass/desired.py:78` `UPDATE desired_state SET desired` — the bump: a generation goes up by one when the recomputed value differs from the stored one, or when a pair that was not sent is sent again, so a returning pair starts above every generation it had; a sent pair with the same value keeps its generation
- `src/hallpass/desired.py:97` `UPDATE desired_state SET sent = 0` — a pair that stops being a subject keeps its row, unsent, so its generation stays on record for a return; deleting the row here would let a return start again at a low generation and confirm a late report
- `src/hallpass/desired.py:233` `AND sent = 1` — `desired_for` reads only sent pairs, so a report for a pair that is not sent is refused, and no page shows the pair as monitored

## Back-channel logout

- `src/hallpass/logout.py:28` `ALGORITHMS = ["RS256"]` — the only algorithm a logout token may use, named here rather
  than read from the token
- `src/hallpass/web/routes_auth.py:185` `logout.check_header(token)` — a token that is not an RS256 JWS with a string kid
  is refused before the provider's keys are asked for, so unauthenticated garbage costs no outbound request
- `src/hallpass/logout.py:147` `return _check_claims(decoded.claims` — the claims are read only after the signature has
  verified against the provider's keys
- `src/hallpass/logout.py:92` `if "nonce" in claims:` — a token with a nonce is an ID token, not a logout token, and is
  refused
- `src/hallpass/web/routes_auth.py:189` `if error.reason != "unknown_kid":` — the keys are fetched again once, and only
  for an unknown kid
- `src/hallpass/web/routes_auth.py:237` `async def backchannel_logout` — the public endpoint takes no session and no
  CSRF token; every failure answers 400 and records nothing
- `src/hallpass/web/routes_auth.py:217` `if declared is not None and (not declared.isdigit()` — the body cap: a
  declared length over the limit is refused before anything is read, and a chunked body is read a chunk at a time
  and refused once it passes the limit, so the endpoint never parses an oversized body
- `src/hallpass/web/routes_auth.py:151` `return logout.end_session_url(` — the sign-out address is built only from
  discovery, the stored ID token and the public URL, never from the request
- `src/hallpass/web/routes_auth.py:163` `revocation.revoke_session(c, sid=sid` — sign-out records the session's sid
  here as well as at the provider, so a copy of the cookie is refused at once, not only when the provider's logout
  arrives
- `src/hallpass/revocation.py:55` `if started_at < stamp(now - timedelta(seconds=max_age_seconds)):` — the absolute
  lifetime: a session that started before now minus the max age is ended whatever the revocation rows say, so a row
  pruned after its window cannot revive a cookie that a response re-signed with a fresh timestamp
- `src/hallpass/revocation.py:70` `return started_at is None or row[0] > started_at` — a subject-wide revocation ends only sessions that started before it; a session with no start time is ended by session_ended, which every request uses first
- `src/hallpass/auth.py:149` `cooled = self._last_fetch is None` — every key fetch attempt, forced or not, is at least FORCED_REFRESH_COOLDOWN_SECONDS after the last one, failed ones included, and serialised by a lock, so unauthenticated traffic cannot make hallpass fetch the provider's keys more than once per window
  Accepted risk, agreed by the user on 2026-11-18: after a failed key fetch with no keys cached, every logout token
  is refused for up to FORCED_REFRESH_COOLDOWN_SECONDS (60 s). Observed on 2026-11-18: Authentik retried a delivery
  refused with 404 (a wrong Logout URI), and a retry after the URI was restored ended the session. Inferred, not
  observed: a 400 from this endpoint is retried the same way, since Authentik's task raises on any non-2xx response,
  so a retry after the window usually gets through. A session ends at max age or at sign-out only if every attempt
  is refused. The trade is a bounded number of outbound key fetches against a revocation that is delayed, or missed
  if every attempt is refused.
- `src/hallpass/revocation.py:40` `ended_at = min(issued_at, now)` — a subject-wide revocation is stamped with the logout token's issue time, never later than now, so a late or replayed delivery cannot end sessions started after the logout event
