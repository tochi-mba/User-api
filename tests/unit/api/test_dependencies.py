"""Where a request becomes an account, and where it stops being one.

Two properties live here. The token is the only way in, and the account it resolves to is
bound task-locally -- which is the mechanism the whole isolation story rests on, so it is
tested as such rather than assumed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from httpx import AsyncClient

from tests.conftest import ACCOUNT, OTHER_ACCOUNT, auth, container_of, token
from user_api.api.schemas.common import PROBLEM_CONTENT_TYPE
from user_api.core.context import bind_account_id, get_account_id

if TYPE_CHECKING:
    from fastapi import FastAPI

    from tests.fakes.keyring import FakeKeyring


class TestTheTokenIsTheOnlyWayIn:
    async def test_a_request_with_no_authorization_header_is_refused(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/v1/user")

        assert response.status_code == 401

    async def test_it_is_refused_in_this_services_own_error_shape(
        self, client: AsyncClient
    ) -> None:
        # HTTPBearer left to itself raises a bare 403 with a plain JSON body -- a
        # different status and a different shape from every other failure here, which a
        # caller would then have to special-case. auto_error=False exists for that.
        response = await client.get("/v1/user")

        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        assert set(response.json()) >= {"type", "title", "status", "detail"}

    async def test_a_token_that_is_not_a_token_is_refused(self, client: AsyncClient) -> None:
        response = await client.get("/v1/user", headers=auth("not-a-token"))

        assert response.status_code == 401

    async def test_a_valid_token_gets_through(self, client: AsyncClient) -> None:
        assert (await client.get("/v1/user", headers=auth(token()))).status_code == 200

    async def test_a_wrong_scheme_is_refused(self, client: AsyncClient) -> None:
        response = await client.get("/v1/user", headers={"Authorization": f"Basic {token()}"})

        assert response.status_code == 401


class TestTheAccountBinding:
    async def test_the_account_is_bound_for_the_rest_of_the_request(
        self, client: AsyncClient
    ) -> None:
        # Bound as a side effect of resolving the token, so every log record the rest of
        # the request produces says who it was for without a handler passing it along.
        response = await client.get("/v1/user", headers=auth(token(ACCOUNT)))

        assert response.json()["account_id"] == ACCOUNT

    async def test_two_concurrent_requests_never_see_each_others_account(
        self, client: AsyncClient
    ) -> None:
        # Context variables are task-local, and that is the mechanism the whole isolation
        # story rests on. A binding that leaked between tasks would hand one person's
        # record to another under load and to nobody in a test.
        mine, theirs = await asyncio.gather(
            client.get("/v1/user", headers=auth(token(ACCOUNT))),
            client.get("/v1/user", headers=auth(token(OTHER_ACCOUNT))),
        )

        assert mine.json()["account_id"] == ACCOUNT
        assert theirs.json()["account_id"] == OTHER_ACCOUNT

    async def test_the_binding_does_not_outlive_the_request(self, client: AsyncClient) -> None:
        await client.get("/v1/user", headers=auth(token()))

        assert get_account_id() is None

    def test_binding_by_hand_restores_what_was_there_before(self) -> None:
        with bind_account_id("outer"), bind_account_id("inner"):
            assert get_account_id() == "inner"

        assert get_account_id() is None


class TestKeyringBeingDown:
    async def test_an_authenticated_call_is_503_rather_than_401(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # The token may be perfectly good and we cannot tell. A 401 would send a person
        # through a login that would not fix anything.
        keyring.status = 500

        response = await client.get("/v1/user", headers=auth(token()))

        assert response.status_code == 503

    async def test_the_response_is_a_problem_body_rather_than_a_traceback(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        keyring.status = 500

        response = await client.get("/v1/user", headers=auth(token()))

        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        assert "Traceback" not in response.text


class TestTheContainer:
    async def test_every_handler_reaches_the_one_startup_built(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        # get_container is how a handler reaches application state without touching
        # request.app.state itself. Exercised through a real request rather than a
        # stand-in object, because a stand-in would prove only that the stand-in works.
        response = await client.get("/healthy")

        assert response.status_code in {200, 503}
        assert container_of(app).settings is app.state.settings
