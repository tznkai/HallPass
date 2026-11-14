# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The monitor API wire types, as hallmonitor reads and writes them.

Hallpass keeps its own copy of these shapes. Both copies are checked against tests/contract/monitor_v1.json, which is
byte-identical in both projects. Error messages name the key that is wrong, never the value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

OBSERVED = frozenset({"allowed", "suspended", "unsupported", "banned_external", "error"})
DESIRED = frozenset({"allowed", "suspended"})


class ContractError(ValueError):
    """A body does not have the shape the contract requires."""


@dataclass(frozen=True)
class Identity:
    kind: str
    value: str


@dataclass(frozen=True)
class Subject:
    ref: str
    identities: tuple[Identity, ...]
    desired: str
    generation: int


@dataclass(frozen=True)
class ServiceDesired:
    slug: str
    adapter: str
    subjects: tuple[Subject, ...]


@dataclass(frozen=True)
class Report:
    slug: str
    ref: str
    observed: str
    detail: str | None
    generation: int
    at: datetime


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{where} must be an object")
    return value


def _field(obj: Mapping[str, Any], key: str) -> Any:
    if key not in obj:
        raise ContractError(f"missing key {key}")
    return obj[key]


def _text(obj: Mapping[str, Any], key: str) -> str:
    value = _field(obj, key)
    if not isinstance(value, str):
        raise ContractError(f"{key} must be a string")
    return value


def _number(obj: Mapping[str, Any], key: str) -> int:
    value = _field(obj, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{key} must be an integer")
    return value


def _items(obj: Mapping[str, Any], key: str) -> list[Any]:
    value = _field(obj, key)
    if not isinstance(value, list):
        raise ContractError(f"{key} must be a list")
    return value


def _subject(raw: Any) -> Subject:
    obj = _object(raw, "subject")
    identities = tuple(
        Identity(kind=_text(_object(item, "identity"), "kind"), value=_text(_object(item, "identity"), "value"))
        for item in _items(obj, "identities")
    )
    desired = _text(obj, "desired")
    if desired not in DESIRED:
        raise ContractError("desired must be allowed or suspended")
    return Subject(ref=_text(obj, "ref"), identities=identities, desired=desired,
                   generation=_number(obj, "generation"))


def parse_desired(body: Mapping[str, Any]) -> list[ServiceDesired]:
    """The services of a `GET /desired` body. Keys the contract does not name are dropped, never kept."""
    services = []
    for raw in _items(_object(body, "desired body"), "services"):
        obj = _object(raw, "service")
        services.append(ServiceDesired(
            slug=_text(obj, "slug"),
            adapter=_text(obj, "adapter"),
            subjects=tuple(_subject(item) for item in _items(obj, "subjects")),
        ))
    return services


def iso_utc(moment: datetime) -> str:
    """ISO-8601 in UTC with a `Z` suffix, to the second. A naive time is refused rather than guessed."""
    if moment.tzinfo is None:
        raise ContractError("at must carry a timezone")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _report_entry(report: Report) -> dict[str, Any]:
    if report.observed not in OBSERVED:
        raise ContractError("observed is not a known value")
    return {
        "slug": report.slug,
        "ref": report.ref,
        "observed": report.observed,
        "detail": report.detail,
        "generation": report.generation,
        "at": iso_utc(report.at),
    }


def reports_body(reports: Sequence[Report], unmanaged: Mapping[str, int]) -> dict[str, Any]:
    """The `POST /reports` body. Unmanaged counts are sorted by slug so the body is stable."""
    counts = []
    for slug, count in sorted(unmanaged.items()):
        if count < 0:
            raise ContractError("unmanaged count must not be negative")
        counts.append({"slug": slug, "count": count})
    return {"reports": [_report_entry(report) for report in reports], "unmanaged": counts}
