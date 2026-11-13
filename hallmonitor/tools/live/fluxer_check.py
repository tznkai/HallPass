# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run Fluxer admin live checks for hallmonitor. Acts only on the --user test account it is given.

Environment: FLUXER_ADMIN_URL (API base, before /v1), FLUXER_ADMIN_KEY.
Exit codes: 0 every step ran; 1 at least one step failed; 2 configuration error; 130 interrupted.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx
from _live import Console, LiveConfigError, instance_secrets, make_client, required_env

DISABLED_BIT = 38
DISABLED_MASK = 1 << DISABLED_BIT
FILL_TEXT = "hallmonitor live check"
LIFT_STATEMENT = "hallmonitor live check"
# Lookup (user and SSO identity), list sessions, and terminate sessions (which Fluxer files under update_flags).
MINIMAL_ACLS = ("user:lookup", "user:list_sessions", "user:update_flags")
DIGITS = re.compile(r"[0-9]+")
UUID_SHAPE = re.compile(r"[0-9a-fA-F-]{36}")
USER_FIELDS = ("flags", "temp_banned_until", "enforcement_decision_id")


class Skip(Exception):
    """A step cannot run because an earlier step did not produce what it needs."""


class StepFailed(Exception):
    """A step got a response it cannot use. The message names the status or field, never a value."""


def body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def top_keys(payload: Any) -> list[str]:
    return sorted(payload) if isinstance(payload, dict) else []


def describe(record: dict[str, Any], field: str) -> str:
    if field not in record:
        return "absent"
    value = record[field]
    return f"type={type(value).__name__} value={value!r}"


def unwrap_user(payload: Any) -> tuple[str, dict[str, Any]]:
    """Accepts {"users": [...]} or a bare user object. Returns the shape name and the first user."""
    if isinstance(payload, dict) and isinstance(payload.get("users"), list):
        users = [u for u in payload["users"] if isinstance(u, dict)]
        return "wrapped {users: [...]}", (users[0] if users else {})
    if isinstance(payload, dict):
        return "bare object", payload
    return f"unexpected {type(payload).__name__}", {}


def error_fields(payload: Any) -> list[str]:
    """Field names a validation error names, in order and without duplicates. Messages and values are dropped."""
    names: list[str] = []

    def add(name: Any) -> None:
        if isinstance(name, list):
            strings = [part for part in name if isinstance(part, str)]
            name = strings[-1] if strings else None
        if isinstance(name, str) and name not in names:
            names.append(name)

    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict):
                    add(item.get("field", item.get("loc", item.get("name"))))
        elif isinstance(errors, dict):
            for key in errors:
                add(key)
        detail = payload.get("detail")
        if isinstance(detail, list):
            for item in detail:
                if isinstance(item, dict):
                    add(item.get("loc"))
        add(payload.get("field"))
    return names


