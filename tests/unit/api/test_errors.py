"""Turning exceptions into problem responses, and the two things that must never leak.

The mapping itself is a lookup. What earns tests is the pair of decisions around it: which
status each domain error gets, because two of them are deliberately not what a reader would
guess; and what the body is allowed to contain, because on this service every request body
is somebody's personal data and the default FastAPI handler echoes it back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from user_api.api.errors import (
    _DOMAIN_STATUS,
    PROBLEM_BASE_URI,
    RETRY_AFTER_HEADER,
    _slug_for,
    problem_response,
    register_exception_handlers,
    unhandled_problem_response,
)
from user_api.api.schemas.common import PROBLEM_CONTENT_TYPE
from user_api.core.context import bind_request_id
from user_api.domain.errors import (
    AuthenticationError,
    EntryNotFoundError,
    KeyringUnreachableError,
    LimitExceededError,
    PreferencesUnavailableError,
    ScopeConflictError,
    ScopeNotGrantedError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

SENTINEL = "ZORBLAX-their-diagnosis-7741"


class Body(BaseModel):
    """A request model shaped like this service's: it forbids what it does not know."""

    model_config = ConfigDict(extra="forbid")

    value: int = Field()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A minimal app with this service's handlers and nothing else.

    Built here rather than reusing the real app, so a failure means the handler is wrong
    rather than that some route changed.
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/validated")
    async def _validated(body: Body) -> dict[str, int]:
        return {"value": body.value}

    @app.get("/raises/{name}")
    async def _raises(name: str) -> None:
        raise _BY_NAME[name]

    @app.get("/http")
    async def _http() -> None:
        raise StarletteHTTPException(status_code=status.HTTP_418_IM_A_TEAPOT, detail="no")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://errors.test") as http:
        yield http


_BY_NAME: dict[str, Exception] = {
    "auth": AuthenticationError("the token was not accepted"),
    "not-found": EntryNotFoundError("no such entry"),
    "scope": ScopeNotGrantedError("this token grants none and cannot write health"),
    "conflict": ScopeConflictError("a field named 'x' already exists outside this scope"),
    "limit": LimitExceededError("at most 40 pinned entries"),
    "keyring": KeyringUnreachableError("keyring's signing keys could not be fetched"),
    "preferences": PreferencesUnavailableError(
        "settings-api did not accept this service's request for your settings"
    ),
}


class TestTheProblemShape:
    def test_it_carries_every_field_rfc_9457_defines(self) -> None:
        response = problem_response(status_code=404, detail="no such entry")

        body = _json(response)
        assert body["type"].startswith(PROBLEM_BASE_URI)
        assert body["title"] == "Not found"
        assert body["status"] == 404
        assert body["detail"] == "no such entry"

    def test_it_is_served_as_problem_json(self) -> None:
        assert problem_response(status_code=404, detail="x").media_type == PROBLEM_CONTENT_TYPE

    def test_it_carries_the_bound_request_id(self) -> None:
        with bind_request_id("a-request-id"):
            body = _json(problem_response(status_code=500, detail="x"))

        assert body["request_id"] == "a-request-id"

    def test_the_request_id_is_absent_rather_than_null_outside_a_request(self) -> None:
        # Absent rather than present-and-null, so a client can filter on existence.
        assert "request_id" not in _json(problem_response(status_code=500, detail="x"))

    def test_a_caller_supplied_problem_type_is_used(self) -> None:
        body = _json(
            problem_response(status_code=422, detail="x", problem_type="validation-failed")
        )

        assert body["type"] == f"{PROBLEM_BASE_URI}/validation-failed"

    def test_a_status_with_no_title_of_its_own_still_produces_one(self) -> None:
        # Reached by any status this service does not map. The slug and the title both
        # fall back rather than rendering "None".
        body = _json(problem_response(status_code=418, detail="x"))

        assert body["title"] == "Error"
        assert body["type"] == f"{PROBLEM_BASE_URI}/error"
        assert _slug_for(418) == "error"

    def test_headers_are_passed_through(self) -> None:
        response = problem_response(status_code=503, detail="x", headers={RETRY_AFTER_HEADER: "5"})

        assert response.headers[RETRY_AFTER_HEADER] == "5"


