# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entitlement resolution: the pure rule over entitlement rows. Covers hallpass.AC5.3 (resolve)."""

from __future__ import annotations

import pytest

from hallpass.entitlements import Row, resolve


@pytest.mark.parametrize(
    ("rows", "account_type", "group_ids", "expected"),
    [
        pytest.param([Row("group", "5", "grant"), Row("person", "7", "deny")], "adult", frozenset({5}), False,
                     id="person-deny-beats-group-grant"),
        pytest.param([Row("type", "adult", "grant"), Row("person", "7", "deny")], "adult", frozenset(), False,
                     id="person-deny-beats-type-grant"),
        pytest.param([Row("person", "7", "grant")], "child", frozenset(), True,
                     id="person-grant-with-no-other-rows"),
        pytest.param([], "adult", frozenset({5}), False, id="no-rows"),
        pytest.param([Row("group", "5", "grant")], "adult", frozenset({5}), True,
                     id="group-grant-for-a-member"),
        pytest.param([Row("group", "5", "grant")], "adult", frozenset({6}), False,
                     id="group-grant-for-a-group-not-held"),
        pytest.param([Row("type", "parent", "grant")], "parent", frozenset(), True,
                     id="type-grant-for-the-persons-type"),
        pytest.param([Row("type", "parent", "grant")], "adult", frozenset(), False,
                     id="type-grant-for-another-type"),
    ],
)
def test_resolve_follows_the_person_group_type_order(rows, account_type, group_ids, expected) -> None:
    assert resolve(rows, account_type=account_type, group_ids=group_ids, person_id=7) is expected


def test_person_grant_on_one_person_does_not_entitle_another() -> None:
    rows = [Row("person", "7", "grant")]
    assert resolve(rows, account_type="adult", group_ids=frozenset(), person_id=8) is False
