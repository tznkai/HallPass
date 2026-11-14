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

- `src/hallpass/auth.py:219` `async def resolve_sign_in` — the order of the binding decisions: a known sub, then an enrollment ref, then the admin bootstrap, then refusal
- `src/hallpass/registry.py:201` `enrollment_ref = NULL, invite_url = NULL` — binding clears the ref, so a second Authentik account that carries the same ref matches no person

## Sessions and CSRF

- `src/hallpass/auth.py:121` `def add_session_middleware` — the session cookie's Secure, SameSite=Lax and cookie domain settings
- `src/hallpass/auth.py:125` `domain=config.cookie_domain)` — known risk: the session cookie is parent-domain scoped, so a service behind Caddy's `reverse_proxy` receives it and could replay it against hallpass, admins included, unless the site strips `hallpass_session` from its upstream request as the README Caddy example does
- `src/hallpass/auth.py:147` `async def require_csrf` — every POST checks the form token and the Origin header
- `src/hallpass/web/routes_auth.py:39` `def safe_next` — the only check on where a sign-in returns the person to, so it must refuse off-site URLs

## Admin check

- `src/hallpass/auth.py:273` `async def require_admin` — the one gate for every admin page and action; it tests the Authentik sub against the configured admins

## Fluxer address

- `src/hallpass/registry.py:376` `def fluxer_address` — the order is the override, then the person's own email, then a parent's plus-address; changing it changes which address Fluxer uses

## Authentik calls

- `src/hallpass/authentik.py:129` `isinstance(pk, bool) or not isinstance(pk, int) or pk <= 0` — the end_sessions guard: a missing, zero, negative or boolean pk is refused before any request, so it can never reach Authentik as a bulk delete that could end every user's sessions

## Terms gate

- `src/hallpass/terms.py:100` `def services_blocked(state: TermsState) -> bool:` — the rule for when a person may use services: no version, no acceptance, or an unaccepted blocking version
- `src/hallpass/terms.py:107` `def pages_blocked(state: TermsState) -> bool:` — the page gate, which currently follows the same rule as services; the two names let each caller say what it gates
- `src/hallpass/auth.py:262` `if terms.pages_blocked(state):` — the current_person redirect to /terms; every page that depends on it is gated, and the terms routes are not

## Status transitions

- `src/hallpass/grants.py:69` `if not _is_linked(conn, child_id, parent_id):` — the linked-parent check: a person who is not a linked parent of the child cannot grant, and this check runs first, before any lookup or status change, so a missing child and an unlinked one are refused alike
- `src/hallpass/grants.py:44` `if person.status == "locked":` — a locked person's terms acceptance is refused inside the write's transaction, so a lock that lands after the sign-in check cannot leave an acceptance behind

## Entitlement resolution

- `src/hallpass/entitlements.py:28` `def resolve(rows` — the rule order: a person-level row decides first, then a group grant, then a type grant, and otherwise the person is not entitled
- `src/hallpass/entitlements.py:33` `return row.effect == "grant"` — a person-level deny returns before any group or type grant is read, so it overrides them

## Access decision

- `src/hallpass/access.py:48` `def evaluate(conn` — the one per-person, per-service decision that forward auth, the personal page and the desired-state computation all share
- `src/hallpass/access.py:60` `Access(desired=` — the service is suspended whenever any reason applies, so a retired, unentitled, inactive, blocked or held person is never allowed

## Forward auth

- `src/hallpass/forward_auth.py:107` `if found.kind == "feed":` — the feed-token branch: the token is read from the forwarded URI, and the answer is 200 or 403, never a redirect
- `src/hallpass/forward_auth.py:117` `status_code=302` — the no-session branch, which sends the person to sign-in and is the only redirect the endpoint returns

## Feed tokens

