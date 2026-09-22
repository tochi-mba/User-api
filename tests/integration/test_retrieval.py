"""The half the service exists for: getting things back out.

Every filter alone and several combined, the two orderings, cursor pagination walked to
the end and walked across a concurrent write, and the hostile search table -- every string
in which must come back 200 or 422 and never 500.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from tests.conftest import ACCOUNT, auth, set_field, write_note
from tests.conftest import token as _token

if TYPE_CHECKING:
    from httpx import AsyncClient

    from tests.fakes.clock import FakeClock

DECADE_SECONDS = 10 * 365 * 24 * 3600

HOSTILE = [
    '"',
    'a"b',
    "x AND OR y",
    "*",
    "NEAR(",
    "^",
    "-",
    "()",
    "a:b",
    "AND",
    '"unclosed',
    "col:*",
    "   ",
    "***",
    "a OR",
    "(((",
    "NOT",
    "^abc$",
]
"""Eight of these raise OperationalError if handed to FTS5's MATCH unchanged.

They are here rather than only in the domain tests because this is the path a person's
typing actually travels, and a 500 on a read endpoint is the failure being prevented.
"""


def token(account_id: str = ACCOUNT, *, scope: str | None = None) -> str:
    return _token(account_id, scope=scope, ttl_seconds=DECADE_SECONDS)


@pytest.fixture
async def furnished(client: AsyncClient, clock: FakeClock) -> dict[str, Any]:
    """A record with enough variety in it that every filter has something to exclude."""
    mine = token()
    await set_field(
        client,
        mine,
        "preferred_name",
        value="Sam",
        description="What to call them",
        pinned=True,
        source="stated",
    )
    clock.advance(timedelta(minutes=1))
    await set_field(
        client,
        mine,
        "contact_email",
        value="sam@example.test",
        description="Where to reach them for work",
        source="imported",
    )
    clock.advance(timedelta(minutes=1))
    await set_field(
        client,
        mine,
        "contacts",
        value=3,
        description="How many people they listed",
    )
    clock.advance(timedelta(minutes=1))
    await write_note(
        client,
        mine,
        body="They mentioned preferring tea to coffee.",
        note_kind="observation",
        description="A drink preference",
        source="inferred",
    )
    clock.advance(timedelta(minutes=1))
    await write_note(
        client,
        mine,
        body="We went to Lisbon in March and it rained.",
        note_kind="episode",
        description="A trip",
        source="stated",
        sensitivity="sensitive",
    )
    clock.advance(timedelta(minutes=1))
    await write_note(
        client,
        mine,
        body="Do not summarise before they have read something.",
        note_kind="lesson",
        description="How to present things",
        source="stated",
        pinned=True,
    )
    return {"token": mine}


async def find(client: AsyncClient, furnished: dict[str, Any], **params: Any) -> dict[str, Any]:
    response = await client.get("/v1/user/entries", params=params, headers=auth(furnished["token"]))
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


class TestFullTextSearch:
    async def test_a_word_in_a_note_body_finds_it(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        page = await find(client, furnished, q="Lisbon")

        assert page["count"] == 1
        assert "Lisbon" in page["entries"][0]["body"]

    async def test_stemming_finds_a_different_form_of_the_word(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        # porter unicode61: the note says "preferring" and the search says "prefer".
        # This is most of what makes recall work on prose typed months ago.
        page = await find(client, furnished, q="prefer")

        assert any("preferring" in entry["body"] for entry in page["entries"] if entry["body"])

    async def test_a_field_is_found_by_its_description_as_well_as_its_value(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        # The word somebody searches for is at least as often in the name of the thing as
        # in the thing.
        page = await find(client, furnished, q="reach")

        assert {entry["key"] for entry in page["entries"]} == {"contact_email"}

    async def test_a_field_is_found_by_its_key(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        page = await find(client, furnished, q="preferred name")

        assert "preferred_name" in {entry["key"] for entry in page["entries"]}

    async def test_results_are_ranked_rather_than_merely_filtered(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        page = await find(client, furnished, q="tea coffee preferring")

        ranks = [entry["rank"] for entry in page["entries"]]
        assert all(rank is not None for rank in ranks)
        assert ranks == sorted(ranks)

    @pytest.mark.parametrize("query", HOSTILE)
    async def test_a_hostile_query_is_never_a_500(
        self, client: AsyncClient, furnished: dict[str, Any], query: str
    ) -> None:
        response = await client.get(
            "/v1/user/entries", params={"q": query}, headers=auth(furnished["token"])
        )

        assert response.status_code in {200, 422}

    async def test_a_query_with_nothing_matchable_says_so(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        response = await client.get(
            "/v1/user/entries", params={"q": "()"}, headers=auth(furnished["token"])
        )

        assert response.status_code == 422
        assert "letter or digit" in response.json()["detail"]


class TestFilters:
    async def test_by_type(self, client: AsyncClient, furnished: dict[str, Any]) -> None:
        fields = await find(client, furnished, type="field")
        notes = await find(client, furnished, type="note")

        assert {entry["entry_type"] for entry in fields["entries"]} == {"field"}
        assert {entry["entry_type"] for entry in notes["entries"]} == {"note"}

    async def test_by_a_batch_of_keys(self, client: AsyncClient, furnished: dict[str, Any]) -> None:
        page = await find(client, furnished, keys="preferred_name,contacts")

        assert {entry["key"] for entry in page["entries"]} == {"preferred_name", "contacts"}

    async def test_a_batch_of_keys_is_normalised(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        page = await find(client, furnished, keys="Preferred Name")

        assert {entry["key"] for entry in page["entries"]} == {"preferred_name"}

    async def test_by_key_prefix(self, client: AsyncClient, furnished: dict[str, Any]) -> None:
        page = await find(client, furnished, key_prefix="contact_")

        assert {entry["key"] for entry in page["entries"]} == {"contact_email"}

    async def test_a_key_prefix_does_not_treat_its_underscore_as_a_wildcard(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        # Unescaped, LIKE reads _ as "any character", so "contact_" would also match
        # "contacts" -- and a prefix containing % would match everything.
        narrow = await find(client, furnished, key_prefix="contact_")
        broad = await find(client, furnished, key_prefix="contact")

        assert len(narrow["entries"]) == 1
        assert len(broad["entries"]) == 2

    async def test_by_note_kind(self, client: AsyncClient, furnished: dict[str, Any]) -> None:
        page = await find(client, furnished, note_kind="lesson")

        assert {entry["note_kind"] for entry in page["entries"]} == {"lesson"}

    async def test_by_source(self, client: AsyncClient, furnished: dict[str, Any]) -> None:
        # "Show me only what I actually told you" is the query that makes this field worth
        # having.
        page = await find(client, furnished, source="inferred")

        assert {entry["source"] for entry in page["entries"]} == {"inferred"}

    async def test_by_asserted_by(self, client: AsyncClient, furnished: dict[str, Any]) -> None:
        page = await find(client, furnished, asserted_by="user")

        assert page["count"] == 6

    async def test_by_sensitivity(self, client: AsyncClient, furnished: dict[str, Any]) -> None:
        page = await find(client, furnished, sensitivity="sensitive")

        assert {entry["sensitivity"] for entry in page["entries"]} == {"sensitive"}

    async def test_by_pinned(self, client: AsyncClient, furnished: dict[str, Any]) -> None:
        pinned = await find(client, furnished, pinned=True)
        unpinned = await find(client, furnished, pinned=False)

        assert all(entry["pinned"] for entry in pinned["entries"])
        # pinned=false means "only the unpinned ones", which is a filter, not an absence
        # of one.
        assert all(not entry["pinned"] for entry in unpinned["entries"])
        assert pinned["count"] + unpinned["count"] == 6

    async def test_by_since_and_until(
        self, client: AsyncClient, furnished: dict[str, Any], clock: FakeClock
    ) -> None:
        everything = await find(client, furnished, limit=100)
        stamps = sorted(entry["updated_at"] for entry in everything["entries"])

        since = await find(client, furnished, since=stamps[3], limit=100)
        until = await find(client, furnished, until=stamps[3], limit=100)

        assert since["count"] == 3
        assert until["count"] == 3

    async def test_by_stale_before_including_what_was_never_confirmed(
        self, client: AsyncClient, furnished: dict[str, Any], clock: FakeClock
    ) -> None:
        # Never confirmed is the stalest thing there is. A plain "confirmed_at < ?" would
        # silently exclude exactly the entries this filter exists to surface.
        everything = await find(client, furnished, limit=100)
        one = everything["entries"][0]["entry_id"]
        await client.post(f"/v1/user/entries/{one}/confirm", headers=auth(furnished["token"]))
        clock.advance(timedelta(days=365))

        page = await find(client, furnished, stale_before=clock.now().isoformat(), limit=100)

        assert page["count"] == 6
        assert any(entry["confirmed_at"] is None for entry in page["entries"])

    async def test_several_filters_combine(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        page = await find(client, furnished, type="note", source="stated", sensitivity="normal")

        assert page["count"] == 1
        assert page["entries"][0]["note_kind"] == "lesson"

    async def test_a_query_combines_with_a_filter(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        page = await find(client, furnished, q="Lisbon tea", type="note", note_kind="episode")

        assert page["count"] == 1
        assert "Lisbon" in page["entries"][0]["body"]


class TestOrdering:
    async def test_recent_is_newest_first(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        page = await find(client, furnished, order="recent", limit=100)

        stamps = [entry["updated_at"] for entry in page["entries"]]
        assert stamps == sorted(stamps, reverse=True)

    async def test_oldest_is_oldest_first(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        page = await find(client, furnished, order="oldest", limit=100)

        stamps = [entry["created_at"] for entry in page["entries"]]
        assert stamps == sorted(stamps)

    async def test_relevance_without_a_query_is_refused(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        response = await client.get(
            "/v1/user/entries",
            params={"order": "relevance"},
            headers=auth(furnished["token"]),
        )

        assert response.status_code == 422
        assert "needs a q" in response.text


class TestPagination:
    async def test_a_walk_sees_every_entry_exactly_once(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        seen = await _walk(client, furnished, limit=2, order="oldest")

        assert len(seen) == len(set(seen)) == 6

    async def test_the_last_page_reports_no_next_cursor_even_when_it_is_full(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        # Which is why a caller loops until next_cursor is null rather than comparing
        # count to its limit -- that gets the last page wrong whenever it happens to fill.
        page = await find(client, furnished, limit=6)

        assert page["count"] == 6
        assert page["next_cursor"] is None

    async def test_a_concurrent_write_makes_the_walk_neither_repeat_nor_skip(
        self, client: AsyncClient, furnished: dict[str, Any], clock: FakeClock
    ) -> None:
        # On the stable ordering. created_at never moves after the insert, so a row cannot
        # shift position while the walk is in progress.
        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": 2, "order": "oldest"}
            if cursor is not None:
                params["cursor"] = cursor
            page = await find(client, furnished, **params)
            seen.extend(entry["entry_id"] for entry in page["entries"])
            pages += 1
            if pages == 2:
                clock.advance(timedelta(minutes=1))
                await write_note(client, furnished["token"], body="written mid-walk")
            cursor = page["next_cursor"]
            if cursor is None:
                break

        assert len(seen) == len(set(seen))
        assert len(seen) >= 6

    async def test_a_cursor_from_another_ordering_is_refused(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        # Replaying one against a different order would compare a bm25 float to an ISO
        # timestamp, which SQLite does happily and meaninglessly.
        page = await find(client, furnished, order="oldest", limit=2)

        response = await client.get(
            "/v1/user/entries",
            params={"order": "recent", "cursor": page["next_cursor"]},
            headers=auth(furnished["token"]),
        )

        assert response.status_code == 422

    async def test_a_cursor_nobody_issued_is_refused(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        response = await client.get(
            "/v1/user/entries",
            params={"cursor": "not-a-cursor"},
            headers=auth(furnished["token"]),
        )

        assert response.status_code == 422

    async def test_a_limit_beyond_the_maximum_is_refused_by_validation(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        response = await client.get(
            "/v1/user/entries", params={"limit": 10_000}, headers=auth(furnished["token"])
        )

        assert response.status_code == 422


class TestTheAlwaysLoadBlock:
    async def test_it_carries_counts_and_the_pinned_entries(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        response = await client.get("/v1/user", headers=auth(furnished["token"]))

        body = response.json()
        assert body["counts"] == {"fields": 3, "notes": 3, "pinned": 2, "forgotten": 0, "events": 6}
        assert len(body["pinned"]) == 2

    async def test_it_says_which_scope_the_token_grants(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        plain = await client.get("/v1/user", headers=auth(token()))
        scoped = await client.get("/v1/user", headers=auth(token(scope="health")))

        assert plain.json()["granted_scope"] is None
        assert scoped.json()["granted_scope"] == "health"


class TestTheSchemaEndpoint:
    async def test_it_returns_no_values_at_all(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        # What keeps it cheap enough to call before inventing a key, which is the whole
        # reason it exists.
        response = await client.get("/v1/user/schema", headers=auth(furnished["token"]))

        assert "sam@example.test" not in response.text
        assert all("value" not in key for key in response.json()["keys"])

    async def test_it_lists_the_well_known_keys_whether_set_or_not(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        response = await client.get("/v1/user/schema", headers=auth(furnished["token"]))

        listed = {key["key"]: key for key in response.json()["keys"]}
        assert listed["pronouns"]["set"] is False
        assert listed["pronouns"]["well_known"] is True
        assert listed["preferred_name"]["set"] is True


class TestExport:
    async def test_it_walks_to_completion(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(
                "/v1/user/export", params=params, headers=auth(furnished["token"])
            )
            body = response.json()
            seen.extend(entry["entry_id"] for entry in body["entries"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        assert len(seen) == len(set(seen)) == 6

    async def test_it_is_ordered_oldest_first(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        response = await client.get(
            "/v1/user/export", params={"limit": 100}, headers=auth(furnished["token"])
        )

        stamps = [entry["created_at"] for entry in response.json()["entries"]]
        assert stamps == sorted(stamps)


class TestStaleness:
    async def test_confirming_moves_confirmed_at_and_nothing_else(
        self, client: AsyncClient, furnished: dict[str, Any], clock: FakeClock
    ) -> None:
        stored = await set_field(client, furnished["token"], "timezone", value="Europe/Lisbon")
        clock.advance(timedelta(days=1))

        confirmed = await client.post(
            f"/v1/user/entries/{stored['entry_id']}/confirm", headers=auth(furnished["token"])
        )

        body = confirmed.json()
        assert body["confirmed_at"] is not None
        assert body["updated_at"] == stored["updated_at"]
        assert body["revision"] == stored["revision"]
        assert body["value"] == stored["value"]

    async def test_a_revision_does_not_move_confirmed_at(
        self, client: AsyncClient, furnished: dict[str, Any], clock: FakeClock
    ) -> None:
        stored = await set_field(client, furnished["token"], "timezone", value="Europe/Lisbon")
        confirmed = await client.post(
            f"/v1/user/entries/{stored['entry_id']}/confirm", headers=auth(furnished["token"])
        )
        clock.advance(timedelta(days=1))

        revised = await client.patch(
            f"/v1/user/entries/{stored['entry_id']}",
            json={"description": "Their timezone, corrected"},
            headers=auth(furnished["token"]),
        )

        assert revised.json()["confirmed_at"] == confirmed.json()["confirmed_at"]

    async def test_both_timestamps_come_back_on_every_read(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        page = await find(client, furnished, limit=100)

        assert all({"updated_at", "confirmed_at"} <= set(entry) for entry in page["entries"])


class TestTheEventLogEndpoint:
    async def test_it_pages_by_sequence(
        self, client: AsyncClient, furnished: dict[str, Any]
    ) -> None:
        first = await client.get(
            "/v1/user/events", params={"limit": 2}, headers=auth(furnished["token"])
        )
        second = await client.get(
            "/v1/user/events",
            params={"limit": 2, "before": first.json()["next_before"]},
            headers=auth(furnished["token"]),
        )

        seen = {event["sequence"] for event in first.json()["events"]}
        assert seen & {event["sequence"] for event in second.json()["events"]} == set()

    async def test_an_empty_log_reports_no_next_page(self, client: AsyncClient) -> None:
        response = await client.get("/v1/user/events", headers=auth(token()))

        assert response.json() == {"events": [], "count": 0, "next_before": None}


async def _walk(client: AsyncClient, furnished: dict[str, Any], **params: Any) -> list[str]:
    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = await find(client, furnished, **({**params, "cursor": cursor} if cursor else params))
        seen.extend(entry["entry_id"] for entry in page["entries"])
        cursor = page["next_cursor"]
        if cursor is None:
            return seen
