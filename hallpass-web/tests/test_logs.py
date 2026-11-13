# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Allowlisted JSON logging and token redaction."""

import json
import logging
import warnings
from collections.abc import Callable, Iterator, Sequence
from io import StringIO

import pytest

from hallpass.logs import TokenRedactionFilter, log_event, setup_logging, token_variants

TOKEN = "SENTINEL-TOKEN-7f3a9c"
OIDC = "SENTINEL-OIDC-SECRET-91c2"
CONTENT = "SENTINEL-CONTENT-b41e"
ESCAPY = 'SENT-"\'-\\-7f3a9c'   # quote, apostrophe and backslash: repr() and json.dumps() both escape it
OIDC_ESCAPY = 'SENT-"\'-\\-91c2'
PINNED_LOGGERS = ("httpx", "httpcore")


@pytest.fixture
def install() -> Iterator[Callable[[Sequence[str]], StringIO]]:
    """setup_logging replaces every root handler and the root level, captures warnings and pins
    the HTTP client loggers: undo all of it."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_pinned = {name: logging.getLogger(name).level for name in PINNED_LOGGERS}

    def _install(secrets: Sequence[str]) -> StringIO:
        stream = StringIO()
        setup_logging("DEBUG", secrets, stream)
        return stream

    try:
        yield _install
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.captureWarnings(False)
        for name, level in saved_pinned.items():
            logging.getLogger(name).setLevel(level)


@pytest.fixture
def captured(install) -> StringIO:
    return install([TOKEN, OIDC])


@pytest.fixture
def captured_escapy(install) -> StringIO:
    return install([ESCAPY, OIDC_ESCAPY])


def lines(stream: StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line]


def parsed(stream: StringIO) -> list[dict]:
    return [json.loads(line) for line in lines(stream)]


def assert_no_secret_forms(text: str, secret: str) -> None:
    """The raw secret, and its repr() and json.dumps() escaped forms, must all be absent."""
    for form in {secret, repr(secret)[1:-1], json.dumps(secret)[1:-1]}:
        assert form not in text


def test_allowlist_keeps_known_keys_and_drops_content_and_names(captured):
    log = logging.getLogger("hallpass.test")
    log_event(log, "x", person_id=7, child_id="1001", content=CONTENT, nickname="bob", command="ping", count=3)
    (record,) = parsed(captured)
    assert record["event"] == "x"
    assert record["person_id"] == 7
    assert record["child_id"] == "1001"
    assert "command" not in record          # not on the allowlist
    assert record["count"] == 3
    assert "content" not in record
    assert "nickname" not in record
    assert CONTENT not in captured.getvalue()


def test_non_identifier_string_and_non_finite_number_are_dropped(captured):
    log = logging.getLogger("hallpass.test")
    log_event(log, "y", reason="has space", status=float("nan"))
    (record,) = parsed(captured)
    assert "reason" not in record
    assert "status" not in record


def test_id_keys_accept_row_ids_and_digit_strings_only(captured):
    log = logging.getLogger("hallpass.test")
    log_event(log, "z", person_id="12x", group_id=True, identity_id=-1, parent_id="42")
    (record,) = parsed(captured)
    assert "person_id" not in record
    assert "group_id" not in record
    assert "identity_id" not in record
    assert record["parent_id"] == "42"


def test_action_outcome_and_service_keys_are_kept_when_identifier_like(captured):
    log = logging.getLogger("hallpass.test")
    log_event(log, "sign_in", action="enrolled", outcome="authentik_failed", service="fluxer", code="SQLITE_BUSY")
    (record,) = parsed(captured)
    assert record["action"] == "enrolled"
    assert record["outcome"] == "authentik_failed"
    assert record["service"] == "fluxer"
    assert record["code"] == "SQLITE_BUSY"


def test_action_with_emoji_is_dropped(captured):
    log = logging.getLogger("hallpass.test")
    log_event(log, "press", action="\U0001F642", outcome="added")
    (record,) = parsed(captured)
    assert "action" not in record
    assert record["outcome"] == "added"


def test_plain_stdlib_record_is_valid_json_with_message_as_event(captured):
    logging.getLogger("hallpass.authentik").warning("request failed with %s", "status 500")
    (record,) = parsed(captured)
    assert record["event"] == "request failed with status 500"
    assert record["logger"] == "hallpass.authentik"
    assert record["level"] == "WARNING"


def test_every_line_is_valid_json(captured):
    log = logging.getLogger("hallpass.test")
    log_event(log, "a", person_id=3, content=CONTENT)
    logging.getLogger("hallpass.authentik").warning("request failed with %s", "status 500")
    log_event(log, "b", reason="has space", status=float("nan"))
    raw = lines(captured)
    assert len(raw) == 3
    for line in raw:
        json.loads(line)


@pytest.mark.parametrize("secret", [TOKEN, OIDC])
def test_secret_in_message_and_args_is_redacted(captured, secret):
    logging.getLogger("x").error("boom %s", secret)
    text = captured.getvalue()
    assert "[REDACTED]" in text
    assert secret not in text


@pytest.mark.parametrize("secret", [TOKEN, OIDC])
def test_secret_in_exception_text_is_redacted(captured, secret):
    log = logging.getLogger("x")
    try:
        raise RuntimeError(f"bad {secret}")
    except RuntimeError:
        log.exception("failed")
    (record,) = parsed(captured)
    assert "[REDACTED]" in record["exc"]
    assert secret not in captured.getvalue()


@pytest.mark.parametrize("secret", [TOKEN, OIDC])
def test_secret_in_chained_cause_is_redacted(captured, secret):
    log = logging.getLogger("x")
    try:
        try:
            raise RuntimeError(f"inner {secret}")
        except RuntimeError as inner:
            raise ValueError("outer") from inner
    except ValueError:
        log.exception("failed")
    (record,) = parsed(captured)
    assert "[REDACTED]" in record["exc"]
    assert secret not in captured.getvalue()


@pytest.mark.parametrize("secret", [TOKEN, OIDC])
def test_mismatched_format_args_do_not_raise_and_still_redact(captured, secret):
    log = logging.getLogger("x")
    log.warning(f"bad {secret} %s %s", 1)            # stdlib would raise TypeError from getMessage()
    log.warning("two args, one given %s %s", secret)
    records = parsed(captured)
    assert len(records) == 2
    assert records[0]["event"] == "bad [REDACTED] %s %s (unformattable args)"
    assert records[1]["event"] == "two args, one given %s %s (unformattable args)"   # args are dropped, not printed
    assert_no_secret_forms(captured.getvalue(), secret)


@pytest.mark.parametrize("secret", [ESCAPY, OIDC_ESCAPY])
def test_repr_escaped_secret_in_args_is_redacted(captured_escapy, secret):
    logging.getLogger("x").warning("headers %r and raw %s", secret, secret)
    (record,) = parsed(captured_escapy)
    assert record["event"] == "headers '[REDACTED]' and raw [REDACTED]"   # %r keeps its own quotes
    assert_no_secret_forms(captured_escapy.getvalue(), secret)


@pytest.mark.parametrize("secret", [ESCAPY, OIDC_ESCAPY])
def test_repr_escaped_secret_in_exception_traceback_is_redacted(captured_escapy, secret):
    log = logging.getLogger("x")
    try:
        raise RuntimeError(f"connection failed: {secret!r}")
    except RuntimeError:
        log.exception("connection failed: %r", secret)
    (record,) = parsed(captured_escapy)
    assert "[REDACTED]" in record["exc"]
    assert_no_secret_forms(captured_escapy.getvalue(), secret)


@pytest.mark.parametrize("secret", [ESCAPY, OIDC_ESCAPY])
def test_secret_escaped_twice_in_exception_message_is_redacted(captured_escapy, secret):
    # The secret is repr'd into the exception message, then the exception is repr'd again by %r.
    logging.getLogger("x").warning("%r", RuntimeError(f"x {secret!r}"))
    (record,) = parsed(captured_escapy)
    assert "[REDACTED]" in record["event"]
    assert_no_secret_forms(captured_escapy.getvalue(), secret)
    assert secret.split("-")[-1] not in captured_escapy.getvalue()   # the unique suffix survives every escape


@pytest.mark.parametrize("secret", [ESCAPY, OIDC_ESCAPY])
def test_secret_escaped_three_levels_is_redacted(captured_escapy, secret):
    inner = RuntimeError(f"x {secret!r}")
    outer = RuntimeError(f"y {inner!r}")
    logging.getLogger("x").warning("%r", outer)
    (record,) = parsed(captured_escapy)
    assert "[REDACTED]" in record["event"]
    assert_no_secret_forms(captured_escapy.getvalue(), secret)
    assert secret.split("-")[-1] not in captured_escapy.getvalue()


def test_all_secrets_in_one_message_are_redacted(captured):
    logging.getLogger("x").error("authentik %s and oidc %s", TOKEN, OIDC)
    text = captured.getvalue()
    assert TOKEN not in text
    assert OIDC not in text
    assert text.count("[REDACTED]") == 2


def test_empty_secret_is_ignored_and_does_not_blank_the_output(install):
    stream = install(["", TOKEN])
    logging.getLogger("x").error("hello %s", "world")
    (record,) = parsed(stream)
    assert record["event"] == "hello world"


def test_token_variants_of_plain_token_is_only_the_token():
    assert token_variants(TOKEN) == [TOKEN]


def test_token_variants_is_bounded_for_escape_heavy_token():
    # Three escape rounds, at most four escape functions each: 1 + 4 + 16 + 64 forms at most.
    assert len(token_variants(ESCAPY)) <= 85


def test_bare_string_secret_is_refused_by_setup_logging():
    with pytest.raises(TypeError, match="secrets must be a sequence of strings"):
        setup_logging("INFO", TOKEN, StringIO())


def test_bare_string_secret_is_refused_by_the_filter():
    with pytest.raises(TypeError, match="secrets must be a sequence of strings"):
        TokenRedactionFilter(TOKEN)


def test_http_client_loggers_are_pinned_to_warning(install):
    install([TOKEN])
    for name in PINNED_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


def test_http_client_info_lines_with_urls_are_not_written(install):
    stream = install([TOKEN])
    logging.getLogger("httpx").info("HTTP Request: GET %s", "SENTINEL-URL-5d1e")
    assert stream.getvalue() == ""


def test_warning_text_with_token_is_json_and_redacted(captured):
    with warnings.catch_warnings():
        warnings.simplefilter("always")               # -W error would otherwise raise instead of showing it
        warnings.warn(f"leaky {TOKEN}", UserWarning, stacklevel=1)
    (record,) = parsed(captured)
    assert record["logger"] == "py.warnings"
    assert record["level"] == "WARNING"
    assert "[REDACTED]" in record["event"]
    assert_no_secret_forms(captured.getvalue(), TOKEN)
