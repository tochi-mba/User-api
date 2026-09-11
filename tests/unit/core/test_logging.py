"""Structured logging, and the processor that keeps a person's record out of it.

keyring's redactor works by field name because its secrets live in fields called
``password`` and ``token``. Here the sensitive thing is the *value* of a field called
``body`` or ``value_json`` or ``description`` -- a therapist's name, an address, what
somebody said last month -- so the name list is a second mechanism rather than the same
one reused. A log record is not a private place: records are shipped to aggregators, kept
longer than anyone intends, and read by whoever is debugging at the time.

:class:`TestExactMatching` is the subtle one. ``value`` as a *substring* would also redact
``value_type`` and ``values_logged``, which are metadata and nobody's personal data, and a
redactor that eats the metadata is one somebody eventually turns off.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import structlog

from user_api.core.config import LogFormat
from user_api.core.context import bind_account_id, bind_request_id
from user_api.core.logging import (
    MAX_REDACTION_DEPTH,
    REDACTED,
    add_account_id,
    add_request_id,
    configure_logging,
    get_logger,
    is_sensitive,
    redact_secrets,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

SENSITIVE_ANYWHERE = (
    "authorization",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
    "api_key",
)
"""Names that mean "a credential", wherever they appear. Written out rather than imported,
so that shortening the list in the source fails a test instead of quietly widening what is
logged."""

CONTENT_FIELDS = (
    "body",
    "description",
    "detail",
    "note",
    "old_value",
    "search_text",
    "source_detail",
    "value",
    "value_json",
    "values",
)
"""Names that carry what a person told us. Written out for the same reason."""

SENTINEL = "Dr Okonkwo at the Meadow Clinic"
"""A value of the kind this service exists to hold, for the end-to-end tests."""


def render(event_dict: dict[str, object]) -> dict[str, object]:
    """Run one event dict through the redaction processor."""
    return dict(redact_secrets(None, "info", event_dict))


def nest(depth: int, leaf: object) -> object:
    """Bury ``leaf`` under ``depth`` layers of dictionary."""
    buried: object = leaf
    for _ in range(depth):
        buried = {"nested": buried}
    return buried


class TestSensitiveNames:
    @pytest.mark.parametrize("field", SENSITIVE_ANYWHERE)
    def test_a_credential_name_is_sensitive_wherever_it_appears(self, field: str) -> None:
        assert is_sensitive(field) is True

    @pytest.mark.parametrize("field", CONTENT_FIELDS)
    def test_a_field_carrying_what_a_person_told_us_is_sensitive(self, field: str) -> None:
        assert is_sensitive(field) is True

    @pytest.mark.parametrize(
        "field",
        ["refresh_token", "client_secret", "x_api_key", "session_cookie", "signing_private_key"],
    )
    def test_a_compound_credential_name_is_matched_as_a_substring(self, field: str) -> None:
        # Matched loosely on purpose. The cost of an over-broad rule is a redacted field
        # nobody needed; the cost of a narrow one is a bearer token in a log file.
        assert is_sensitive(field) is True

    def test_matching_is_case_insensitive(self) -> None:
        # Header names arrive capitalised, and a case-sensitive rule would sail past them.
        assert is_sensitive("Authorization") is True
        assert is_sensitive("BODY") is True

    @pytest.mark.parametrize(
        "field",
        ["entry_id", "key", "entry_type", "account_id", "scope", "count", "event"],
    )
    def test_the_metadata_worth_having_is_left_alone(self, field: str) -> None:
        # Redacting everything would be safe and useless. A record still has to say what
        # happened, or nobody can debug this service without reading somebody's notes.
        assert is_sensitive(field) is False


class TestExactMatching:
    """Content names are matched exactly; credential names are matched as substrings.

    The distinction is what lets ``values_logged`` -- how many values a sweep touched --
    stay in the record while ``values`` itself never does.
    """

    @pytest.mark.parametrize("field", ["value_type", "values_logged", "body_bytes", "note_kind"])
    def test_a_name_that_merely_starts_with_a_content_name_survives(self, field: str) -> None:
        assert is_sensitive(field) is False

    def test_the_content_names_themselves_do_not(self) -> None:
        assert is_sensitive("value") is True
        assert is_sensitive("values") is True


class TestRedaction:
    def test_a_sensitive_value_never_survives_to_the_record(self) -> None:
        assert render({"body": SENTINEL})["body"] == REDACTED

    def test_an_ordinary_field_is_left_exactly_as_it_was(self) -> None:
        assert render({"event": "entry_written", "entry_id": "ent_1", "values_logged": 3}) == {
            "event": "entry_written",
            "entry_id": "ent_1",
            "values_logged": 3,
        }

    def test_a_non_string_value_is_replaced_rather_than_rendered(self) -> None:
        # Replaced wholesale because the type and the length are themselves information:
        # `body=None` would say no note was sent, and a list would leak through its repr.
        assert render({"body": None})["body"] == REDACTED
        assert render({"value": [SENTINEL, "second"]})["value"] == REDACTED

    def test_nesting_does_not_smuggle_content_past_the_processor(self) -> None:
        # Entry content reaches a log call as a structure far more often than as a
        # top-level string -- a serialised entry, a request payload, an error context.
        redacted = render({"entry": {"key": "therapist", "value": SENTINEL}})

        assert redacted["entry"] == {"key": "therapist", "value": REDACTED}

    def test_content_inside_a_list_is_redacted_too(self) -> None:
        redacted = render({"entries": [{"key": "a", "body": SENTINEL}, {"key": "b"}]})

        assert redacted["entries"] == [{"key": "a", "body": REDACTED}, {"key": "b"}]

    def test_a_non_string_key_is_stringified_so_the_record_still_renders(self) -> None:
        # A JSON renderer cannot serialise an integer key. Stringifying here is what stops
        # a stray one from killing the log call that was carrying the error.
        payload = render({"payload": {1: "one", "body": SENTINEL}})["payload"]

        assert isinstance(payload, dict)
        assert set(payload) == {"1", "body"}

    def test_a_value_under_a_non_string_key_is_redacted_rather_than_name_checked(self) -> None:
        # Fails closed, and it has to. The name check runs on the key, and a key that is
        # not a string has to be rendered before it can be checked -- at which point
        # b"body" has become "b'body'", which matches no content name and walks straight
        # past the redactor carrying the thing it was supposed to catch.
        payload = render({"payload": {b"body": SENTINEL, 1: SENTINEL}})["payload"]

        assert isinstance(payload, dict)
        assert list(payload.values()) == [REDACTED, REDACTED]

    def test_a_structure_deeper_than_the_cap_is_dropped_rather_than_walked(self) -> None:
        # Fails closed. A structure this deep is either a bug or an attempt to bury
        # something past the walker, and neither has earned the right to be rendered.
        deep = render({"payload": nest(MAX_REDACTION_DEPTH + 2, {"harmless": SENTINEL})})

        assert REDACTED in json.dumps(deep)
        assert SENTINEL not in json.dumps(deep)

    def test_a_structure_within_the_cap_is_walked_all_the_way_down(self) -> None:
        # The cap has to be high enough that ordinary records still read properly,
        # otherwise the redactor becomes the reason the logs are useless.
        walked = render({"payload": nest(MAX_REDACTION_DEPTH - 2, {"key": "a", "body": SENTINEL})})

        assert SENTINEL not in json.dumps(walked)
        assert '"key": "a"' in json.dumps(walked)


class TestContextProcessors:
    def test_the_request_id_is_attached_when_one_is_bound(self) -> None:
        with bind_request_id("req-1"):
            assert add_request_id(None, "info", {})["request_id"] == "req-1"

    def test_the_request_id_is_absent_rather_than_null_outside_a_request(self) -> None:
        # Absent rather than present-and-null, so a log query can filter on existence
        # instead of having to know which of two spellings of "no request" it will meet.
        assert "request_id" not in add_request_id(None, "info", {})

    def test_the_account_id_is_attached_when_the_request_has_one(self) -> None:
        with bind_account_id("account-a"):
            assert add_account_id(None, "info", {})["account_id"] == "account-a"

    def test_the_account_id_is_absent_on_an_anonymous_request(self) -> None:
        assert "account_id" not in add_account_id(None, "info", {})

    def test_the_account_id_is_the_only_identifier_a_record_carries(self) -> None:
        # It is keyring's opaque identifier, not an email address -- this service never
        # learns one -- which is what makes "what did this person do" answerable without
        # a log record ever naming a human being.
        with bind_account_id("account-a"):
            record = add_account_id(None, "info", {"event": "entry_written"})

        assert record == {"event": "entry_written", "account_id": "account-a"}


class TestConfiguration:
    @pytest.fixture(autouse=True)
    def _restore_the_global_configuration(self) -> Iterator[None]:
        """Hand structlog's process-wide configuration back after each test in this class.

        A test that left a renderer installed would change what every later test in the
        session sees, which is a bad way to find out that two files disagree.
        """
        try:
            yield
        finally:
            structlog.reset_defaults()

    @pytest.mark.parametrize("log_format", list(LogFormat))
    def test_every_format_configures_without_error(self, log_format: LogFormat) -> None:
        configure_logging(level="INFO", log_format=log_format)

        assert structlog.is_configured() is True

    def test_a_logger_carries_the_name_it_was_asked_for(self) -> None:
        configure_logging(level="INFO", log_format=LogFormat.JSON)

        bound = get_logger("user_api.test")._context

        assert bound["logger"] == "user_api.test"

    def test_a_record_below_the_configured_level_is_not_emitted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="WARNING", log_format=LogFormat.JSON)

        get_logger("user_api.test").info("entry_written")

        assert capsys.readouterr().out == ""

    def test_the_configured_pipeline_redacts_what_it_actually_emits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The processor works; this says it is installed in the pipeline we log through.

        The unit tests above would all still pass if ``redact_secrets`` were dropped from
        the processor chain, which is exactly the edit somebody makes while debugging.
        """
        configure_logging(level="INFO", log_format=LogFormat.JSON)

        with bind_request_id("req-1"), bind_account_id("account-a"):
            get_logger("user_api.test").info("entry_written", key="therapist", body=SENTINEL)

        emitted = capsys.readouterr().out
        record = json.loads(emitted)
        assert SENTINEL not in emitted
        assert record["body"] == REDACTED
        assert record["key"] == "therapist"
        assert record["request_id"] == "req-1"
        assert record["account_id"] == "account-a"

    def test_the_console_renderer_redacts_the_same_way(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The local format is the one a developer reads over a shoulder, so it is the one
        # where a leaked value is most likely to be seen by somebody it is not for.
        configure_logging(level="INFO", log_format=LogFormat.CONSOLE)

        get_logger("user_api.test").info("entry_written", value_json=SENTINEL)

        emitted = capsys.readouterr().out
        assert SENTINEL not in emitted
        assert REDACTED in emitted
