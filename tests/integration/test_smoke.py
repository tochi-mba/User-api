"""The one test that proves the harness works, so every other test can trust it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.conftest import auth, set_field, token

if TYPE_CHECKING:
    from httpx import AsyncClient


async def test_a_field_written_through_http_comes_back(client: AsyncClient) -> None:
    stored = await set_field(client, token(), "timezone", value="Europe/Lisbon")

    response = await client.get("/v1/user/fields/timezone", headers=auth(token()))

    assert response.status_code == 200
    assert response.json()["value"] == "Europe/Lisbon"
    assert response.json()["entry_id"] == stored["entry_id"]
