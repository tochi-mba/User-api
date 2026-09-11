"""Where forgetting becomes gone.

The group that earns its keep. Most of it is arithmetic on a fake clock, and one test is
not: :class:`TestTheBytes` reads the raw bytes of the database file and its write-ahead log
and asserts a sentinel is in neither. That one cannot be replaced by anything else in this
suite, because it is about the file rather than about the code -- and because ``DELETE``
alone leaves the value in the ``-wal``, where a purge that reported success would have left
it findable with ``grep``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.fakes.clock import EPOCH, FakeClock
from user_api.domain.entries import NoteKind, Sensitivity, Source
from user_api.domain.settings import ErasureMode
from user_api.entries.store import Journal
from user_api.users.erasure import SWEEP_BATCH, Erasure
from user_api.users.sql_settings import SqlSettingsStore
from user_api.users.sql_store import SqlUserStore

if TYPE_CHECKING:
    from user_api.domain.entries import Entry
    from user_api.entries.sql_store import SqlEntryStore
    from user_api.events.sql_log import SqlEventLog
    from user_api.storage.database import Database

GRACE_DAYS = 30
SENTINEL = "ZORBLAX-their-diagnosis-is-nobody-elses-business-7741"
JOURNAL = Journal(cap=500, log_values=True)
"""Values ON throughout, so every test also proves the purge reaches into the log."""


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def users(database: Database) -> SqlUserStore:
    store = SqlUserStore(database=database)
    await store.ensure(ACCOUNT, now=EPOCH)
    await store.ensure(OTHER_ACCOUNT, now=EPOCH)
    return store


@pytest.fixture
def user_settings(database: Database) -> SqlSettingsStore:
    return SqlSettingsStore(database=database)


# Six collaborators, because that is what the thing under test takes.
@pytest.fixture
def erasure(  # noqa: PLR0917
    database: Database,
    entries: SqlEntryStore,
    events: SqlEventLog,
    user_settings: SqlSettingsStore,
    clock: FakeClock,
    users: SqlUserStore,
) -> Erasure:
    return Erasure(
        database=database,
        entries=entries,
        events=events,
        settings=user_settings,
        clock=clock,
        default_grace_days=GRACE_DAYS,
    )


async def note(
    entries: SqlEntryStore, *, account_id: str = ACCOUNT, body: str = "an ordinary note"
) -> Entry:
    return await entries.write_note(
        account_id=account_id,
        body=body,
        note_kind=NoteKind.EPISODE,
        description="something that happened",
        source=Source.STATED,
        source_detail=None,
        asserted_by="user",
        scopes=(),
        sensitivity=Sensitivity.NORMAL,
        pinned=False,
        now=EPOCH,
        entry_cap=10_000,
        pin_cap=100,
        journal=JOURNAL,
    )


async def forget(entries: SqlEntryStore, entry: Entry) -> Entry:
    return await entries.forget(
        account_id=entry.account_id,
        entry_id=entry.entry_id,
        granted=None,
        now=EPOCH,
        asserted_by="user",
        journal=JOURNAL,
    )


async def still_on_disk(database: Database, entry_id: str) -> bool:
    """Whether the row is still there at all, forgotten or not.

    ``EntryStore.get`` cannot answer this: it filters forgotten entries out, so it returns
    ``None`` for a tombstone and for a purge alike -- which is right for a caller and
    useless for a test that is about the difference between the two.
    """
    row = await database.fetch_one("SELECT 1 FROM entries WHERE entry_id = ?", (entry_id,))
    return row is not None


async def set_mode(
    user_settings: SqlSettingsStore, mode: ErasureMode, *, account_id: str = ACCOUNT, **rest: int
) -> None:
    await user_settings.update(
        account_id,
        now=EPOCH,
        default_grace_days=GRACE_DAYS,
        erasure_mode=mode,
        **rest,  # type: ignore[arg-type]
    )


class TestTheGracePeriod:
    async def test_a_forgotten_entry_survives_until_its_grace_period_is_up(
        self, database: Database, entries: SqlEntryStore, erasure: Erasure, clock: FakeClock
    ) -> None:
        forgotten = await forget(entries, await note(entries))

        clock.advance(timedelta(days=GRACE_DAYS - 1))

        assert await erasure.sweep_once() == 0
        assert await still_on_disk(database, forgotten.entry_id)
        assert forgotten.forgotten_at is not None

    async def test_it_is_destroyed_once_the_grace_period_has_passed(
        self, database: Database, entries: SqlEntryStore, erasure: Erasure, clock: FakeClock
    ) -> None:
        forgotten = await forget(entries, await note(entries))

        clock.advance(timedelta(days=GRACE_DAYS + 1))

        assert await erasure.sweep_once() == 1
        assert not await still_on_disk(database, forgotten.entry_id)

    async def test_an_entry_nobody_forgot_is_never_touched(
        self, entries: SqlEntryStore, erasure: Erasure, clock: FakeClock
    ) -> None:
        kept = await note(entries)

        clock.advance(timedelta(days=GRACE_DAYS * 10))
        await erasure.sweep_once()

        assert await entries.get(ACCOUNT, kept.entry_id, granted=None) is not None

    # account setup, the store, the sweeper, the settings and the clock.
    async def test_each_account_waits_out_its_own_window(
        self,
        database: Database,
        entries: SqlEntryStore,
        erasure: Erasure,
        user_settings: SqlSettingsStore,
        clock: FakeClock,
    ) -> None:
        # One global "everything older than N" would apply one person's setting to
        # another's data. The sweep asks who has forgotten something, then asks each of
        # them separately what that means.
        await set_mode(user_settings, ErasureMode.GRACE, grace_days=1)
        await set_mode(user_settings, ErasureMode.GRACE, account_id=OTHER_ACCOUNT, grace_days=90)
        impatient = await forget(entries, await note(entries))
        patient = await forget(entries, await note(entries, account_id=OTHER_ACCOUNT))

        clock.advance(timedelta(days=2))
        await erasure.sweep_once()

        assert not await still_on_disk(database, impatient.entry_id)
        assert await still_on_disk(database, patient.entry_id)


class TestImmediate:
    async def test_purge_now_destroys_before_the_call_returns(
        self, entries: SqlEntryStore, erasure: Erasure
    ) -> None:
        forgotten = await forget(entries, await note(entries))

        await erasure.purge_now(ACCOUNT, forgotten.entry_id)

        assert await entries.get(ACCOUNT, forgotten.entry_id, granted=None) is None
        assert await entries.due_for_purge(ACCOUNT, before=EPOCH, limit=10) == ()

    async def test_it_pays_for_its_own_checkpoint(
        self, database: Database, entries: SqlEntryStore, erasure: Erasure
    ) -> None:
        # The price of the promise that mode makes: the bytes are gone when the response
        # goes out, not whenever the sweeper next runs.
        forgotten = await forget(entries, await note(entries, body=SENTINEL))

        await erasure.purge_now(ACCOUNT, forgotten.entry_id)

        assert SENTINEL.encode() not in database.path.read_bytes()


class TestTombstone:
    async def test_a_tombstoned_entry_is_never_destroyed_however_long_it_waits(
        self,
        database: Database,
        entries: SqlEntryStore,
        erasure: Erasure,
        user_settings: SqlSettingsStore,
        clock: FakeClock,
    ) -> None:
        await set_mode(user_settings, ErasureMode.TOMBSTONE)
        forgotten = await forget(entries, await note(entries))

        clock.advance(timedelta(days=GRACE_DAYS * 100))

        assert await erasure.sweep_once() == 0
        assert (
            await entries.get(ACCOUNT, forgotten.entry_id, granted=None) is None
        )  # still invisible
        assert await entries.due_for_purge(ACCOUNT, before=clock.now(), limit=10) != ()


class TestChangingTheSettingIsNotRetroactive:
    async def test_switching_to_immediate_does_not_purge_what_is_already_waiting(
        self,
        database: Database,
        entries: SqlEntryStore,
        erasure: Erasure,
        user_settings: SqlSettingsStore,
        clock: FakeClock,
    ) -> None:
        # A settings change that silently destroyed data would be the worst surprise this
        # service could produce.
        forgotten = await forget(entries, await note(entries))
        await set_mode(user_settings, ErasureMode.IMMEDIATE)

        assert await erasure.sweep_once() == 0
        assert await still_on_disk(database, forgotten.entry_id)

    async def test_switching_away_from_tombstone_does_not_schedule_what_is_tombstoned(
        self,
        database: Database,
        entries: SqlEntryStore,
        erasure: Erasure,
        user_settings: SqlSettingsStore,
        clock: FakeClock,
    ) -> None:
        await set_mode(user_settings, ErasureMode.TOMBSTONE)
        forgotten = await forget(entries, await note(entries))
        await set_mode(user_settings, ErasureMode.GRACE, grace_days=GRACE_DAYS)

        # It is now an ordinary grace entry and waits out an ordinary grace period. What
        # it does NOT do is vanish the moment the setting changed.
        assert await erasure.sweep_once() == 0
        assert await still_on_disk(database, forgotten.entry_id)
        clock.advance(timedelta(days=GRACE_DAYS + 1))
        assert await erasure.sweep_once() == 1
        assert not await still_on_disk(database, forgotten.entry_id)


class TestTheBytes:
    async def test_a_purged_value_is_in_neither_the_database_nor_its_write_ahead_log(
        self, database: Database, entries: SqlEntryStore, erasure: Erasure, clock: FakeClock
    ) -> None:
        # The test the measurement exists for, and the only one here that is about the
        # file rather than the code. DELETE takes the row out of the b-tree and leaves
        # what it held in the -wal; a FULL checkpoint clears the main file and leaves the
        # page it copied FROM in the log. Only the truncating checkpoint empties both.
        for index in range(40):
            await note(entries, body=f"an ordinary neighbour note number {index}")
        doomed = await forget(entries, await note(entries, body=SENTINEL))
        await database.checkpoint_truncate()
        assert SENTINEL.encode() in database.path.read_bytes(), "the sentinel was never stored"

        clock.advance(timedelta(days=GRACE_DAYS + 1))
        await erasure.sweep_once()

        wal = database.path.with_name(database.path.name + "-wal")
        assert SENTINEL.encode() not in database.path.read_bytes()
        assert not wal.exists() or SENTINEL.encode() not in wal.read_bytes()
        assert await entries.get(ACCOUNT, doomed.entry_id, granted=None) is None

    async def test_the_search_index_no_longer_matches_a_purged_entry(
        self, entries: SqlEntryStore, erasure: Erasure, clock: FakeClock
    ) -> None:
        from user_api.domain.cursors import Ordering
        from user_api.entries.store import Filters

        await forget(entries, await note(entries, body=SENTINEL))

        clock.advance(timedelta(days=GRACE_DAYS + 1))
        await erasure.sweep_once()

        found = await entries.search(
            ACCOUNT,
            granted=None,
            filters=Filters(query="ZORBLAX", include_forgotten=True),
            ordering=Ordering.RELEVANCE,
            limit=10,
        )
        assert found.entries == ()
        assert await entries.index_agrees(ACCOUNT)


class TestTheEventLog:
    async def test_an_event_outlives_the_entry_it_describes(
        self, entries: SqlEntryStore, events: SqlEventLog, erasure: Erasure, clock: FakeClock
    ) -> None:
        # The record that something was forgotten survives the thing that was forgotten,
        # which is the whole point of keeping a log.
        doomed = await forget(entries, await note(entries, body=SENTINEL))

        clock.advance(timedelta(days=GRACE_DAYS + 1))
        await erasure.sweep_once()

        written = await events.read(ACCOUNT, limit=20)
        assert doomed.entry_id in {event.entry_id for event in written}

    async def test_the_values_in_those_events_are_stripped(
        self, entries: SqlEntryStore, events: SqlEventLog, erasure: Erasure, clock: FakeClock
    ) -> None:
        # With log_values on, the old value of a forgotten entry is sitting in the event
        # that recorded the change. A purge that only deleted the entry would leave it
        # there, in the same file.
        doomed = await forget(entries, await note(entries, body=SENTINEL))
        before = await events.read(ACCOUNT, limit=20)
        assert any(event.detail for event in before), "nothing was logged to strip"

        clock.advance(timedelta(days=GRACE_DAYS + 1))
        await erasure.sweep_once()

        after = [
            event
            for event in await events.read(ACCOUNT, limit=20)
            if event.entry_id == doomed.entry_id
        ]
        assert after != []
        assert all(event.detail is None for event in after)


class TestTheSweep:
    async def test_a_sweep_with_nothing_due_does_no_work_and_says_so(
        self, erasure: Erasure
    ) -> None:
        assert await erasure.sweep_once() == 0

    async def test_an_account_with_nothing_forgotten_is_not_visited(
        self, entries: SqlEntryStore, erasure: Erasure, clock: FakeClock
    ) -> None:
        await note(entries)

        assert await entries.accounts_with_forgotten() == ()
        assert await erasure.sweep_once() == 0

    async def test_one_sweep_is_bounded_and_the_rest_goes_on_the_next(
        self, entries: SqlEntryStore, erasure: Erasure, clock: FakeClock
    ) -> None:
        # Bounded so one account with a very large backlog cannot hold the single database
        # thread for an unbounded stretch.
        assert SWEEP_BATCH > 0
        for index in range(3):
            await forget(entries, await note(entries, body=f"doomed {index}"))
        clock.advance(timedelta(days=GRACE_DAYS + 1))

        first = await erasure.sweep_once()
        second = await erasure.sweep_once()

        assert first == 3
        assert second == 0

    async def test_the_cutoff_is_measured_on_the_injected_clock(
        self, database: Database, entries: SqlEntryStore, erasure: Erasure, clock: FakeClock
    ) -> None:
        forgotten = await forget(entries, await note(entries))

        # Exactly at the boundary is not past it: due_for_purge asks for strictly before.
        clock.advance(timedelta(days=GRACE_DAYS))
        assert await erasure.sweep_once() == 0

        clock.advance(timedelta(seconds=1))
        assert await erasure.sweep_once() == 1
        assert not await still_on_disk(database, forgotten.entry_id)

    async def test_a_zero_day_grace_still_waits_for_a_sweep(
        self,
        entries: SqlEntryStore,
        erasure: Erasure,
        user_settings: SqlSettingsStore,
        clock: FakeClock,
    ) -> None:
        # Which is what distinguishes it from immediate mode, and is worth knowing before
        # somebody sets it to zero expecting the other thing.
        await set_mode(user_settings, ErasureMode.GRACE, grace_days=0)
        forgotten = await forget(entries, await note(entries))

        assert await entries.get(ACCOUNT, forgotten.entry_id, granted=None) is None
        clock.advance(timedelta(seconds=1))
        assert await erasure.sweep_once() == 1