- `src/hallpass/tokens.py:76` `def hash_token(raw` — the raw token is 192 random bits, and only its SHA-256 hex is ever stored
- `src/hallpass/tokens.py:77` `hashlib.sha256(raw.encode())` — the hash that feed lookups compare against, so a database read yields no usable token
- `src/hallpass/tokens.py:86` `UPDATE token SET revoked_at` — issuing a new feed token revokes the person's previous one for the same service, so regenerating cuts off the old URL

## Conflict engine

- `src/hallpass/conflict.py:47` `previous.actor_id != proposed.actor_id` — the reversal test: the previous overlapping action must be by a different parent and be the opposite kind (place vs lift), and admin and system actions never reach this history
- `src/hallpass/conflict.py:52` `now - window < e.at <= now` — the window check: a second reversal locks the child down only when an earlier reversal falls in (now - window, now], so an edge exactly at the window is outside
- `src/hallpass/holds.py:133` `AND id > ?` — the fresh start after a resolved review: only parent actions logged after the child's latest `review_resolved` row (by id) are history, so earlier reversals do not count
- `src/hallpass/holds.py:230` `if not verdict.lockdown or _open_review(conn, child.id):` — a further reversal while a review is open is logged, but opens no second review and no second set of lockdown holds

## Hold permissions

- `src/hallpass/holds.py:329` `raise HoldRefused("strict_not_yours"` — the strict refusal: a parent with no hold of their own cannot lift while another parent's hold exists, and the refusal names those parents
- `src/hallpass/holds.py:332` `if any(hold.kind in ("admin", "lockdown") for hold in held):` — a parent asking to lift a service held only by admin or lockdown holds is refused, so a parent never removes those
- `src/hallpass/holds.py:405` `DELETE FROM hold WHERE review_id = ? AND kind = 'lockdown'` — the only place lockdown holds are deleted, shared by a resolution with the lift option and by the administrator's later lift
- `src/hallpass/web/routes_admin.py:935` `@router.post("/reviews/{review_id}/lift"` — the administrator's lift of a resolved review's lockdown, the only route that lifts one after its resolution; it is gated by `require_admin` and CSRF
- `src/hallpass/holds.py:418` `raise ValueError("review is still open")` — a lockdown cannot be lifted while its review is open

## Outbox

- `src/hallpass/mail.py:106` `except Exception as exc:` — the failure branch of `deliver_due`: a failed send is caught here, so it never reaches the decision that queued the mail
- `src/hallpass/mail.py:108` `next_attempt = _iso(now + _backoff(attempts))` — the retry schedule: each failure pushes the next attempt out by the capped backoff, and the row stays in the outbox

## Authentik sync

- `src/hallpass/authentik_sync.py:34` `def wanted(conn` — the single definition of what Authentik should hold for a person: group membership, the fluxer_email attribute and the active flag
- `src/hallpass/authentik_sync.py:43` `access.evaluate(conn, person, fluxer_service).desired == "allowed"` — the membership rule's entitlement test: a held, unentitled or retired-service person is not a member of the Fluxer group
- `src/hallpass/authentik_sync.py:94` `steps.append(("remove_from_group"` — the restrictive steps run first, so a failed attribute call can never leave a hold or a lock unenforced; each step's failure is logged and the others still run

## Monitor API

- `src/hallpass/monitor_api.py:158` `if any(name in request.headers for name in PROXY_HEADERS):` — the proxy-header refusal: a request carrying a forwarded or Via header gets 403 before its token is read, so nothing that came through the public proxy reaches the monitor routes
- `src/hallpass/monitor_api.py:162` `if not hmac.compare_digest(presented, expected):` — the bearer token check, compared in constant time on the encoded bytes, so a wrong token is refused as 401 whatever its prefix

## Generations

- `src/hallpass/desired.py:114` `generation = _store_generation(conn` — the only route by which `refresh` stores a generation: every subject of every monitored service passes through it on each refresh
- `src/hallpass/desired.py:71` `UPDATE desired_state SET desired` — the bump: a generation goes up by one only when the recomputed value differs from the stored one, so an unchanged value keeps its generation
