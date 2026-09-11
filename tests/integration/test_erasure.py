"""Erasure end to end, over HTTP, in every mode the account can choose.

The sweeper is driven by moving the fake clock and awaiting ``sweep_once`` directly rather
than by waiting for the interval, which is the only reason a thirty-day grace period is
testable at all.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from tests.conftest import ACCOUNT, auth, container_of, set_field, write_note
from tests.conftest import token as _token

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient

    from tests.fakes.clock import FakeClock

DECADE_SECONDS = 10 * 365 * 24 * 3600
"""How long the tokens in this file live.

Worth a note, because it surprised this file once. The clock that ages a grace period is
the same clock the token verifier checks ``exp`` against, so a test that advances thirty-one
days to provoke a sweep also expires an ordinary fifteen-minute token and gets a 401 where
it expected a page. That is the service behaving correctly in both respects; it just means
a test about erasure has to hold a token that outlives the window it is testing.
"""

SENTINEL = "ZORBLAX their diagnosis is nobody elses business 7741"
"""Distinctive enough to grep for, and shaped like prose rather than like a token.

Spaces matter here: the first version of this string was hyphenated, which made it a
fifty-two character space-free mixed-case run -- and the credential detector refused every
write in this file, correctly.
"""
GRACE_DAYS = 30


def token(account_id: str = ACCOUNT, *, scope: str | None = None) -> str:
    """A token that outlives any window this file advances the clock past."""
    return _token(account_id, scope=scope, ttl_seconds=DECADE_SECONDS)


async def set_mode(client: AsyncClient, mode: str, **rest: object) -> None:
    response = await client.put(
        "/v1/user/settings", json={"erasure_mode": mode, **rest}, headers=auth(token())
    )
    assert response.status_code == 200, response.text


async def sweep(app: FastAPI) -> int:
    return await container_of(app).erasure.sweep_once()


def scan(app: FastAPI) -> tuple[bool, bool]:
    """Whether the sentinel is in the database file, and in its write-ahead log."""
    path = container_of(app).database.path
    wal = path.with_name(path.name + "-wal")
    needle = SENTINEL.encode()
    return needle in path.read_bytes(), wal.exists() and needle in wal.read_bytes()


class TestGrace:
    async def test_a_forgotten_entry_is_invisible_immediately(self, client: AsyncClient) -> None:
        written = await write_note(client, token(), body="Something to forget.")

        await client.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        assert (
            await client.get(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))
        ).status_code == 404

    async def test_it_is_still_reachable_with_include_forgotten(self, client: AsyncClient) -> None:
        # How somebody reviews what they asked to be forgotten, and undoes it if they were
        # wrong. It has to keep working, which is why the search index row survives the
        # forget and only goes at the purge.
        written = await write_note(client, token(), body="Something to forget.")
        await client.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        response = await client.get(
            "/v1/user/entries", params={"include_forgotten": True}, headers=auth(token())
        )

        found = {entry["entry_id"]: entry for entry in response.json()["entries"]}
        assert written["entry_id"] in found
        assert found[written["entry_id"]]["forgotten_at"] is not None

    async def test_it_survives_a_clock_advance_short_of_the_window(
        self, client: AsyncClient, app: FastAPI, clock: FakeClock
    ) -> None:
        written = await write_note(client, token(), body=SENTINEL)
        await client.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        clock.advance(timedelta(days=GRACE_DAYS - 1))

        assert await sweep(app) == 0

    async def test_it_is_destroyed_once_the_window_has_passed(
        self, client: AsyncClient, app: FastAPI, clock: FakeClock
    ) -> None:
        written = await write_note(client, token(), body=SENTINEL)
        await client.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        clock.advance(timedelta(days=GRACE_DAYS + 1))

        assert await sweep(app) == 1
        response = await client.get(
            "/v1/user/entries", params={"include_forgotten": True}, headers=auth(token())
        )
        assert response.json()["entries"] == []


class TestImmediate:
    async def test_it_is_gone_before_the_response_returns(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        await set_mode(client, "immediate")
        written = await write_note(client, token(), body=SENTINEL)

        await client.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        response = await client.get(
            "/v1/user/entries", params={"include_forgotten": True}, headers=auth(token())
        )
        assert response.json()["entries"] == []
        assert scan(app) == (False, False)

    async def test_the_response_still_describes_what_went(self, client: AsyncClient) -> None:
        await set_mode(client, "immediate")
        written = await write_note(client, token(), body="Something to destroy.")

        response = await client.delete(
            f"/v1/user/entries/{written['entry_id']}", headers=auth(token())
        )

        assert response.status_code == 200
        assert response.json()["forgotten_at"] is not None


class TestTombstone:
    async def test_it_is_never_destroyed_however_far_the_clock_moves(
        self, client: AsyncClient, app: FastAPI, clock: FakeClock
    ) -> None:
        await set_mode(client, "tombstone")
        written = await write_note(client, token(), body="Something to keep a record of.")
        await client.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        clock.advance(timedelta(days=GRACE_DAYS * 100))

        assert await sweep(app) == 0
        response = await client.get(
            "/v1/user/entries", params={"include_forgotten": True}, headers=auth(token())
        )
        assert len(response.json()["entries"]) == 1

    @pytest.mark.parametrize(
        "path",
        ["/v1/user/entries", "/v1/user/export", "/v1/user/schema"],
    )
    async def test_it_is_invisible_from_every_read_path(
        self, client: AsyncClient, path: str
    ) -> None:
        await set_mode(client, "tombstone")
        stored = await set_field(client, token(), "timezone", value="Europe/Lisbon")
        await client.delete(f"/v1/user/entries/{stored['entry_id']}", headers=auth(token()))

        response = await client.get(path, headers=auth(token()))

        assert stored["entry_id"] not in response.text
        assert "Europe/Lisbon" not in response.text

    async def test_it_is_absent_from_the_counts_too(self, client: AsyncClient) -> None:
        await set_mode(client, "tombstone")
        stored = await set_field(client, token(), "timezone", value="Europe/Lisbon")
        await client.delete(f"/v1/user/entries/{stored['entry_id']}", headers=auth(token()))

        counts = (await client.get("/v1/user", headers=auth(token()))).json()["counts"]

        assert counts["fields"] == 0
        assert counts["forgotten"] == 1


class TestChangingTheSettingIsNotRetroactive:
    async def test_switching_to_immediate_leaves_what_is_already_waiting(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        # A settings change that silently destroyed data would be the worst surprise this
        # service could produce.
        written = await write_note(client, token(), body=SENTINEL)
        await client.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        await set_mode(client, "immediate")

        assert await sweep(app) == 0
        response = await client.get(
            "/v1/user/entries", params={"include_forgotten": True}, headers=auth(token())
        )
        assert len(response.json()["entries"]) == 1


class TestDeletingTheWholeRecord:
    @pytest.mark.parametrize("mode", ["grace", "immediate", "tombstone"])
    async def test_it_leaves_nothing_in_any_mode(
        self, client: AsyncClient, app: FastAPI, mode: str
    ) -> None:
        # Tombstone included, which is the mode where a lesser implementation would leave
        # the tombstones behind. "Delete everything you know about me" has one meaning.
        await set_mode(client, mode)
        await set_field(client, token(), "preferred_name", value="Sam")
        doomed = await write_note(client, token(), body=SENTINEL)
        await client.delete(f"/v1/user/entries/{doomed['entry_id']}", headers=auth(token()))

        response = await client.delete("/v1/user", headers=auth(token()))

        assert response.status_code == 200
        assert response.json()["entries"] >= 1
        after = await client.get("/v1/user", headers=auth(token()))
        assert after.json()["counts"] == {
            "fields": 0,
            "notes": 0,
            "pinned": 0,
            "forgotten": 0,
            "events": 0,
        }

    async def test_the_bytes_are_gone_from_the_file_and_its_write_ahead_log(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        # The one assertion in this suite that is about the file rather than the code.
        # DELETE takes the row out of the b-tree and leaves what it held in the -wal.
        for index in range(30):
            await write_note(client, token(), body=f"an ordinary neighbour note {index}")
        await write_note(client, token(), body=SENTINEL)
        await container_of(app).database.checkpoint_truncate()
        assert scan(app)[0], "the sentinel was never written"

        await client.delete("/v1/user", headers=auth(token()))

        assert scan(app) == (False, False)

    async def test_the_record_can_be_written_to_again_afterwards(self, client: AsyncClient) -> None:
        await set_field(client, token(), "preferred_name", value="Sam")
        await client.delete("/v1/user", headers=auth(token()))

        rebuilt = await set_field(client, token(), "preferred_name", value="Sam")

        assert rebuilt["revision"] == 1


class TestTheEventLog:
    async def test_an_event_outlives_the_entry_it_describes(
        self, client: AsyncClient, app: FastAPI, clock: FakeClock
    ) -> None:
        written = await write_note(client, token(), body=SENTINEL)
        await client.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))

        clock.advance(timedelta(days=GRACE_DAYS + 1))
        await sweep(app)

        response = await client.get("/v1/user/events", headers=auth(token()))
        actions = {event["action"] for event in response.json()["events"]}
        assert "entry.forgotten" in actions

    async def test_the_values_in_those_events_are_stripped_by_the_purge(
        self, client: AsyncClient, app: FastAPI, clock: FakeClock
    ) -> None:
        await client.put("/v1/user/settings", json={"log_values": True}, headers=auth(token()))
        written = await write_note(client, token(), body=SENTINEL)
        before = await client.get("/v1/user/events", headers=auth(token()))
        assert SENTINEL in before.text

        await client.delete(f"/v1/user/entries/{written['entry_id']}", headers=auth(token()))
        clock.advance(timedelta(days=GRACE_DAYS + 1))
        await sweep(app)

        after = await client.get("/v1/user/events", headers=auth(token()))
        assert SENTINEL not in after.text
        assert after.json()["events"] != []

    async def test_values_are_kept_out_of_the_log_by_default(self, client: AsyncClient) -> None:
        # The log is a SECOND COPY of the personal data, and "changed diagnosis from X to
        # Y" is itself the sensitive fact.
        await write_note(client, token(), body=SENTINEL)

        response = await client.get("/v1/user/events", headers=auth(token()))

        assert SENTINEL not in response.text
        assert all(event["detail"] is None for event in response.json()["events"])
