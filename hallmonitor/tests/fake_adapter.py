# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A list-backed adapter for tests: the service's allow list is a set of identity values.

A subject is observed `allowed` when one of its identity values is on the list, and `suspended` otherwise. Applying
`allowed` adds the subject's first identity value; applying `suspended` removes all of them. A list entry that no
subject carries is counted as unmanaged and is never touched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from hallmonitor.adapters.base import AdapterError, Capabilities, Observed
from hallmonitor.contract import Subject


@dataclass
class ListAdapter:
    allow: set[str] = field(default_factory=set)
    unreachable: bool = False
    calls: list[str] = field(default_factory=list)   # "observe" or "apply:<ref>", in call order

    capabilities = Capabilities(suspend=True, restore=True, read_state=True, removes_connected=True)

    def _check_reachable(self) -> None:
        if self.unreachable:
            raise AdapterError("unreachable")

    async def observe(self, subjects: Sequence[Subject]) -> dict[str, Observed]:
        self.calls.append("observe")
        self._check_reachable()
        observed = {}
        for subject in subjects:
            values = {identity.value for identity in subject.identities}
            observed[subject.ref] = Observed("allowed" if values & self.allow else "suspended")
        return observed

    async def apply(self, subject: Subject, desired: str) -> Observed:
        self.calls.append(f"apply:{subject.ref}")
        self._check_reachable()
        if desired == "allowed":
            self.allow.add(subject.identities[0].value)
        else:
            self.allow -= {identity.value for identity in subject.identities}
        return Observed(desired)  # type: ignore[arg-type]

    async def unmanaged_count(self, subjects: Sequence[Subject]) -> int | None:
        known = {identity.value for subject in subjects for identity in subject.identities}
        return len(self.allow - known)
