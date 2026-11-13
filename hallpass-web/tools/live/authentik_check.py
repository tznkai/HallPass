# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run Authentik live checks for hallpass. Creates its own objects and removes them in a finally.

Environment: AUTHENTIK_URL, AUTHENTIK_TOKEN, AUTHENTIK_INVITE_FLOW, AUTHENTIK_TOKEN_IDENTIFIER.
Exit codes: 0 every step ran; 1 at least one step errored; 2 configuration error; 130 interrupted.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from typing import Any

import httpx
from _live import Console, LiveConfigError, instance_secrets, make_client, required_env

USERS = "/api/v3/core/users/"
INVITATIONS = "/api/v3/stages/invitation/invitations/"
GROUPS = "/api/v3/core/groups/"
SESSIONS = "/api/v3/core/authenticated_sessions/"
TOKENS = "/api/v3/core/tokens/"
FLOWS = "/api/v3/flows/instances/"

SCOPE_HALLPASS_EXPRESSION = """\
return {"hallpass_enrollment": request.user.attributes.get("hallpass_enrollment")}"""
SCOPE_EMAIL_EXPRESSION = """\
return {"email": request.user.attributes.get("fluxer_email"), "email_verified": True}"""


class Skip(Exception):
    """A step cannot run because an earlier step did not produce what it needs."""


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def results_of(resp: httpx.Response) -> list[dict[str, Any]]:
    results = as_dict(body(resp)).get("results")
    return [r for r in results or [] if isinstance(r, dict)]


def find_key_path(node: Any, needle: str, prefix: str = "") -> str | None:
    """The dotted key path under which `needle` is stored as a value, or None. Values are never returned."""
    if not isinstance(node, dict):
        return None
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if value == needle:
            return path
        found = find_key_path(value, needle, path)
        if found is not None:
            return found
    return None


def session_user_pk(session: dict[str, Any]) -> Any:
    user = session.get("user")
    return user.get("pk") if isinstance(user, dict) else user


