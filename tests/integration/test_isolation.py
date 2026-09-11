"""One test per verb, proving account B cannot reach account A.

The point of these is not that the checks are present. It is that a caller who tries gets
**exactly what they would get for data that never existed** -- a 404 or an empty page,
never a 403. A 403 confirms the entry exists, which across accounts leaks that somebody
else has one.

The same shape runs again within one account, across scopes, because a ``user.home``
assistant and a ``user.health`` assistant are as separate from each other as two people are
for everything except who may change the settings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT, auth, set_field, token, write_note

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient


@pytest.fixture
async def theirs(client: AsyncClient) -> dict[str, str]:
    """One field and one note belonging to account A, for account B to fail to reach."""
    mine = token(ACCOUNT)
    field = await set_field(client, mine, "preferred_name", value="Sam")
    note = await write_note(client, mine, body="They mentioned a trip to Lisbon.")
    return {"field": field["entry_id"], "note": note["entry_id"], "key": field["key"]}


class TestAnotherAccountCannotRead:
    async def test_the_record(self, client: AsyncClient, theirs: dict[str, str]) -> None:
        response = await client.get("/v1/user", headers=auth(token(OTHER_ACCOUNT)))

        assert response.status_code == 200
        assert response.json()["counts"] == {
            "fields": 0,
            "notes": 0,
            "pinned": 0,
            "forgotten": 0,
            "events": 0,
        }

    async def test_an_entry_by_id(self, client: AsyncClient, theirs: dict[str, str]) -> None:
        # Handed the id directly, which is the strongest form of the test: guessing it is
        # not the obstacle, and the answer is the same as for an id nobody ever issued.
        response = await client.get(
            f"/v1/user/entries/{theirs['field']}", headers=auth(token(OTHER_ACCOUNT))
        )
        invented = await client.get(
            "/v1/user/entries/an-id-nobody-issued", headers=auth(token(OTHER_ACCOUNT))
        )

        assert response.status_code == invented.status_code == 404
        assert response.json()["detail"] == invented.json()["detail"]

    async def test_a_field_by_key(self, client: AsyncClient, theirs: dict[str, str]) -> None:
        response = await client.get(
            f"/v1/user/fields/{theirs['key']}", headers=auth(token(OTHER_ACCOUNT))
        )

        assert response.status_code == 404

    async def test_the_schema(self, client: AsyncClient, theirs: dict[str, str]) -> None:
        response = await client.get("/v1/user/schema", headers=auth(token(OTHER_ACCOUNT)))

        assert all(not key["set"] for key in response.json()["keys"])

    async def test_the_event_log(self, client: AsyncClient, theirs: dict[str, str]) -> None:
        response = await client.get("/v1/user/events", headers=auth(token(OTHER_ACCOUNT)))

        assert response.json()["events"] == []

    async def test_the_export(self, client: AsyncClient, theirs: dict[str, str]) -> None:
        response = await client.get("/v1/user/export", headers=auth(token(OTHER_ACCOUNT)))

        assert response.json()["entries"] == []


class TestSearchIsolation:
    async def test_a_search_finds_only_the_searchers_own_entries(self, client: AsyncClient) -> None:
        # Its own class because the FTS index is SHARED across every account: the MATCH
        # alone finds other people's rows, and the account filter lives in the outer WHERE
        # of the join. Get that wrong and search is the one read that leaks everything.
        word = "zorblaxical"
        await write_note(client, token(ACCOUNT), body=f"Account A wrote something {word}.")
        await write_note(client, token(OTHER_ACCOUNT), body=f"Account B also said {word}.")

        mine = await client.get(
            "/v1/user/entries", params={"q": word}, headers=auth(token(ACCOUNT))
        )
        theirs = await client.get(
            "/v1/user/entries", params={"q": word}, headers=auth(token(OTHER_ACCOUNT))
        )

        assert mine.json()["count"] == 1
        assert theirs.json()["count"] == 1
        assert "Account A" in mine.json()["entries"][0]["body"]
        assert "Account B" in theirs.json()["entries"][0]["body"]

    async def test_a_search_for_another_accounts_distinctive_words_finds_nothing(
        self, client: AsyncClient, theirs: dict[str, str]
    ) -> None:
        response = await client.get(
            "/v1/user/entries", params={"q": "Lisbon"}, headers=auth(token(OTHER_ACCOUNT))
        )

        assert response.json()["count"] == 0


class TestAnotherAccountCannotWrite:
    async def test_revising(self, client: AsyncClient, theirs: dict[str, str]) -> None:
        response = await client.patch(
            f"/v1/user/entries/{theirs['field']}",
            json={"value": "Vandalised"},
            headers=auth(token(OTHER_ACCOUNT)),
        )

        assert response.status_code == 404

    async def test_confirming(self, client: AsyncClient, theirs: dict[str, str]) -> None:
        response = await client.post(
            f"/v1/user/entries/{theirs['field']}/confirm", headers=auth(token(OTHER_ACCOUNT))
        )

        assert response.status_code == 404

    async def test_forgetting(self, client: AsyncClient, theirs: dict[str, str]) -> None:
        response = await client.delete(
            f"/v1/user/entries/{theirs['field']}", headers=auth(token(OTHER_ACCOUNT))
        )

        assert response.status_code == 404

    async def test_setting_a_field_writes_its_own_rather_than_overwriting(
        self, client: AsyncClient, theirs: dict[str, str]
    ) -> None:
        # The same key in two accounts is two fields. The unique index is on
        # (account_id, key), so there is no shared namespace to collide in.
        await set_field(client, token(OTHER_ACCOUNT), "preferred_name", value="Someone Else")

        mine = await client.get("/v1/user/fields/preferred_name", headers=auth(token(ACCOUNT)))

        assert mine.json()["value"] == "Sam"

    async def test_deleting_the_record_leaves_the_other_account_intact(
        self, client: AsyncClient, theirs: dict[str, str]
    ) -> None:
        await set_field(client, token(OTHER_ACCOUNT), "timezone", value="Europe/Madrid")

        await client.delete("/v1/user", headers=auth(token(OTHER_ACCOUNT)))

        mine = await client.get("/v1/user", headers=auth(token(ACCOUNT)))
        assert mine.json()["counts"]["fields"] == 1


class TestScopesIsolateWithinOneAccount:
    @pytest.fixture
    async def scoped(self, client: AsyncClient) -> str:
        stored = await set_field(
            client,
            token(ACCOUNT, scope="health"),
            "blood_type",
            value="O-",
            description="Blood type",
            scopes=["health"],
        )
        entry_id: str = stored["entry_id"]
        return entry_id

    async def test_a_home_token_cannot_read_a_health_entry(
        self, client: AsyncClient, scoped: str
    ) -> None:
        response = await client.get(
            f"/v1/user/entries/{scoped}", headers=auth(token(ACCOUNT, scope="home"))
        )

        assert response.status_code == 404

    async def test_a_home_token_cannot_read_it_by_key_either(
        self, client: AsyncClient, scoped: str
    ) -> None:
        response = await client.get(
            "/v1/user/fields/blood_type", headers=auth(token(ACCOUNT, scope="home"))
        )

        assert response.status_code == 404

    async def test_a_home_token_cannot_find_it_by_searching(
        self, client: AsyncClient, scoped: str
    ) -> None:
        response = await client.get(
            "/v1/user/entries", params={"q": "blood"}, headers=auth(token(ACCOUNT, scope="home"))
        )

        assert response.json()["count"] == 0

    async def test_a_home_token_cannot_revise_confirm_or_forget_it(
        self, client: AsyncClient, scoped: str
    ) -> None:
        home = auth(token(ACCOUNT, scope="home"))

        revised = await client.patch(
            f"/v1/user/entries/{scoped}", json={"value": "A+"}, headers=home
        )
        confirmed = await client.post(f"/v1/user/entries/{scoped}/confirm", headers=home)
        forgotten = await client.delete(f"/v1/user/entries/{scoped}", headers=home)

        assert {revised.status_code, confirmed.status_code, forgotten.status_code} == {404}

    async def test_it_is_absent_from_the_counts_a_home_token_sees(
        self, client: AsyncClient, scoped: str
    ) -> None:
        response = await client.get("/v1/user", headers=auth(token(ACCOUNT, scope="home")))

        assert response.json()["counts"]["fields"] == 0

    async def test_the_health_token_can_of_course(self, client: AsyncClient, scoped: str) -> None:
        response = await client.get(
            f"/v1/user/entries/{scoped}", headers=auth(token(ACCOUNT, scope="health"))
        )

        assert response.json()["value"] == "O-"

    async def test_a_home_token_asking_for_the_health_scope_is_refused_rather_than_empty(
        self, client: AsyncClient, scoped: str
    ) -> None:
        # Refused loudly, because a caller that asked for something it may not have and
        # got an empty page caches the emptiness and stops asking.
        response = await client.get(
            "/v1/user/entries",
            params={"scope": "health"},
            headers=auth(token(ACCOUNT, scope="home")),
        )

        assert response.status_code == 403

    async def test_a_home_token_cannot_write_a_health_scoped_entry(
        self, client: AsyncClient
    ) -> None:
        response = await client.put(
            "/v1/user/fields/something",
            json={"value": 1, "description": "x", "scopes": ["health"]},
            headers=auth(token(ACCOUNT, scope="home")),
        )

        assert response.status_code == 403

    async def test_a_home_token_cannot_clobber_a_health_key_it_cannot_read(
        self, client: AsyncClient, scoped: str
    ) -> None:
        # 409 and not a silent overwrite. What leaks is that a key is taken -- not its
        # value, not its scope -- and ADR-0004 argues that trade.
        response = await client.put(
            "/v1/user/fields/blood_type",
            json={"value": "A+", "description": "x"},
            headers=auth(token(ACCOUNT, scope="home")),
        )

        assert response.status_code == 409
        still = await client.get(
            "/v1/user/fields/blood_type", headers=auth(token(ACCOUNT, scope="health"))
        )
        assert still.json()["value"] == "O-"


class TestTheSurfaceCannotExpressACrossAccountRead:
    def test_no_route_takes_anything_that_names_a_subject(self, app: FastAPI) -> None:
        # The property the whole surface is built around. A cross-account read is not
        # forbidden here, it is UNEXPRESSIBLE: which record you are reading comes from the
        # token's subject and there is no parameter that could say otherwise.
        spec = app.openapi()
        forbidden = {"account_id", "user_id", "account", "sub", "subject", "owner"}

        names = {
            parameter["name"]
            for methods in spec["paths"].values()
            for operation in methods.values()
            for parameter in operation.get("parameters", [])
        }
        paths = set(spec["paths"])

        assert names & forbidden == set()
        assert all("{account" not in path and "{user" not in path for path in paths)
