"""The change log, and the two properties that make it a log rather than a habit.

An event has to survive the entry it describes, and it has to be written in the same
transaction as the change it records. Both are easy to state and easy to lose, and neither
shows up as a failure anywhere else -- a log that is usually right looks exactly like a log
that is right.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.fakes.clock import EPOCH, FakeClock
from user_api.domain.entries import Action, EntryType
from user_api.events.log import Event
from user_api.events.sql_log import SqlEventLog

if TYPE_CHECKING:
    import sqlite3

    from user_api.storage.database import Database

CAP = 5


async def append(
    database: Database,
    events: SqlEventLog,
    *,
    account_id: str = ACCOUNT,
    count: int = 1,
    cap: int = 100,
    **overrides: Any,
) -> None:
    """Append ``count`` events in one transaction, as a real write path would."""

    def write(connection: sqlite3.Connection) -> None:
        for index in range(count):
            arguments: dict[str, Any] = {
                "account_id": account_id,
                "at": EPOCH,
                "action": Action.NOTE_WRITTEN,
                "asserted_by": "user",
                "cap": cap,
                "entry_id": f"entry-{index}",
                "entry_type": EntryType.NOTE,
                "key": None,
                "source": "stated",
                "detail": None,
            }
            events.append_in(connection, **{**arguments, **overrides})

    await database.transact(write)


class TestAppending:
    async def test_an_appended_event_comes_back_with_everything_it_was_given(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(
            database,
            events,
            action=Action.FIELD_SET,
            entry_type=EntryType.FIELD,
            key="preferred_name",
            detail={"value": "Sam"},
        )

        (written,) = await events.read(ACCOUNT, limit=10)

        assert written.account_id == ACCOUNT
        assert written.action is Action.FIELD_SET
        assert written.entry_type is EntryType.FIELD
        assert written.key == "preferred_name"
        assert written.asserted_by == "user"
        assert written.source == "stated"
        assert written.detail == {"value": "Sam"}
        assert written.at == EPOCH

    async def test_an_event_with_no_detail_reads_back_as_none_rather_than_empty(
        self, database: Database, events: SqlEventLog
    ) -> None:
        # None is the default and means "values were not being logged", which is a
        # different statement from "this change had no values".
        await append(database, events, detail=None)

        (written,) = await events.read(ACCOUNT, limit=10)

        assert written.detail is None

    async def test_an_event_about_no_entry_at_all_is_allowed(
        self, database: Database, events: SqlEventLog
    ) -> None:
        # settings.updated describes the account rather than an entry.
        await append(
            database,
            events,
            action=Action.SETTINGS_UPDATED,
            entry_id=None,
            entry_type=None,
        )

        (written,) = await events.read(ACCOUNT, limit=10)

        assert written.entry_id is None
        assert written.entry_type is None

    async def test_the_sequence_is_assigned_by_the_database_and_increases(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=3)

        written = await events.read(ACCOUNT, limit=10)

        assert [event.sequence for event in written] == sorted(
            (event.sequence for event in written), reverse=True
        )


class TestOrdering:
    async def test_events_come_back_newest_first(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=3)

        written = await events.read(ACCOUNT, limit=10)

        assert [event.entry_id for event in written] == ["entry-2", "entry-1", "entry-0"]

    async def test_the_order_holds_when_every_event_shares_a_timestamp(
        self, database: Database, events: SqlEventLog
    ) -> None:
        # They all do, here: the clock is injected and does not move unless a test moves
        # it. Ordered by `at` the result would be whatever SQLite felt like, and a page
        # boundary falling between two of them would drop one or repeat it -- only under
        # the fixed clock the tests use, which is the worst place to find out.
        clock = FakeClock()
        await append(database, events, count=6, at=clock.now())

        first = await events.read(ACCOUNT, limit=3)
        second = await events.read(ACCOUNT, limit=3, before_sequence=first[-1].sequence)

        assert {event.at for event in first + second} == {clock.now()}
        assert [event.entry_id for event in first] == ["entry-5", "entry-4", "entry-3"]
        assert [event.entry_id for event in second] == ["entry-2", "entry-1", "entry-0"]


class TestPaging:
    async def test_before_sequence_is_exclusive(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=4)
        page = await events.read(ACCOUNT, limit=10)

        after = await events.read(ACCOUNT, limit=10, before_sequence=page[0].sequence)

        assert page[0].sequence not in {event.sequence for event in after}

    async def test_a_walk_sees_every_event_exactly_once(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=11)

        seen: list[int] = []
        cursor: int | None = None
        while True:
            page = await events.read(ACCOUNT, limit=4, before_sequence=cursor)
            if not page:
                break
            seen.extend(event.sequence for event in page)
            cursor = page[-1].sequence

        assert len(seen) == len(set(seen)) == 11

    async def test_the_limit_is_honoured(self, database: Database, events: SqlEventLog) -> None:
        await append(database, events, count=5)

        assert len(await events.read(ACCOUNT, limit=2)) == 2


class TestTheCap:
    async def test_the_log_is_trimmed_to_the_cap_as_it_is_appended_to(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=CAP * 3, cap=CAP)

        assert await events.count_for_account(ACCOUNT) == CAP

    async def test_the_events_the_cap_keeps_are_the_newest_ones(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=8, cap=3)

        written = await events.read(ACCOUNT, limit=10)

        assert [event.entry_id for event in written] == ["entry-7", "entry-6", "entry-5"]

    async def test_a_log_under_its_cap_is_left_entirely_alone(
        self, database: Database, events: SqlEventLog
    ) -> None:
        # The trim names the rows to delete with a subselect that returns nothing when the
        # account is under the cap, so the comparison is against NULL and no row matches.
        # No count, no guard, and no branch a test cannot reach.
        await append(database, events, count=2, cap=CAP)

        assert await events.count_for_account(ACCOUNT) == 2

    async def test_one_accounts_volume_never_evicts_anothers(
        self, database: Database, events: SqlEventLog
    ) -> None:
        # The subselect repeats the account filter. Without that, a chatty account would
        # quietly delete a quiet one's entire history.
        await append(database, events, account_id=OTHER_ACCOUNT, count=2, cap=CAP)
        await append(database, events, account_id=ACCOUNT, count=CAP * 4, cap=CAP)

        assert await events.count_for_account(OTHER_ACCOUNT) == 2
        assert await events.count_for_account(ACCOUNT) == CAP

    async def test_the_cap_holds_when_appends_arrive_together(
        self, database: Database, events: SqlEventLog
    ) -> None:
        # Trimmed in the same statement as the append rather than counted first. Two
        # appends that both read the same count would both conclude they were under the
        # cap, and the log would settle above it.
        await asyncio.gather(*(append(database, events, count=4, cap=CAP) for _ in range(5)))

        assert await events.count_for_account(ACCOUNT) == CAP


class TestOutlivingTheEntry:
    async def test_purging_an_entrys_values_keeps_the_event_that_recorded_it(
        self, database: Database, events: SqlEventLog
    ) -> None:
        # The whole reason this table has no foreign keys. The record that something was
        # forgotten has to survive the thing that was forgotten, or the log answers "there
        # was never anything here" about a deletion somebody asked for.
        await append(database, events, entry_id="doomed", detail={"value": "secret"})

        await database.transact(
            lambda connection: events.purge_entry_values_in(connection, entry_id="doomed")
        )

        (survivor,) = await events.read(ACCOUNT, limit=10)
        assert survivor.entry_id == "doomed"
        assert survivor.detail is None

    async def test_purging_one_entrys_values_leaves_another_entrys_alone(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, entry_id="doomed", detail={"value": "gone"})
        await append(database, events, entry_id="spared", detail={"value": "kept"})

        await database.transact(
            lambda connection: events.purge_entry_values_in(connection, entry_id="doomed")
        )

        details = {event.entry_id: event.detail for event in await events.read(ACCOUNT, limit=10)}
        assert details == {"doomed": None, "spared": {"value": "kept"}}


class TestDeletingAnAccount:
    async def test_it_removes_every_event_and_says_how_many(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=4)

        removed = await database.transact(
            lambda connection: events.delete_for_account_in(connection, account_id=ACCOUNT)
        )

        assert removed == 4
        assert await events.count_for_account(ACCOUNT) == 0

    async def test_it_leaves_another_account_untouched(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, account_id=OTHER_ACCOUNT, count=3)
        await append(database, events, account_id=ACCOUNT, count=2)

        await database.transact(
            lambda connection: events.delete_for_account_in(connection, account_id=ACCOUNT)
        )

        assert await events.count_for_account(OTHER_ACCOUNT) == 3

    async def test_deleting_an_account_with_no_events_removes_nothing(
        self, database: Database, events: SqlEventLog
    ) -> None:
        removed = await database.transact(
            lambda connection: events.delete_for_account_in(connection, account_id=ACCOUNT)
        )

        assert removed == 0


class TestIsolation:
    async def test_one_account_never_reads_anothers_events(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, account_id=OTHER_ACCOUNT, count=3)

        assert await events.read(ACCOUNT, limit=10) == []
        assert await events.count_for_account(ACCOUNT) == 0


class TestTheEventType:
    def test_a_repr_never_renders_the_detail(self) -> None:
        # The one attribute that can hold what somebody told us, and a repr ends up in
        # test output, in a debugger, and in an exception's context when something
        # upstream logs the object it was working on.
        event = Event(
            sequence=1,
            account_id=ACCOUNT,
            at=EPOCH,
            action=Action.FIELD_SET,
            asserted_by="user",
            detail={"value": "a therapist's name"},
        )

        assert "therapist" not in repr(event)

    @pytest.mark.parametrize("action", list(Action))
    async def test_every_action_survives_the_round_trip_through_its_column(
        self, action: Action, database: Database, events: SqlEventLog
    ) -> None:
        # Parametrised over the enum itself rather than over a list somebody maintains, so
        # an action added without a column value it can survive fails here rather than at
        # the first write that uses it.
        await append(database, events, action=action)

        (written,) = await events.read(ACCOUNT, limit=1)

        assert written.action is action