class AuthentikChecks:
    def __init__(self, client: httpx.Client, console: Console, flow_slug: str, token_identifier: str) -> None:
        self.client = client
        self.console = console
        self.flow_slug = flow_slug
        self.token_identifier = token_identifier
        self.tag = secrets.token_hex(4)
        self.enroll_token = secrets.token_hex(16)
        self.group_name = f"hallpass-live-{self.tag}"
        self.user_pk: Any = None  # created by user_without_email
        self.user_uuid: str | None = None
        self.enrolled_pk: Any = None  # the user the invitation enrolled
        self.enrolled_uuid: str | None = None
        self.invitation_pk: str | None = None
        self.group_pk: str | None = None
        self.cleanup: list[tuple[str, str]] = []  # (label, DELETE path), in creation order

    def run_steps(self) -> int:
        """Runs every step in order. Returns the number of steps that errored."""
        steps: list[tuple[str, Callable[[], None]]] = [
            ("user_without_email", self.user_without_email),
            ("invitation", self.invitation),
            ("invitation_binding", self.invitation_binding),
            ("uuid_lookup", self.uuid_lookup),
            ("deactivate_sessions", self.deactivate_sessions),
            ("group_gate", self.group_gate),
            ("attributes_patch", self.attributes_patch),
            ("scope_mapping", self.scope_mapping),
            ("token_lifetime", self.token_lifetime),
        ]
        failures = 0
        for name, step in steps:
            try:
                step()
            except Skip as exc:
                self.console.step(name, f"skipped: {exc}")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                failures += 1
                self.console.step(name, f"error {self.console.error(exc)}")
        return failures

    def user_without_email(self) -> None:
        resp = self.client.post(USERS, json={"username": f"hallpass-live-{self.tag}", "name": "Live Check"})
        data = as_dict(body(resp))
        pk, uuid = data.get("pk"), data.get("uuid")
        if resp.is_success:
            self.user_pk, self.user_uuid = pk, uuid
            self.cleanup.append(("test user", f"{USERS}{pk}/"))
        self.console.step(
            "user_without_email",
            f"status={resp.status_code} pk_is_int={isinstance(pk, int)} uuid_present={bool(uuid)} "
            f"pk={pk} uuid={uuid}",
        )

    def invitation(self) -> None:
        flows = results_of(self.client.get(FLOWS, params={"slug": self.flow_slug}))
        if not flows:
            raise Skip("no flow matches AUTHENTIK_INVITE_FLOW")
        resp = self.client.post(INVITATIONS, json={
            "name": f"hallpass-live-{self.tag}",
            "single_use": True,
            "flow": flows[0].get("pk"),
            "fixed_data": {"attributes.hallpass_enrollment": self.enroll_token},
        })
        data = as_dict(body(resp))
        pk = data.get("pk")
        if not resp.is_success or pk is None:
            self.console.step("invitation", f"status={resp.status_code} response_keys={sorted(data)}")
            return
        self.invitation_pk = str(pk)
        self.cleanup.append(("invitation", f"{INVITATIONS}{pk}/"))
        self.console.step(
            "invitation",
            f"status={resp.status_code} single_use={data.get('single_use')} "
            f"url=<authentik>/if/flow/{self.flow_slug}/?itoken={pk}",
        )

    def invitation_binding(self) -> None:
        if self.invitation_pk is None:
            raise Skip("no invitation was created")
        self.console.line("Open the invitation URL printed above on the Authentik host, in a private window.")
        self.console.line("Enrol as a new user and do not give an email address.")
        self.console.ask("Press Enter when the enrolment has finished.")

        user, how = self.find_enrolled_user()
        if user is None:
            self.console.step("invitation_binding", f"attribute not found (lookup={how})")
        else:
            self.enrolled_pk, self.enrolled_uuid = user.get("pk"), user.get("uuid")
            self.cleanup.append(("enrolled user", f"{USERS}{self.enrolled_pk}/"))
            key_path = find_key_path(user.get("attributes"), self.enroll_token, prefix="attributes")
            self.console.step("invitation_binding", f"attribute found at {key_path} (lookup={how})")

        resp = self.client.get(f"{INVITATIONS}{self.invitation_pk}/")
        if resp.status_code == 404:
            self.console.step("invitation_binding", "invitation deleted or consumed (single use)")
        else:
            data = as_dict(body(resp))
            self.console.step(
                "invitation_binding",
                f"invitation still present status={resp.status_code} single_use={data.get('single_use')}",
            )

    def find_enrolled_user(self) -> tuple[dict[str, Any] | None, str]:
        """Looks for the enrolled user by the attribute value. Other users' data is never printed."""
        filtered = self.client.get(USERS, params={"attributes": json.dumps({"hallpass_enrollment": self.enroll_token})})
        for user in results_of(filtered):
            if find_key_path(user.get("attributes"), self.enroll_token, prefix="attributes") is not None:
                return user, f"attributes filter status={filtered.status_code}"
        recent = self.client.get(USERS, params={"ordering": "-date_joined", "page_size": 50})
        for user in results_of(recent):
            if find_key_path(user.get("attributes"), self.enroll_token, prefix="attributes") is not None:
                return user, f"attributes filter status={filtered.status_code}, recent-users scan"
        return None, f"attributes filter status={filtered.status_code}, recent-users scan"

    def uuid_lookup(self) -> None:
        target_pk, target_uuid = self.target()
        resp = self.client.get(USERS, params={"uuid": target_uuid})
        results = results_of(resp)
        matches = [u for u in results if u.get("uuid") == target_uuid]
        pk_match = bool(matches) and matches[0].get("pk") == target_pk
        self.console.step(
            "uuid_lookup",
            f"status={resp.status_code} filter_works={len(matches) == 1} "
            f"pk_is_int={isinstance(matches[0].get('pk'), int) if matches else False} pk_matches={pk_match}",
        )

    def deactivate_sessions(self) -> None:
        if self.enrolled_pk is None:
            raise Skip("no enrolled user")
        patch = self.client.patch(f"{USERS}{self.enrolled_pk}/", json={"is_active": False})
        status, sessions = self.sessions_for(self.enrolled_pk)
        mine = [s for s in sessions if session_user_pk(s) == self.enrolled_pk]
        deleted = 0
        for session in mine:
            sid = session.get("uuid") or session.get("pk")
            if sid is None:
                continue
            resp = self.client.delete(f"{SESSIONS}{sid}/")
            deleted += resp.is_success or resp.status_code == 404
        _, after = self.sessions_for(self.enrolled_pk)
        after_mine = [s for s in after if session_user_pk(s) == self.enrolled_pk]
        self.console.step(
            "deactivate_sessions",
            f"patch_status={patch.status_code} filter_status={status} "
            f"filter_scoped={len(mine) == len(sessions)} sessions_before={len(mine)} "
            f"deleted={deleted} sessions_after={len(after_mine)}",
        )

    def sessions_for(self, user_pk: Any) -> tuple[int, list[dict[str, Any]]]:
        resp = self.client.get(SESSIONS, params={"user": user_pk})
        return resp.status_code, results_of(resp)

    def group_gate(self) -> None:
        user_pk = self.target()[0]
        resp = self.client.post(GROUPS, json={"name": self.group_name})
        data = as_dict(body(resp))
        guid = data.get("pk")
        if not resp.is_success or guid is None:
            self.console.step("group_gate", f"create_status={resp.status_code} response_keys={sorted(data)}")
            return
        self.group_pk = str(guid)
        self.cleanup.append(("group", f"{GROUPS}{guid}/"))
        add = self.client.post(f"{GROUPS}{guid}/add_user/", json={"pk": user_pk})
        remove = self.client.post(f"{GROUPS}{guid}/remove_user/", json={"pk": user_pk})
        # Deactivation in deactivate_sessions would make every sign-in fail, so the gate needs the user active.
        reactivate = self.client.patch(f"{USERS}{user_pk}/", json={"is_active": True})
        self.console.line(f"Reactivated the test user for the gate test (status={reactivate.status_code}).")
        self.console.line(f"In the Authentik UI, bind group {self.group_name} to a test application's policies.")
        self.console.line("Then sign in to that application as the test user, who is not in the group.")
        refused = self.console.ask_bool("Was sign-in refused for the non-member?")
        self.console.step(
            "group_gate",
            f"create={resp.status_code} add_user={add.status_code} remove_user={remove.status_code} "
            f"non_member_refused={refused}",
        )

    def attributes_patch(self) -> None:
        # The created user, not the enrolled one: a PATCH that replaces attributes would drop hallpass_enrollment.
        if self.user_pk is None:
            raise Skip("no test user was created")
        first = self.client.patch(f"{USERS}{self.user_pk}/", json={"attributes": {"a": 1}})
        second = self.client.patch(f"{USERS}{self.user_pk}/", json={"attributes": {"b": 2}})
        final = as_dict(body(self.client.get(f"{USERS}{self.user_pk}/")))
        keys = sorted(as_dict(final.get("attributes")))
        self.console.step(
            "attributes_patch",
            f"statuses={first.status_code},{second.status_code} keys_after={keys} a_survives={'a' in keys}",
        )

    def scope_mapping(self) -> None:
        self.console.line("In the Authentik UI, create two scope mappings on the hallpass test provider.")
        self.console.line("Scope name `hallpass`, expression:")
        self.console.line(SCOPE_HALLPASS_EXPRESSION)
        self.console.line("Scope name `email` (Fluxer provider), expression:")
        self.console.line(SCOPE_EMAIL_EXPRESSION)
        self.console.line("Then run an OIDC sign-in with a test client, for example the provider's preview.")
        sub_is_uuid = self.console.ask_bool("Does sub equal the bare user uuid (subject mode UUID)?")
        claim_seen = self.console.ask_bool("Does the hallpass_enrollment claim appear?")
        self.console.step("scope_mapping", f"sub_is_uuid={sub_is_uuid} hallpass_enrollment_claim={claim_seen}")

    def token_lifetime(self) -> None:
        results = [t for t in results_of(self.client.get(TOKENS, params={"identifier": self.token_identifier}))
                   if t.get("identifier") == self.token_identifier]
        if not results:
            self.console.step("token_lifetime", "no token with the configured identifier")
            return
        token = results[0]
        self.console.step("token_lifetime", f"expiring={token.get('expiring')} expires={token.get('expires')}")

    def target(self) -> tuple[Any, str]:
        """The user the later steps act on: the enrolled user when found, else the created test user."""
        if self.enrolled_pk is not None and self.enrolled_uuid is not None:
            return self.enrolled_pk, self.enrolled_uuid
        if self.user_pk is not None and self.user_uuid is not None:
            return self.user_pk, self.user_uuid
        raise Skip("no test user exists")

    def cleanup_all(self) -> None:
        """Deletes everything this run created, newest first. Each delete is reported on its own line."""
        for label, path in reversed(self.cleanup):
            try:
                resp = self.client.delete(path)
                self.console.step(f"cleanup {label}", f"status={resp.status_code}")
            except httpx.HTTPError as exc:
                self.console.step(f"cleanup {label}", f"error {self.console.error(exc)}")


def main() -> int:
    try:
        base_url = required_env("AUTHENTIK_URL")
        token = required_env("AUTHENTIK_TOKEN")
        flow_slug = required_env("AUTHENTIK_INVITE_FLOW")
        token_identifier = required_env("AUTHENTIK_TOKEN_IDENTIFIER")
    except LiveConfigError as exc:
        print(f"config error: {exc}")
        return 2
    console = Console([token, *instance_secrets(base_url)])
    client = make_client(base_url, token)
    checks = AuthentikChecks(client, console, flow_slug, token_identifier)
    try:
        failures = checks.run_steps()
    except (KeyboardInterrupt, EOFError):
        console.line("interrupted")
        return 130
    finally:
        checks.cleanup_all()
        client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
