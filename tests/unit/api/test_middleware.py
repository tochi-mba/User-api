"""The request id, and why the 500 handler lives inside the middleware rather than outside.

A caller is told to quote the request id when reporting a failure. That only works if the
failure response carries one -- and Starlette's outermost error middleware runs after this
binding has unwound, so a 500 handled out there would arrive with no id at all. That is the
whole reason for the try/except in dispatch, and it is what the last test here pins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from user_api.api.errors import register_exception_handlers
from user_api.api.middleware import (
    MAX_SUPPLIED_REQUEST_ID,
    REQUEST_ID_HEADER,
    RESPONSE_TIME_HEADER,
    RequestContextMiddleware,
)
from user_api.api.schemas.common import PROBLEM_CONTENT_TYPE
from user_api.core.context import get_request_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

SENTINEL = "ZORBLAX-7741"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/fine")
    async def _fine() -> dict[str, str | None]:
        return {"seen": get_request_id()}

    @app.get("/boom")
    async def _boom() -> None:
        # Deliberately not a domain error, so it reaches the unhandled path.
        raise RuntimeError(SENTINEL)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://middleware.test"
    ) as http:
        yield http


class TestTheRequestId:
    async def test_one_is_generated_when_the_caller_supplies_none(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/fine")

        assert response.headers[REQUEST_ID_HEADER]
        assert response.json()["seen"] == response.headers[REQUEST_ID_HEADER]

    async def test_a_supplied_one_is_honoured_so_a_trace_can_span_services(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/fine", headers={REQUEST_ID_HEADER: "from-upstream"})

        assert response.headers[REQUEST_ID_HEADER] == "from-upstream"
        assert response.json()["seen"] == "from-upstream"

    async def test_a_supplied_one_is_length_capped(self, client: AsyncClient) -> None:
        # It ends up in every log record for this request, so an unbounded one is an
        # unbounded amount of somebody else's text in our logs.
        response = await client.get("/fine", headers={REQUEST_ID_HEADER: "x" * 500})

        assert len(response.headers[REQUEST_ID_HEADER]) == MAX_SUPPLIED_REQUEST_ID

    async def test_two_requests_get_different_ids(self, client: AsyncClient) -> None:
        first = await client.get("/fine")
        second = await client.get("/fine")

        assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]

    async def test_the_binding_does_not_outlive_the_request(self, client: AsyncClient) -> None:
        await client.get("/fine")

        assert get_request_id() is None


class TestTiming:
    async def test_every_response_says_how_long_it_took(self, client: AsyncClient) -> None:
        response = await client.get("/fine")

        assert float(response.headers[RESPONSE_TIME_HEADER]) >= 0


class TestAnUnexpectedFailure:
    async def test_it_becomes_a_problem_response_rather_than_a_traceback(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/boom")

        assert response.status_code == 500
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)

    async def test_the_response_still_carries_the_request_id(self, client: AsyncClient) -> None:
        # The whole reason this is handled here rather than by Starlette's outermost error
        # middleware, which runs after the binding has unwound and would return a response
        # with no id on it -- the one thing the caller is told to quote.
        response = await client.get("/boom", headers={REQUEST_ID_HEADER: "traceable"})

        assert response.headers[REQUEST_ID_HEADER] == "traceable"
        assert response.json()["request_id"] == "traceable"

    async def test_the_exceptions_message_does_not_reach_the_caller(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/boom")

        assert SENTINEL not in response.text

    async def test_a_failure_is_still_timed(self, client: AsyncClient) -> None:
        response = await client.get("/boom")

        assert float(response.headers[RESPONSE_TIME_HEADER]) >= 0