class TestTheStatusMapping:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("auth", status.HTTP_401_UNAUTHORIZED),
            ("not-found", status.HTTP_404_NOT_FOUND),
            ("scope", status.HTTP_403_FORBIDDEN),
            ("conflict", status.HTTP_409_CONFLICT),
            ("limit", status.HTTP_409_CONFLICT),
            ("preferences", status.HTTP_503_SERVICE_UNAVAILABLE),
        ],
    )
    async def test_a_domain_error_becomes_its_status(
        self, client: AsyncClient, name: str, expected: int
    ) -> None:
        response = await client.get(f"/raises/{name}")

        assert response.status_code == expected
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)

    def test_every_error_in_the_map_has_a_status_this_service_uses(self) -> None:
        # Parametrised over the map itself, so an error added without a status fails here
        # rather than becoming an undiagnosed 500 at the first request that raises it.
        assert _DOMAIN_STATUS
        assert all(400 <= code < 600 for code in _DOMAIN_STATUS.values())

    async def test_an_entry_you_may_not_see_is_404_and_not_403(self, client: AsyncClient) -> None:
        # A 403 confirms the entry exists. Across accounts that leaks that somebody else
        # has one; within an account it tells a home-scoped assistant which health entries
        # are there to be found.
        assert (await client.get("/raises/not-found")).status_code == 404

    async def test_a_scope_refusal_is_403_and_says_so(self, client: AsyncClient) -> None:
        # The exception, and it is safe: this is a fact about the caller's OWN token, not
        # about what exists. A caller that cannot tell "refused" from "absent" retries
        # forever.
        response = await client.get("/raises/scope")

        assert response.status_code == 403
        assert "cannot write health" in response.json()["detail"]

    async def test_keyring_being_unreachable_is_503_with_a_retry_after(
        self, client: AsyncClient
    ) -> None:
        # Not a 401. A 401 tells a person to log in again because THIS service could not
        # fetch a public key, sending them through a login that would not fix it.
        response = await client.get("/raises/keyring")

        assert response.status_code == 503
        assert response.headers[RETRY_AFTER_HEADER] == "5"
        assert response.json()["type"].endswith("keyring-unreachable")

    async def test_settings_api_refusing_this_service_is_503_with_fixed_text(
        self, client: AsyncClient
    ) -> None:
        # Not a 4xx, and not the grant named in settings-api's own body. Serving defaults
        # would hide a missing grant behind behaviour that happened to work.
        response = await client.get("/raises/preferences")

        assert response.status_code == 503
        assert "granted" not in response.json()["detail"]
        assert "settings-api did not accept" in response.json()["detail"]

    async def test_a_starlette_http_exception_is_rendered_in_the_same_shape(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/http")

        assert response.status_code == 418
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        assert response.json()["detail"] == "no"


class TestValidationNeverEchoesTheInput:
    async def test_a_rejected_value_appears_nowhere_in_the_response(
        self, client: AsyncClient
    ) -> None:
        # FastAPI's default handler includes the offending input. On this service every
        # request body is somebody's personal data, so the default would put a rejected
        # note body into a response, a client log, and quite possibly an aggregator.
        response = await client.post("/validated", json={"value": SENTINEL})

        assert response.status_code == 422
        assert SENTINEL not in response.text

    async def test_an_invented_field_is_rejected_without_echoing_its_value(
        self, client: AsyncClient
    ) -> None:
        response = await client.post("/validated", json={"value": 1, "invented": SENTINEL})

        assert response.status_code == 422
        assert SENTINEL not in response.text

    async def test_it_still_says_where_the_problem_is(self, client: AsyncClient) -> None:
        # Location and message only, which is enough to fix the request and not enough to
        # repeat it back.
        response = await client.post("/validated", json={"value": SENTINEL})

        errors = response.json()["errors"]
        assert errors
        assert all(set(error) == {"location", "message"} for error in errors)
        assert any("value" in error["location"] for error in errors)


class TestUnexpectedFailures:
    def test_the_exceptions_own_message_is_withheld(self) -> None:
        # It can carry a filesystem path, an internal hostname, or a fragment of what
        # somebody wrote down.
        body = _json(unhandled_problem_response(ValueError(SENTINEL)))

        assert SENTINEL not in str(body)
        assert body["status"] == 500

    def test_the_caller_is_given_something_to_quote(self) -> None:
        with bind_request_id("the-request-id"):
            body = _json(unhandled_problem_response(RuntimeError("boom")))

        assert body["request_id"] == "the-request-id"
        assert "request id" in body["detail"]


def _json(response: Any) -> dict[str, Any]:
    """The rendered body of a JSONResponse, as a dict."""
    import json

    decoded: dict[str, Any] = json.loads(bytes(response.body))
    return decoded
