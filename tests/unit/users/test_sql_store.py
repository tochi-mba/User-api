"""The account's own row, and the transaction that destroys everything hanging off it.

Two shapes carry the weight here, and both are about what happens when something else is
touching the same account at the same moment.

:meth:`SqlUserStore.ensure` runs on the way into every write, so it is the most frequently
called method in the service and the one with the smallest window to get wrong. Written as
two statements -- insert if absent, then read it back -- somebody else's ``DELETE
/v1/user`` fits in the gap, and the read comes home empty for a record the method has
already promised to return. The concurrency tests below queue calls behind the database's
only worker thread so that every one of them is provably submitted and unstarted before
the first is allowed to proceed, which is the interleaving that would expose it.

:meth:`SqlUserStore.delete` is the other half. It deletes in an order, and the order is
load-bearing: ``entry_search`` rows know their entry by rowid and by nothing else, so
entries deleted first leave index rows that no statement can reach by account any more --
each one still holding the words of a note somebody asked to have destroyed.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import TYPE_CHECKING, Any

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.fakes.clock import EPOCH
from user_api.domain.entries import NoteKind, Sensitivity, Source
from user_api.domain.settings import ErasureMode
from user_api.entries.store import Journal
from user_api.users.sql_settings import SqlSettingsStore
from user_api.users.sql_store import SqlUserStore
from user_api.users.store import Erased, UserStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from datetime import datetime

    from user_api.domain.entries import Entry
    from user_api.entries.sql_store import SqlEntryStore
    from user_api.storage.database import Database

JOURNAL = Journal(cap=100)
"""Room enough that nothing in this module trips the event cap by accident."""

SCHEDULER_TURNS = 8
"""Passes through the event loop, enough for every queued call to reach the database."""

DAY = 86_400.0


@contextlib.asynccontextmanager
async def database_held(database: Database) -> AsyncIterator[None]:
    """Occupy the database's only worker thread for the duration of the block.

    Everything submitted inside the block queues behind this and cannot begin, so a test
    can line several calls up and know that none of them has read anything yet.
    """
    started = threading.Event()
    release = threading.Event()

    def block(_connection: Any) -> None:
        started.set()
        release.wait(timeout=5)

    holding = asyncio.create_task(database.run(block))
    for _ in range(1000):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set(), "the database never picked the blocking call up"

    try:
        yield
    finally:
        release.set()
        await holding


async def park_behind_the_database(calls: Sequence[asyncio.Future[Any]]) -> None:
    """Let every call start and queue behind the held database.

    Asserting that none of them finished is what makes these tests mean something: it
    proves each call really is suspended before it has read anything, rather than having
    run to completion before the next one started.
    """
    for _ in range(SCHEDULER_TURNS):
        await asyncio.sleep(0)

    assert [call.done() for call in calls] == [False] * len(calls)


async def write_field(
    entries: SqlEntryStore,
    account_id: str,
    *,
    key: str = "preferred_name",
    value: object = "Sam",
    scopes: tuple[str, ...] = (),
    now: datetime = EPOCH,
) -> Entry:
    return await entries.put_field(
        account_id=account_id,
        key=key,
        value=value,
        description="What to call them",
        source=Source.STATED,
        source_detail=None,
        asserted_by="user",
        scopes=scopes,
        sensitivity=Sensitivity.NORMAL,
        pinned=False,
        granted=scopes[0] if scopes else None,
        now=now,
        entry_cap=100,
        field_cap=100,
        pin_cap=10,
        journal=JOURNAL,
    )


async def write_note(
    entries: SqlEntryStore, account_id: str, *, body: str, now: datetime = EPOCH
) -> Entry:
    return await entries.write_note(
        account_id=account_id,
        body=body,
        note_kind=NoteKind.OBSERVATION,
        description="A preference worth remembering",
        source=Source.INFERRED,
        source_detail=None,
        asserted_by="user",
        scopes=(),
        sensitivity=Sensitivity.NORMAL,
        pinned=False,
        now=now,
        entry_cap=100,
        pin_cap=10,
        journal=JOURNAL,
    )


async def total(database: Database, sql: str, parameters: Sequence[object] = ()) -> int:
    """One ``count(*)``, for the tests that assert a table is empty."""
    return await database.count(sql, parameters)


@pytest.fixture
def store(database: Database) -> SqlUserStore:
    return SqlUserStore(database=database)


@pytest.fixture
def user_settings(database: Database) -> SqlSettingsStore:
    return SqlSettingsStore(database=database)


class TestEnsure:
    async def test_it_satisfies_the_port(self, store: SqlUserStore) -> None:
        checked: UserStore = store

        assert isinstance(checked, UserStore)

    async def test_a_first_write_creates_the_record_with_both_stamps_at_now(
        self, store: SqlUserStore
    ) -> None:
        record = await store.ensure(ACCOUNT, now=EPOCH)

        assert record.account_id == ACCOUNT
        assert record.created_at == EPOCH
        assert record.updated_at == EPOCH

    async def test_ensuring_again_keeps_the_created_at_the_account_arrived_with(
        self, store: SqlUserStore, clock: object
    ) -> None:
        # DO NOTHING rather than DO UPDATE. "Known since" is the one fact this row holds
        # that nobody can restate, so a later write must not move it -- and moving
        # updated_at on is touch()'s job, which every write does separately.
        first = await store.ensure(ACCOUNT, now=EPOCH)
        later = EPOCH.replace(year=2027)

        again = await store.ensure(ACCOUNT, now=later)

        assert again.created_at == first.created_at
        assert again.updated_at == EPOCH

    async def test_two_accounts_ensured_separately_keep_separate_records(
        self, store: SqlUserStore
    ) -> None:
        await store.ensure(ACCOUNT, now=EPOCH)
        later = EPOCH.replace(year=2027)

        other = await store.ensure(OTHER_ACCOUNT, now=later)

        assert other.created_at == later
        assert await total(store_database(store), "SELECT count(*) AS total FROM users") == 2

    async def test_two_simultaneous_first_writes_leave_exactly_one_row(
        self, store: SqlUserStore, database: Database
    ) -> None:
        await asyncio.gather(store.ensure(ACCOUNT, now=EPOCH), store.ensure(ACCOUNT, now=EPOCH))

        assert await total(database, "SELECT count(*) AS total FROM users") == 1

    async def test_neither_of_two_simultaneous_first_writes_sees_a_half_created_record(
        self, store: SqlUserStore, database: Database
    ) -> None:
        # The insert and the read-back are one transaction, so there is no instant at
        # which the row is half there. As two statements the loser of the race reads back
        # a record the winner has not committed yet, and ensure returns for a row it has
        # already promised to hand over.
        async with database_held(database):
            calls = [
                asyncio.create_task(store.ensure(ACCOUNT, now=EPOCH)),
                asyncio.create_task(store.ensure(ACCOUNT, now=EPOCH)),
            ]
            await park_behind_the_database(calls)

        records = await asyncio.gather(*calls)

        assert [record.account_id for record in records] == [ACCOUNT, ACCOUNT]
        assert records[0] == records[1]

    async def test_an_ensure_racing_a_delete_still_comes_home_with_a_record(
        self, store: SqlUserStore, database: Database
    ) -> None:
        """The race the single transaction exists for, spelled out.

        ``DELETE /v1/user`` is the somebody else that can run in the gap between an insert
        and a read-back, and an ensure that came home empty would raise inside a write path
        that had already decided the record was there.
        """
        async with database_held(database):
            first = asyncio.create_task(store.ensure(ACCOUNT, now=EPOCH))
            deletion = asyncio.create_task(store.delete(ACCOUNT))
            second = asyncio.create_task(store.ensure(ACCOUNT, now=EPOCH))
            await park_behind_the_database([first, deletion, second])

        outcomes = await asyncio.gather(first, deletion, second, return_exceptions=True)

        assert [isinstance(outcome, BaseException) for outcome in outcomes] == [False] * 3
        assert (await store.get(ACCOUNT)) is not None


def store_database(store: SqlUserStore) -> Database:
    """The database a store was built on. For assertions about the table itself."""
    return store._db


class TestGet:
    async def test_an_account_that_has_never_written_reads_back_as_absent(
        self, store: SqlUserStore
    ) -> None:
        assert await store.get("nobody") is None

    async def test_a_record_reads_back_with_the_stamps_it_was_written_with(
        self, store: SqlUserStore
    ) -> None:
        written = await store.ensure(ACCOUNT, now=EPOCH)

        read = await store.get(ACCOUNT)

        assert read == written


class TestTouch:
    async def test_touching_moves_updated_at_and_leaves_created_at_alone(
        self, store: SqlUserStore
    ) -> None:
        await store.ensure(ACCOUNT, now=EPOCH)
        later = EPOCH.replace(year=2027)

        await store.touch(ACCOUNT, now=later)

        record = await store.get(ACCOUNT)
        assert record is not None
        assert record.updated_at == later
        assert record.created_at == EPOCH

    async def test_touching_an_account_with_no_record_is_a_silent_no_op(
        self, store: SqlUserStore
    ) -> None:
        # The only way here is an account erased between the ensure and the touch of one
        # request, and the loser of that race can do nothing useful with a failure. A
        # rowcount check would be a branch no test could then provoke.
        await store.touch("nobody", now=EPOCH)

        assert await store.get("nobody") is None

    async def test_touching_one_account_leaves_another_alone(self, store: SqlUserStore) -> None:
        await store.ensure(ACCOUNT, now=EPOCH)
        await store.ensure(OTHER_ACCOUNT, now=EPOCH)

        await store.touch(ACCOUNT, now=EPOCH.replace(year=2027))

        untouched = await store.get(OTHER_ACCOUNT)
        assert untouched is not None
        assert untouched.updated_at == EPOCH


class TestDelete:
    async def test_it_destroys_the_entries_their_scopes_and_the_record_itself(
        self, store: SqlUserStore, entries: SqlEntryStore, database: Database
    ) -> None:
        await store.ensure(ACCOUNT, now=EPOCH)
        await write_field(entries, ACCOUNT, key="blood_type", value="O-", scopes=("health",))
        await write_note(entries, ACCOUNT, body="They prefer tea to coffee.")

        await store.delete(ACCOUNT)

        assert await total(database, "SELECT count(*) AS total FROM entries") == 0
        assert await total(database, "SELECT count(*) AS total FROM entry_scopes") == 0
        assert await total(database, "SELECT count(*) AS total FROM users") == 0

    async def test_it_destroys_the_search_index_rows_as_well(
        self, store: SqlUserStore, entries: SqlEntryStore, database: Database
    ) -> None:
        """The order of the deletes, asserted rather than described.

        ``entry_search`` rows know their entry by rowid and by nothing else, so deleting
        the entries first strands every index row -- unreachable by account, and each one
        still holding the words of a note somebody asked to have destroyed, in the same
        file, findable with ``grep``.
        """
        await store.ensure(ACCOUNT, now=EPOCH)
        await write_note(entries, ACCOUNT, body="They prefer tea to coffee.")

        await store.delete(ACCOUNT)

        assert await total(database, "SELECT count(*) AS total FROM entry_search") == 0

    async def test_it_destroys_the_events_and_the_settings(
        self,
        store: SqlUserStore,
        entries: SqlEntryStore,
        user_settings: SqlSettingsStore,
        database: Database,
    ) -> None:
        await store.ensure(ACCOUNT, now=EPOCH)
        await user_settings.update(
            ACCOUNT, now=EPOCH, default_grace_days=30, erasure_mode=ErasureMode.IMMEDIATE
        )
        await write_field(entries, ACCOUNT)

        await store.delete(ACCOUNT)

        assert await total(database, "SELECT count(*) AS total FROM events") == 0
        assert await total(database, "SELECT count(*) AS total FROM user_settings") == 0

    async def test_the_counts_say_what_went(
        self, store: SqlUserStore, entries: SqlEntryStore
    ) -> None:
        # The response says "2 entries, 3 events" and the person decides whether that is
        # what they expected, so a count that is merely plausible is no use.
        await store.ensure(ACCOUNT, now=EPOCH)
        await write_field(entries, ACCOUNT)
        note = await write_note(entries, ACCOUNT, body="They prefer tea to coffee.")
        await entries.forget(
            account_id=ACCOUNT,
            entry_id=note.entry_id,
            granted=None,
            now=EPOCH,
            asserted_by="user",
            journal=JOURNAL,
        )

        erased = await store.delete(ACCOUNT)

        assert erased == Erased(entries=2, events=3)

    async def test_a_forgotten_entry_is_destroyed_by_the_delete_like_any_other(
        self, store: SqlUserStore, entries: SqlEntryStore, database: Database
    ) -> None:
        # Unconditional, whatever the account's erasure mode says. "Delete everything you
        # know about me" has one honest reading and a tombstone is not it.
        await store.ensure(ACCOUNT, now=EPOCH)
        note = await write_note(entries, ACCOUNT, body="They prefer tea to coffee.")
        await entries.forget(
            account_id=ACCOUNT,
            entry_id=note.entry_id,
            granted=None,
            now=EPOCH,
            asserted_by="user",
            journal=JOURNAL,
        )

        erased = await store.delete(ACCOUNT)

        assert erased.entries == 1
        assert await total(database, "SELECT count(*) AS total FROM entries") == 0

    async def test_it_leaves_another_account_completely_untouched(
        self,
        store: SqlUserStore,
        entries: SqlEntryStore,
        user_settings: SqlSettingsStore,
        database: Database,
    ) -> None:
        # Every statement in the purge is account-scoped, and the one table without an
        # account column -- the search index -- is reached through the seq values of the
        # entries being destroyed. A test with one account in it would not notice.
        for account in (ACCOUNT, OTHER_ACCOUNT):
            await store.ensure(account, now=EPOCH)
            await user_settings.update(account, now=EPOCH, default_grace_days=30, grace_days=7)
            await write_field(entries, account)
            await write_note(entries, account, body=f"A note belonging to {account}.")

        await store.delete(ACCOUNT)

        assert await store.get(OTHER_ACCOUNT) is not None
        survivors = "SELECT count(*) AS total FROM {} WHERE account_id = ?"
        assert await total(database, survivors.format("entries"), (OTHER_ACCOUNT,)) == 2
        assert await total(database, survivors.format("events"), (OTHER_ACCOUNT,)) == 2
        assert await total(database, survivors.format("user_settings"), (OTHER_ACCOUNT,)) == 1
        assert await total(database, "SELECT count(*) AS total FROM entry_search") == 2
        assert await entries.index_agrees(OTHER_ACCOUNT)

    async def test_deleting_an_account_with_nothing_in_it_returns_zeroes(
        self, store: SqlUserStore
    ) -> None:
        await store.ensure(ACCOUNT, now=EPOCH)

        assert await store.delete(ACCOUNT) == Erased(entries=0, events=0)

    async def test_deleting_an_account_that_was_never_here_returns_zeroes(
        self, store: SqlUserStore
    ) -> None:
        # An erasure request for an account with no record is not an error: the caller
        # asked for there to be nothing, and there is nothing.
        assert await store.delete("nobody") == Erased(entries=0, events=0)

    async def test_a_second_delete_is_a_no_op_rather_than_a_failure(
        self, store: SqlUserStore, entries: SqlEntryStore
    ) -> None:
        await store.ensure(ACCOUNT, now=EPOCH)
        await write_field(entries, ACCOUNT)
        await store.delete(ACCOUNT)

        assert await store.delete(ACCOUNT) == Erased(entries=0, events=0)

    async def test_a_delete_racing_a_write_leaves_no_entry_without_its_record(
        self, store: SqlUserStore, entries: SqlEntryStore, database: Database
    ) -> None:
        # Entries reference the record by foreign key, so the two orders this can commit
        # in are "the entry went with the record" and "the entry was written afterwards
        # and has one". Neither is a row pointing at an account that is gone.
        await store.ensure(ACCOUNT, now=EPOCH)
        await write_field(entries, ACCOUNT)

        async with database_held(database):
            deletion = asyncio.create_task(store.delete(ACCOUNT))
            touch = asyncio.create_task(store.touch(ACCOUNT, now=EPOCH.replace(year=2027)))
            await park_behind_the_database([deletion, touch])

        await asyncio.gather(deletion, touch)

        orphans = (
            "SELECT count(*) AS total FROM entries e"
            " LEFT JOIN users u ON u.account_id = e.account_id WHERE u.account_id IS NULL"
        )
        assert await total(database, orphans) == 0
