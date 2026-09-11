"""No entry content reaches a log record, on any path.

keyring redacts by field NAME, and that works there because its secrets live in fields
called ``password`` and ``token``. Here the sensitive thing is the value of a field called
``body``, or ``value``, or ``description`` -- so a name-based redactor alone would let every
one of them through, and the real rule is that no call site passes content to a logger at
all. A rule about call sites is a rule somebody forgets, which is what this file is for.

The failure paths matter more than the success path. An exception handler that logs its
whole context, or a validation error that echoes the input it rejected, is the usual way
this goes wrong -- and a rejected value is routinely the most sensitive thing in a request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import structlog

from tests.conftest import auth, token
from user_api.core.logging import REDACTED

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import AsyncClient

SENTINEL = "ZORBLAX their therapist is Dr Mirembe and they see her on Tuesdays"
"""Prose rather than a token, so the credential detector does not refuse it first."""

GITHUB_TOKEN = "ghp" + "_16CharsOfTokenMaterialGoesRightHere0"
"""A GitHub token SHAPE, assembled rather than written down.

Split across the concatenation on purpose. A test that needs a credential-shaped string
is a test that puts one in the repository, and a secret scanner cannot tell the
difference -- correctly, which is why this one is joined at import instead."""


class Captured:
    """Every record the service emitted, as the dicts the renderer would have received.

    A capturing processor rather than reading stdout: it sees the record *after* the
    redactor has run and before anything renders it, which is exactly the point the rule
    has to hold at. Hand-written -- there is no mocking library in this suite.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def __call__(self, _logger: Any, _method: str, event_dict: Any) -> Any:
        self.records.append(dict(event_dict))
        raise structlog.DropEvent

    def text(self) -> str:
        return repr(self.records)


@pytest.fixture
def captured() -> Iterator[Captured]:
    """Install a capturing processor over whatever the app configured, then restore it."""
    from user_api.core.logging import add_account_id, add_request_id, redact_secrets

    sink = Captured()
    previous = structlog.get_config()
    structlog.configure(
        processors=[add_request_id, add_account_id, redact_secrets, sink],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    try:
        yield sink
    finally:
        structlog.configure(**previous)


class TestTheSentinelNeverReachesARecord:
    async def test_on_the_success_path(self, client: AsyncClient, captured: Captured) -> None:
        response = await client.post(
            "/v1/user/notes",
            json={"body": SENTINEL, "note_kind": "observation", "description": "private"},
            headers=auth(token()),
        )

        assert response.status_code == 201
        assert captured.records != []
        assert SENTINEL not in captured.text()

    async def test_when_validation_rejects_the_body(
        self, client: AsyncClient, captured: Captured
    ) -> None:
        # FastAPI's own handler includes the offending input. This asserts ours does not,
        # at the point where a log record is assembled rather than only in the response.
        response = await client.post(
            "/v1/user/notes",
            json={"body": SENTINEL, "note_kind": "not-a-kind", "description": "private"},
            headers=auth(token()),
        )

        assert response.status_code == 422
        assert SENTINEL not in captured.text()

    async def test_when_a_credential_is_refused(
        self, client: AsyncClient, captured: Captured
    ) -> None:
        response = await client.put(
            "/v1/user/fields/k",
            json={"value": f"{SENTINEL} {GITHUB_TOKEN}", "description": "x"},
            headers=auth(token()),
        )

        assert response.status_code == 422
        assert SENTINEL not in captured.text()
        assert GITHUB_TOKEN not in captured.text()

    async def test_when_the_value_is_too_large(
        self, client: AsyncClient, captured: Captured
    ) -> None:
        response = await client.put(
            "/v1/user/fields/k",
            json={"value": SENTINEL * 200, "description": "x"},
            headers=auth(token()),
        )

        assert response.status_code == 422
        assert SENTINEL not in captured.text()

    async def test_when_an_invented_field_is_rejected(
        self, client: AsyncClient, captured: Captured
    ) -> None:
        response = await client.post(
            "/v1/user/notes",
            json={
                "body": SENTINEL,
                "note_kind": "episode",
                "description": "x",
                "invented": SENTINEL,
            },
            headers=auth(token()),
        )

        assert response.status_code == 422
        assert SENTINEL not in captured.text()

    async def test_when_a_cursor_is_malformed(
        self, client: AsyncClient, captured: Captured
    ) -> None:
        response = await client.get(
            "/v1/user/entries", params={"cursor": SENTINEL}, headers=auth(token())
        )

        assert response.status_code == 422
        assert SENTINEL not in captured.text()

    async def test_when_the_entry_is_forgotten(
        self, client: AsyncClient, captured: Captured
    ) -> None:
        written = await client.post(
            "/v1/user/notes",
            json={"body": SENTINEL, "note_kind": "episode", "description": "x"},
            headers=auth(token()),
        )

        await client.delete(f"/v1/user/entries/{written.json()['entry_id']}", headers=auth(token()))

        assert SENTINEL not in captured.text()

    async def test_when_the_whole_record_is_erased(
        self, client: AsyncClient, captured: Captured
    ) -> None:
        await client.post(
            "/v1/user/notes",
            json={"body": SENTINEL, "note_kind": "episode", "description": "x"},
            headers=auth(token()),
        )

        await client.delete("/v1/user", headers=auth(token()))

        assert SENTINEL not in captured.text()


class TestWhatIsRecordedInstead:
    async def test_a_write_records_metadata_that_names_no_content(
        self, client: AsyncClient, captured: Captured
    ) -> None:
        # entry_id, key, entry_type and counts are safe and are what makes a log useful.
        await client.put(
            "/v1/user/fields/preferred_name",
            json={"value": SENTINEL, "description": "What to call them"},
            headers=auth(token()),
        )

        written = [record for record in captured.records if record.get("event") == "field_set"]
        assert written
        assert written[0]["key"] == "preferred_name"
        assert "value" not in written[0]

    async def test_a_record_carrying_a_content_name_is_redacted_rather_than_dropped(
        self, captured: Captured
    ) -> None:
        # The second line of defence. The first is that no call site passes content at
        # all; this is what catches the call site that forgets.
        from user_api.core.logging import get_logger

        get_logger("probe").info("a_call_site_that_forgot", body=SENTINEL, key="timezone")

        assert captured.records[-1]["body"] == REDACTED
        assert captured.records[-1]["key"] == "timezone"
