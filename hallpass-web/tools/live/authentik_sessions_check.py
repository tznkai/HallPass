# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run check of Authentik's per-user session bulk delete for hallpass. Creates one throwaway user and removes it in a finally.

Environment: AUTHENTIK_URL, AUTHENTIK_TOKEN.
Exit codes: 0 every step ran and the checks held; 1 a step errored or a check did not hold; 2 configuration error;
130 interrupted. The empty-parameter case is deliberately not run: bulk_delete is never called without user_pks.
"""

from __future__ import annotations

import re
import secrets
from collections import Counter
from typing import Any

import httpx
from _live import Console, LiveConfigError, instance_secrets, make_client, required_env

USERS = "/api/v3/core/users/"
SESSIONS = "/api/v3/core/authenticated_sessions/"
BULK_DELETE = f"{SESSIONS}bulk_delete/"
PAGE_SIZE = 100
MAX_PAGES = 1000
LONG_DIGITS = re.compile(r"[0-9]{15,}")


class Skip(Exception):
    """A step cannot run because an earlier step did not produce what it needs."""


class StepFailed(Exception):
    """A step got a response it cannot use, or a check did not hold. The message names a status or field only."""


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def session_user_pk(session: dict[str, Any]) -> Any:
    user = session.get("user")
    return user.get("pk") if isinstance(user, dict) else user


def new_password() -> str:
    """A random password with no run of 15 or more digits, so the console redactor never rewrites it."""
    while True:
        candidate = secrets.token_urlsafe(18)
        if LONG_DIGITS.search(candidate) is None:
            return candidate


class SessionCheck:
    def __init__(self, client: httpx.Client, console: Console) -> None:
        self.client = client
        self.console = console
        self.username = f"hallpass-live-{secrets.token_hex(4)}"
        self.user_pk: int | None = None
        self.test_before = 0
        self.others_before = 0
        self.delete_status: int | None = None
        self.deleted: Any = None

    def run_steps(self) -> int:
        """Runs every step in order. Returns the number of steps that failed."""
        steps: list[tuple[str, Any]] = [
            ("baseline", self.baseline),
            ("create_user", self.create_user),
            ("sign_in", self.sign_in),
            ("before", self.before),
            ("bulk_delete", self.bulk_delete),
            ("after", self.after),
        ]
        failures = 0
        for name, step in steps:
            try:
                step()
            except Skip as exc:
                self.console.step(name, f"skipped: {exc}")
            except (StepFailed, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                failures += 1
                self.console.step(name, f"error {self.console.error(exc)}")
        return failures

    def list_sessions(self) -> list[dict[str, Any]]:
        """Every session, following pagination. Session contents are read for their user pk only."""
        sessions: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self.client.get(SESSIONS, params={"page": page, "page_size": PAGE_SIZE})
            if resp.status_code >= 400:
                raise StepFailed(f"session list status={resp.status_code}")
            data = as_dict(body(resp))
            results = data.get("results")
            sessions.extend(s for s in results or [] if isinstance(s, dict))
            next_page = as_dict(data.get("pagination")).get("next")
            if not isinstance(next_page, int) or next_page <= 0:
                return sessions
            page = next_page
            if page > MAX_PAGES:
                raise StepFailed("session pagination did not end")

    def counts(self, sessions: list[dict[str, Any]]) -> dict[Any, int]:
        return dict(Counter(session_user_pk(s) for s in sessions))

    def baseline(self) -> None:
        counts = self.counts(self.list_sessions())
        total = sum(counts.values())
        per_pk = {str(pk): n for pk, n in sorted(counts.items(), key=lambda kv: str(kv[0]))}
        self.console.step("baseline", f"total={total} per_user_pk={per_pk}")

    def create_user(self) -> None:
        resp = self.client.post(USERS, json={"username": self.username, "name": "Live Check"})
        data = as_dict(body(resp))
        pk = data.get("pk")
        if not resp.is_success or not isinstance(pk, int):
            raise StepFailed(f"create status={resp.status_code} response_keys={sorted(data)}")
        self.user_pk = pk
        password = new_password()
        pw = self.client.post(f"{USERS}{pk}/set_password/", json={"password": password})
        self.console.step("create_user", f"create_status={resp.status_code} set_password_status={pw.status_code}")
        if not pw.is_success:
            raise StepFailed(f"set_password status={pw.status_code}")
        # The only print of the credentials. They are not stored anywhere else in this run.
        self.console.line(f"Test user username: {self.username}")
        self.console.line(f"Test user password (shown once): {password}")

    def sign_in(self) -> None:
        if self.user_pk is None:
            raise Skip("no test user was created")
        self.console.line("In a private window, open <authentik>/ with <authentik> replaced by the instance URL.")
        self.console.line("Sign in as the test user above with the password above. Leave that window signed in.")
        self.console.ask("Press Enter once the test user is signed in.")

    def test_pk(self) -> int:
        if self.user_pk is None:
            raise Skip("no test user was created")
        return self.user_pk

    def before(self) -> None:
        pk = self.test_pk()
        counts = self.counts(self.list_sessions())
        self.test_before = counts.get(pk, 0)
        self.others_before = sum(n for key, n in counts.items() if key != pk)
        self.console.step("before", f"test_user_sessions={self.test_before} others_sessions={self.others_before}")

    def bulk_delete(self) -> None:
        pk = self.test_pk()
        if self.test_before < 1:
            raise Skip("the test user has no session, so the delete is not run")
        # Never called without user_pks: an empty or missing parameter is what this check must not exercise.
        if not isinstance(pk, int) or pk <= 0:
            raise StepFailed("refusing to call bulk_delete without a valid user_pks")
        resp = self.client.delete(BULK_DELETE, params={"user_pks": pk})
        data = as_dict(body(resp))
        self.delete_status = resp.status_code
        self.deleted = data.get("deleted")
        self.console.step("bulk_delete", f"status={resp.status_code} deleted={self.deleted!r} "
                                         f"response_keys={sorted(data)}")
        if not resp.is_success:
            raise StepFailed(f"bulk_delete status={resp.status_code}")

    def after(self) -> None:
        if self.delete_status is None:
            raise Skip("bulk_delete did not run")
        pk = self.test_pk()
        counts = self.counts(self.list_sessions())
        test_after = counts.get(pk, 0)
        others_after = sum(n for key, n in counts.items() if key != pk)
        others_survived = others_after == self.others_before
        deleted_matches = self.deleted == self.test_before
        self.console.step("after", f"test_user_sessions={test_after} others_sessions={others_after}")
        self.console.line(f"others_survived={others_survived}")
        self.console.line(f"deleted_matches={deleted_matches}")
        problems = [
            name for name, ok in (
                ("test user still has sessions", test_after == 0),
                ("other users' sessions changed", others_survived),
                ("deleted count differs from the test user's sessions", deleted_matches),
            ) if not ok
        ]
        if problems:
            raise StepFailed("; ".join(problems))

    def cleanup_all(self) -> None:
        """Deletes the throwaway user. Reported on its own line, so a failed delete is visible."""
        if self.user_pk is None:
            return
        try:
            resp = self.client.delete(f"{USERS}{self.user_pk}/")
            self.console.step("cleanup test user", f"status={resp.status_code}")
        except httpx.HTTPError as exc:
            self.console.step("cleanup test user", f"error {self.console.error(exc)}")


def main() -> int:
    try:
        base_url = required_env("AUTHENTIK_URL")
        token = required_env("AUTHENTIK_TOKEN")
    except LiveConfigError as exc:
        print(f"config error: {exc}")
        return 2
    console = Console([token, *instance_secrets(base_url)])
    client: httpx.Client | None = None
    try:
        client = make_client(base_url, token)
        check = SessionCheck(client, console)
        try:
            failures = check.run_steps()
        finally:
            check.cleanup_all()
    except (KeyboardInterrupt, EOFError):
        console.line("interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - any unexpected error leaves only as its redacted type and message
        console.line(console.error(exc))
        return 1
    finally:
        if client is not None:
            client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