def listed_entries(payload: Any) -> list[Any]:
    """The bare list, or the first list inside a wrapper object. Empty when neither is present."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return next((v for v in payload.values() if isinstance(v, list)), [])
    return []


def count_sessions(payload: Any) -> tuple[int, int]:
    """Returns (active, total). The admin list also returns ended sessions, each with `deleted_at` set."""
    if isinstance(payload, dict) and isinstance(payload.get("sessions"), list):
        entries = payload["sessions"]
    elif isinstance(payload, list):
        entries = payload
    else:
        raise StepFailed(f"unexpected sessions shape, top_keys={top_keys(payload)}")
    active = sum(1 for s in entries if not (isinstance(s, dict) and s.get("deleted_at")))
    return active, len(entries)


def contains_value(node: Any, wanted: str) -> bool:
    if isinstance(node, dict):
        return any(contains_value(v, wanted) for v in node.values())
    if isinstance(node, list):
        return any(contains_value(v, wanted) for v in node)
    if isinstance(node, bool):
        return False
    if isinstance(node, (int, str)):
        return str(node) == wanted
    return False


class FluxerChecks:
    def __init__(
        self,
        client: httpx.Client,
        console: Console,
        user_id: str,
        decision_json: dict[str, Any] | None,
        key_owner: str | None,
        subject: str | None,
    ) -> None:
        self.client = client
        self.console = console
        self.user_id = user_id
        self.user_path = f"/v1/admin/users/{user_id}"
        self.decision_json = decision_json
        self.key_owner = key_owner
        self.subject = subject
        self.decision_id: Any = None
        self.failures = 0

    def run_steps(self) -> int:
        """Runs every step in order. Returns the number of steps that failed."""
        self.run("lookup", self.lookup)
        self.run("suspend", self.suspend)
        self.run("sessions", self.sessions)
        self.run("decision_owner", self.decision_owner)
        self.run("lift", self.lift)
        self.run("lookup_sso", self.lookup_sso)
        self.run("acls", self.acls)
        return self.failures

    def run(self, name: str, step: Callable[[], None]) -> None:
        try:
            step()
        except Skip as exc:
            self.console.step(name, f"skipped: {exc}")
        except StepFailed as exc:
            self.failures += 1
            self.console.step(name, f"failed: {exc}")
        except httpx.HTTPError as exc:
            self.failures += 1
            self.console.step(name, f"error {self.console.error(exc)}")

    def show_user(self, label: str) -> dict[str, Any]:
        """Reads the test account, prints its shape and the three fields the checks record, and returns it."""
        resp = self.client.get(self.user_path)
        payload = body(resp)
        if resp.status_code >= 400:
            raise StepFailed(f"{label} status={resp.status_code}")
        shape, user = unwrap_user(payload)
        self.console.step(
            label,
            f"status={resp.status_code} shape={shape} top_keys={top_keys(payload)} user_keys={sorted(user)}",
        )
        for field in USER_FIELDS:
            self.console.line(f"  {field}: {describe(user, field)}")
        flags = user.get("flags")
        if isinstance(flags, int):
            self.console.line(f"  disabled_bit_set={bool(flags & DISABLED_MASK)}")
        return user

    def lookup(self) -> None:
        self.show_user("lookup")

    def suspend(self) -> None:
        decision: dict[str, Any] = {} if self.decision_json is None else dict(self.decision_json)
        resp = self.put_ban(decision)
        self.console.step("suspend", f"status={resp.status_code} sent_decision_keys={sorted(decision)}")
        if resp.status_code == 400 and self.decision_json is None:
            names = error_fields(body(resp))
            self.console.line(f"  validation error fields={names}")
            fill = {name: FILL_TEXT for name in names if name not in decision}
            if not fill:
                raise StepFailed("400 named no fields; re-run with --decision-json")
            decision = {**decision, **fill}
            resp = self.put_ban(decision)
            self.console.step("suspend retry", f"status={resp.status_code} sent_decision_keys={sorted(decision)}")
        if resp.status_code >= 400:
            raise StepFailed(f"suspend status={resp.status_code}")
        self.console.line(f"  accepted ban body keys=[duration_hours, decision] response_keys={top_keys(body(resp))}")
        user = self.show_user("lookup after suspend")
        self.decision_id = user.get("enforcement_decision_id")

    def put_ban(self, decision: dict[str, Any] | None) -> httpx.Response:
        payload: dict[str, Any] = {"duration_hours": 0}
        if decision is not None:
            payload["decision"] = decision
        return self.client.put(f"{self.user_path}/ban", json=payload)

    def require_decision(self) -> str:
        if self.decision_id is None:
            raise Skip("the test account exposes no enforcement_decision_id")
        return quote(str(self.decision_id), safe="")

    def session_count(self) -> tuple[int, int]:
        resp = self.client.get(f"{self.user_path}/sessions")
        if resp.status_code >= 400:
            raise StepFailed(f"sessions status={resp.status_code}")
        return count_sessions(body(resp))

    def sessions(self) -> None:
        active, total = self.session_count()
        self.console.step("sessions before delete", f"active={active} listed={total}")
        resp = self.client.delete(f"{self.user_path}/sessions")
        payload = body(resp)
        terminated = payload.get("terminated_count") if isinstance(payload, dict) else None
        self.console.step("sessions delete", f"status={resp.status_code} terminated_count={terminated}")
        if resp.status_code >= 400:
            raise StepFailed(f"sessions delete status={resp.status_code}")
        active, total = self.session_count()
        self.console.step("sessions after delete", f"active={active} listed={total}")

    def decision_owner(self) -> None:
        decision_id = self.require_decision()
        resp = self.client.get(f"/v1/admin/enforcement-decisions/{decision_id}")
        payload = body(resp)
        if resp.status_code >= 400:
            raise StepFailed(f"decision status={resp.status_code}")
        self.console.step("decision_owner", f"status={resp.status_code} top_keys={top_keys(payload)}")
        decided_by = payload.get("decided_by") if isinstance(payload, dict) else None
        if self.key_owner is None:
            self.console.line(f"  decided_by_present={decided_by is not None}; no --key-owner, comparison not made")
        else:
            self.console.line(f"  decided_by_equals_key_owner={str(decided_by) == self.key_owner}")

    def lift(self) -> None:
        decision_id = self.require_decision()
        resp = self.client.post(
            f"/v1/admin/enforcement-decisions/{decision_id}/end",
            json={"outcome": "lifted", "statement": LIFT_STATEMENT},
        )
        self.console.step("lift via decision", f"status={resp.status_code} response_keys={top_keys(body(resp))}")
        if resp.status_code >= 400:
            raise StepFailed(f"lift status={resp.status_code}")
        self.show_user("lookup after lift")
        self.run("lift without decision", self.lift_without_decision)

    def lift_without_decision(self) -> None:
        resp = self.put_ban(None)
        self.console.step("ban without decision", f"status={resp.status_code} response_keys={top_keys(body(resp))}")
        if resp.status_code >= 400:
            self.console.line("  the API did not accept a suspension without a decision")
            return
        resp = self.client.delete(f"{self.user_path}/ban")
        self.console.step("unban without decision", f"status={resp.status_code}")
        if resp.status_code >= 400:
            raise StepFailed(f"unban status={resp.status_code}; the test account may still be suspended")
        self.show_user("lookup after unban")

    def lookup_sso(self) -> None:
        if self.subject is None:
            raise Skip("no --subject given")
        # The subject lookup needs the provider id too; the user's own identity list supplies it.
        resp = self.client.get(f"{self.user_path}/sso-identities")
        payload = body(resp)
        if resp.status_code >= 400:
            raise StepFailed(f"user sso-identities status={resp.status_code}")
        identities = payload.get("sso_identities") if isinstance(payload, dict) else None
        identities = identities if isinstance(identities, list) else []
        mine = [i for i in identities if isinstance(i, dict) and i.get("subject") == self.subject]
        self.console.step(
            "lookup_sso user identities",
            f"status={resp.status_code} count={len(identities)} subject_bound={bool(mine)} "
            f"identity_keys={sorted(identities[0]) if identities and isinstance(identities[0], dict) else []}",
        )
        for identity in identities:
            if isinstance(identity, dict):
                self.console.line(f"  bound subject={identity.get('subject')}")
        if not mine or not mine[0].get("provider_id"):
            raise StepFailed("the subject is not bound to this account, so there is no provider id to look it up by")
        resp = self.client.get(
            "/v1/admin/sso-identities",
            params={"provider_id": mine[0]["provider_id"], "subject": self.subject},
        )
        payload = body(resp)
        if resp.status_code >= 400:
            raise StepFailed(f"sso-identities status={resp.status_code}")
        self.console.step("lookup_sso by subject", f"status={resp.status_code} top_keys={top_keys(payload)}")
        self.console.line(f"  returns_fluxer_user_id={contains_value(payload, self.user_id)}")

    def acls(self) -> None:
        resp = self.client.get("/v1/admin/api-keys")
        payload = body(resp)
        self.console.step("acls", f"status={resp.status_code} top_keys={top_keys(payload)}")
        self.console.line(f"  minimal_acl_set={', '.join(MINIMAL_ACLS)}")
        if resp.status_code >= 400:
            self.console.line("  the endpoint does not expose the key's ACLs")
            return
        entries = listed_entries(payload)
        acls: set[str] = set()
        for entry in entries:
            if isinstance(entry, dict):
                for field in ("acls", "permissions", "scopes"):
                    values = entry.get(field)
                    if isinstance(values, list):
                        acls.update(v for v in values if isinstance(v, str))
        self.console.line(f"  entries={len(entries)} acl_values_across_listed_keys={sorted(acls)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hand-run Fluxer admin live checks against one test account.")
    parser.add_argument("--user", required=True, help="id of a dedicated test account, never a real member")
    parser.add_argument(
        "--decision-json",
        help="JSON object sent as the ban's decision (default: minimal body, then the fields a 400 names)",
    )
    parser.add_argument("--key-owner", help="account id of the admin key's owner, to compare with decided_by")
    parser.add_argument("--subject", help="Authentik uuid of the SSO test user; enables the lookup_sso step")
    args = parser.parse_args()

    if not DIGITS.fullmatch(args.user):
        print("config error: --user must be a numeric id")
        return 2
    if args.key_owner is not None and not DIGITS.fullmatch(args.key_owner):
        print("config error: --key-owner must be a numeric id")
        return 2
    if args.subject is not None and not UUID_SHAPE.fullmatch(args.subject):
        print("config error: --subject must be a uuid")
        return 2
    decision_json: dict[str, Any] | None = None
    if args.decision_json is not None:
        try:
            parsed = json.loads(args.decision_json)
        except ValueError:
            print("config error: --decision-json is not valid JSON")
            return 2
        if not isinstance(parsed, dict):
            print("config error: --decision-json must be a JSON object")
            return 2
        decision_json = parsed

    try:
        base_url = required_env("FLUXER_ADMIN_URL")
        key = required_env("FLUXER_ADMIN_KEY")
    except LiveConfigError as exc:
        print(f"config error: {exc}")
        return 2

    extra = [args.user, args.key_owner or "", args.subject or ""]
    console = Console([key, *instance_secrets(base_url), *extra])
    client = make_client(base_url, key)
    checks = FluxerChecks(client, console, args.user, decision_json, args.key_owner, args.subject)
    try:
        failures = checks.run_steps()
    except (KeyboardInterrupt, EOFError):
        console.line("interrupted")
        return 130
    finally:
        client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
