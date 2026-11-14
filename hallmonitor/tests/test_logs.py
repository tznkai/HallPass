# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logging: only allowlisted fields survive, and a configured secret never reaches the output."""

from __future__ import annotations

import io
import json
import logging

from hallmonitor.logs import JsonFormatter, clean_fields, log_event, setup_logging, token_variants

SENTINEL = "SENTINEL-" + "TOKEN-value"


def test_clean_fields_keeps_allowlisted_keys_only() -> None:
    kept = clean_fields({"ref": "1001", "generation": 2, "observed": "suspended", "display": "Steve", "count": 3})
    assert kept == {"ref": "1001", "generation": 2, "observed": "suspended", "count": 3}


def test_clean_fields_drops_identifier_values_that_are_not_safe() -> None:
    kept = clean_fields({"ref": "not a number", "reason": "has space", "adapter": "fluxer"})
    assert kept == {"adapter": "fluxer"}


def test_token_variants_include_escaped_forms() -> None:
    variants = token_variants("a'b")
    assert "a'b" in variants
    assert "a\\'b" in variants


def test_setup_logging_redacts_a_secret_and_writes_json() -> None:
    stream = io.StringIO()
    handler = setup_logging("INFO", [SENTINEL], stream=stream)
    try:
        log_event(logging.getLogger("hallmonitor.test"), "cycle_done", count=1, detail=f"sent {SENTINEL}")
    finally:
        logging.getLogger().removeHandler(handler)
    text = stream.getvalue()
    assert SENTINEL not in text
    record = json.loads(text.splitlines()[0])
    assert record["event"] == "cycle_done"
    assert record["count"] == 1


def test_formatter_is_json() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "ev", None, None)
    assert json.loads(JsonFormatter().format(record))["event"] == "ev"
