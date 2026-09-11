"""What survives stopping the service and starting it again.

Each test runs two apps in sequence over one database file, which is exactly what a restart
is: a new process, a new container, a new connection, the same file underneath.

The one that matters most is searchability. The FTS index is a second copy of the content
maintained by hand rather than by the database, so "it comes back" is a claim about a file
on disk rather than about an in-memory structure being rebuilt -- and an index that did not
survive would leave every read working and every search quietly empty.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from tests.conftest import ACCOUNT, auth, build_settings, set_field, write_note
from tests.conftest import token as _token
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import FakeKeyring
from user_api.api.app import create_app
from user_api.core.container import Container

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from user_api.core.config import Settings

DECADE_SECONDS = 10 * 365 * 24 * 3600


def token(account_id: str = ACCOUNT, *, scope: str | None = None) -> str:
    return _token(account_id, scope=scope, ttl_seconds=DECADE_SECONDS)


@pytest.fixture
def durable(tmp_path: Path) -> Settings:
    """One settings object, and therefore one database file, for both runs."""
    return build_settings(tmp_path)


@contextlib.asynccontextmanager
async def running(settings: Settings) -> AsyncIterator[AsyncClient]:
    """One run of the service, lifespan and all, over the given settings.

    Entering it twice with the same settings is a restart. The container is built here
    rather than left to the factory only so the fake clock and the fake keyring survive
    into the second run; everything else is exactly what a deployment does.
    """
    app = create_app(settings)
    container = Container.build(settings, clock=FakeClock())
    container.jwks._client = AsyncClient(transport=FakeKeyring().transport())
    app.state.prebuilt = container
    app.state.container = container

    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://user.test") as http,
    ):
        yield http


class TestEntriesSurvive:
    async def test_a_field_comes_back_with_its_value(self, durable: Settings) -> None:
        async with running(durable) as first:
            await set_field(first, token(), "preferred_name", value="Sam")

        async with running(durable) as second:
            response = await second.get("/v1/user/fields/preferred_name", headers=auth(token()))

            assert response.json()["value"] == "Sam"

    async def test_a_note_comes_back_with_its_body(self, durable: Settings) -> None:
        async with running(durable) as first:
            await write_note(first, token(), body="We went to Lisbon in March.")

        async with running(durable) as second:
            response = await second.get(
                "/v1/user/entries", params={"type": "note"}, headers=auth(token())
            )

            assert "Lisbon" in response.json()["entries"][0]["body"]

    async def test_the_counts_are_the_same(self, durable: Settings) -> None:
        async with running(durable) as first:
            await set_field(first, token(), "preferred_name", value="Sam")
            await write_note(first, token(), body="Something happened.")
            before = (await first.get("/v1/user", headers=auth(token()))).json()["counts"]

        async with running(durable) as second:
            after = (await second.get("/v1/user", headers=auth(token()))).json()["counts"]

        assert after == before

    async def test_a_revision_and_its_revision_number_survive(self, durable: Settings) -> None:
        async with running(durable) as first:
            await set_field(first, token(), "timezone", value="Europe/Lisbon")
            await set_field(first, token(), "timezone", value="Europe/Madrid")

        async with running(durable) as second:
            response = await second.get("/v1/user/fields/timezone", headers=auth(token()))

            assert response.json()["value"] == "Europe/Madrid"
            assert response.json()["revision"] == 2


class TestSearchabilitySurvives:
    async def test_a_note_written_before_the_restart_is_still_findable(
        self, durable: Settings
    ) -> None:
        # The index is a second copy maintained by hand. An index that did not come back
        # would leave every read working and every search quietly empty -- which looks
        # like "you never told me that" rather than like a broken service.
        async with running(durable) as first:
            await write_note(first, token(), body="They mentioned preferring tea to coffee.")

        async with running(durable) as second:
            response = await second.get(
                "/v1/user/entries", params={"q": "prefer"}, headers=auth(token())
            )

            assert response.json()["count"] == 1

    async def test_the_index_still_agrees_with_the_table(self, durable: Settings) -> None:
        async with running(durable) as first:
            await write_note(first, token(), body="A note.")
            await set_field(first, token(), "preferred_name", value="Sam")

        async with running(durable) as second:
            page = await second.get(
                "/v1/user/entries", params={"q": "Sam note"}, headers=auth(token())
            )
            everything = await second.get(
                "/v1/user/entries", params={"limit": 100}, headers=auth(token())
            )

            assert page.json()["count"] == everything.json()["count"] == 2


class TestScopesAndPinsSurvive:
    async def test_a_scoped_entry_is_still_scoped(self, durable: Settings) -> None:
        async with running(durable) as first:
            await set_field(
                first,
                token(scope="health"),
                "blood_type",
                value="O-",
                description="Blood type",
                scopes=["health"],
            )

        async with running(durable) as second:
            hidden = await second.get(
                "/v1/user/fields/blood_type", headers=auth(token(scope="home"))
            )
            visible = await second.get(
                "/v1/user/fields/blood_type", headers=auth(token(scope="health"))
            )

            assert hidden.status_code == 404
            assert visible.json()["value"] == "O-"

    async def test_a_pinned_entry_is_still_in_the_always_load_block(
        self, durable: Settings
    ) -> None:
        async with running(durable) as first:
            await set_field(first, token(), "preferred_name", value="Sam", pinned=True)

        async with running(durable) as second:
            response = await second.get("/v1/user", headers=auth(token()))

            assert len(response.json()["pinned"]) == 1


class TestSettingsAndEventsSurvive:
    async def test_a_settings_change_survives(self, durable: Settings) -> None:
        async with running(durable) as first:
            await first.put(
                "/v1/user/settings",
                json={"erasure_mode": "tombstone", "grace_days": 5, "log_values": True},
                headers=auth(token()),
            )

        async with running(durable) as second:
            response = await second.get("/v1/user/settings", headers=auth(token()))

            assert response.json() == {
                "erasure_mode": "tombstone",
                "grace_days": 5,
                "log_values": True,
            }

    async def test_the_event_log_survives(self, durable: Settings) -> None:
        async with running(durable) as first:
            await set_field(first, token(), "preferred_name", value="Sam")

        async with running(durable) as second:
            response = await second.get("/v1/user/events", headers=auth(token()))

            assert [event["action"] for event in response.json()["events"]] == ["field.set"]


class TestTheNegatives:
    async def test_a_forgotten_entry_stays_forgotten(self, durable: Settings) -> None:
        async with running(durable) as first:
            written = await write_note(first, token(), body="Something to forget.")
            await first.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        async with running(durable) as second:
            response = await second.get(
                f"/v1/user/entries/{written['entry_id']}", headers=auth(token())
            )

            assert response.status_code == 404

    async def test_a_purged_entry_stays_purged(self, durable: Settings) -> None:
        async with running(durable) as first:
            await first.put(
                "/v1/user/settings", json={"erasure_mode": "immediate"}, headers=auth(token())
            )
            written = await write_note(first, token(), body="Something to destroy.")
            await first.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        async with running(durable) as second:
            response = await second.get(
                "/v1/user/entries",
                params={"include_forgotten": True},
                headers=auth(token()),
            )

            assert response.json()["entries"] == []

    async def test_an_erased_record_stays_erased(self, durable: Settings) -> None:
        async with running(durable) as first:
            await set_field(first, token(), "preferred_name", value="Sam")
            await first.delete("/v1/user", headers=auth(token()))

        async with running(durable) as second:
            response = await second.get("/v1/user", headers=auth(token()))

            assert response.json()["counts"]["fields"] == 0
            assert response.json()["created_at"] is None

    async def test_another_accounts_data_is_still_another_accounts(self, durable: Settings) -> None:
        async with running(durable) as first:
            await set_field(first, token(), "preferred_name", value="Sam")

        async with running(durable) as second:
            from tests.conftest import OTHER_ACCOUNT

            response = await second.get("/v1/user", headers=auth(token(OTHER_ACCOUNT)))

            assert response.json()["counts"]["fields"] == 0


class TestTheFileItself:
    async def test_it_is_still_owner_only_after_a_restart(self, durable: Settings) -> None:
        # The mode is the only access control there is, and it is applied at connect time
        # -- so a second connection to an existing file has to apply it again rather than
        # assume the first one did.
        async with running(durable) as first:
            await set_field(first, token(), "preferred_name", value="Sam")

        async with running(durable):
            assert durable.database_path.stat().st_mode & 0o777 == 0o600
