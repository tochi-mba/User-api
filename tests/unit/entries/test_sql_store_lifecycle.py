"""Caps, revisions, forget/purge and the search index beside them.

**The search index is a second copy maintained by hand.** :class:`TestIndexIntegrity` is
the price of that trade: create, revise, forget, purge and erase, and the table and the
index still hold the same rows. One test in there corrupts the index on purpose, because a
check that cannot fail is not a check.

**Every cap is counted inside the transaction that writes.** :class:`TestCaps` therefore
runs its writes concurrently, through the same ``database_held`` harness keyring's role
store uses: every call is provably queued and unstarted before the first is allowed to
proceed, which is the interleaving a check made outside the transaction gets wrong. What
is asserted is the outcome -- how many rows survive -- and never the mechanism.

**Nothing here sleeps.** :data:`~tests.fakes.clock.EPOCH` and offsets from it are passed
in as ``now``, so an entry confirmed an hour ago is a parameter rather than a wait.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.fakes.clock import EPOCH
from tests.unit.entries._helpers import (
    LATER,
    MUCH_LATER,
    confirm,
    count_rows,
    database_held,
    find,
    forget,
    ids,
    park_behind_the_database,
    purge,
    revise,
    write_field,
    write_note,
)
from user_api.domain.entries import NoteKind, Sensitivity, ValueType, new_entry_id
from user_api.domain.errors import EntryNotFoundError, LimitExceededError
from user_api.entries.sql_rows import _require_rowid
from user_api.entries.store import UNSET, Counts
from user_api.users.sql_store import SqlUserStore

if TYPE_CHECKING:
    from user_api.entries.sql_store import SqlEntryStore
    from user_api.storage.database import Database


class TestCaps:
    async def test_the_entry_cap_refuses_a_new_entry(self, entries: SqlEntryStore) -> None:
        await write_field(entries, key="a", entry_cap=1)

        with pytest.raises(LimitExceededError):
            await write_field(entries, key="b", entry_cap=1)

    async def test_the_entry_cap_refuses_a_new_note(self, entries: SqlEntryStore) -> None:
        await write_field(entries, key="a", entry_cap=1)

        with pytest.raises(LimitExceededError):
            await write_note(entries, entry_cap=1)

    async def test_the_field_cap_refuses_a_new_field_but_not_a_note(
        self, entries: SqlEntryStore
    ) -> None:
        # Separate caps, separately counted: a full field budget must not be reported as
        # a full account.
        await write_field(entries, key="a", field_cap=1)

        with pytest.raises(LimitExceededError):
            await write_field(entries, key="b", field_cap=1)
        assert await write_note(entries) is not None

    async def test_the_pin_cap_refuses_one_pin_too_many(self, entries: SqlEntryStore) -> None:
        # Pinning is a token budget before it is a preference: the always-load set goes
        # straight into a context window at the start of every conversation.
        await write_field(entries, key="a", pinned=True, pin_cap=1)

        with pytest.raises(LimitExceededError):
            await write_field(entries, key="b", pinned=True, pin_cap=1)
        with pytest.raises(LimitExceededError):
            await write_note(entries, pinned=True, pin_cap=1)

    async def test_the_pin_cap_refuses_a_revision_that_would_pass_it(
        self, entries: SqlEntryStore
    ) -> None:
        await write_field(entries, key="a", pinned=True, pin_cap=1)
        other = await write_field(entries, key="b", pinned=False)

        with pytest.raises(LimitExceededError):
            await revise(entries, other, pinned=True, pin_cap=1)

        still = await entries.get(ACCOUNT, other.entry_id, granted=None)
        assert still is not None
        assert still.pinned is False

    async def test_repinning_something_already_pinned_is_not_a_new_pin(
        self, entries: SqlEntryStore
    ) -> None:
        # The check is on the transition, not on the state: a replacement that leaves an
        # already-pinned field pinned adds nothing to the budget and must not be refused.
        pinned = await write_field(entries, key="a", pinned=True, pin_cap=1)

        again = await write_field(entries, key="a", pinned=True, pin_cap=1, now=LATER)
        revised = await revise(entries, pinned, pinned=True, pin_cap=1)

        assert again.pinned is True
        assert revised.pinned is True

    async def test_replacing_a_field_is_never_refused_however_full_the_account_is(
        self, entries: SqlEntryStore
    ) -> None:
        # The caps bound how many entries an account holds, not how often it corrects
        # them. An account at its limit that could not fix a wrong value would be stuck.
        await write_field(entries, key="a", value="Sam", entry_cap=1, field_cap=1)

        replaced = await write_field(
            entries, key="a", value="Samantha", entry_cap=1, field_cap=1, now=LATER
        )

        assert replaced.value == "Samantha"
        assert replaced.revision == 2

    async def test_the_entry_cap_holds_when_every_write_arrives_at_once(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        # Counted and enforced inside the transaction that writes, so there is no window
        # in which two writers both read "one held" and both proceed.
        async with database_held(database):
            writes = [
                asyncio.create_task(write_field(entries, key=f"k{index}", entry_cap=2))
                for index in range(5)
            ]
            await park_behind_the_database(writes)

        outcomes = await asyncio.gather(*writes, return_exceptions=True)

        refused = [item for item in outcomes if isinstance(item, LimitExceededError)]
        assert len(refused) == 3
        assert await count_rows(database, "entries WHERE account_id = ?", (ACCOUNT,)) == 2

    async def test_the_field_cap_holds_when_every_write_arrives_at_once(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        async with database_held(database):
            writes = [
                asyncio.create_task(write_field(entries, key=f"k{index}", field_cap=2))
                for index in range(5)
            ]
            await park_behind_the_database(writes)

        outcomes = await asyncio.gather(*writes, return_exceptions=True)

        refused = [item for item in outcomes if isinstance(item, LimitExceededError)]
        assert len(refused) == 3
        assert await count_rows(database, "entries WHERE entry_type = 'field'") == 2

    async def test_the_pin_cap_holds_when_every_pin_arrives_at_once(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        async with database_held(database):
            writes = [
                asyncio.create_task(write_note(entries, pinned=True, pin_cap=2)) for _ in range(5)
            ]
            await park_behind_the_database(writes)

        outcomes = await asyncio.gather(*writes, return_exceptions=True)

        refused = [item for item in outcomes if isinstance(item, LimitExceededError)]
        assert len(refused) == 3
        assert len(await entries.pinned(ACCOUNT, granted=None, limit=10)) == 2

    async def test_two_writes_of_one_key_at_once_leave_one_field_and_no_lost_write(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        # Idempotence by key has to survive the moment it matters: two retries of the
        # same PUT crossing in flight. One row, one bumped revision, and the value of
        # whichever write went second -- not a silent half of each.
        async with database_held(database):
            writes = [
                asyncio.create_task(write_field(entries, value="Sam")),
                asyncio.create_task(write_field(entries, value="Samantha")),
            ]
            await park_behind_the_database(writes)

        first, second = await asyncio.gather(*writes)

        assert first.entry_id == second.entry_id
        assert await count_rows(database, "entries WHERE key = 'preferred_name'") == 1
        stored = await entries.get_field(ACCOUNT, "preferred_name", granted=None)
        assert stored is not None
        assert stored.revision == 2
        assert stored.value == max((first, second), key=lambda entry: entry.revision).value


class TestIndexIntegrity:
    """The index is a second copy kept honest by one helper, so this is the price.

    Every assertion here is ``index_agrees``: the table and the FTS index hold the same
    rows, with the same text, and neither holds a row the other does not.
    """

    async def test_the_index_agrees_after_a_field_is_created(self, entries: SqlEntryStore) -> None:
        await write_field(entries)

        assert await entries.index_agrees(ACCOUNT) is True

    async def test_the_index_agrees_after_a_replacement(self, entries: SqlEntryStore) -> None:
        await write_field(entries, value="Sam")

        await write_field(entries, value="Samantha", now=LATER)

        assert await entries.index_agrees(ACCOUNT) is True

    async def test_the_index_agrees_after_a_revision(self, entries: SqlEntryStore) -> None:
        written = await write_field(entries, value="Sam")

        await revise(entries, written, value="Samantha")

        assert await entries.index_agrees(ACCOUNT) is True
        assert ids((await find(entries, query="Samantha")).entries) == [written.entry_id]

    async def test_a_revised_field_is_no_longer_found_by_its_old_value(
        self, entries: SqlEntryStore
    ) -> None:
        # INSERT OR REPLACE on the rowid rather than delete-then-insert, so a revision
        # cannot leave two rows behind -- and the old text cannot survive in one of them.
        written = await write_field(entries, value="Sam")

        await revise(entries, written, value="Samantha")

        assert (await find(entries, query="Sam")).entries == ()

    async def test_the_index_agrees_after_a_forget(self, entries: SqlEntryStore) -> None:
        written = await write_field(entries)

        await forget(entries, written)

        assert await entries.index_agrees(ACCOUNT) is True

    async def test_the_index_agrees_after_a_purge(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        written = await write_field(entries)
        await forget(entries, written)

        await purge(entries, database, written.entry_id)

        assert await entries.index_agrees(ACCOUNT) is True

    async def test_a_purged_entry_no_longer_matches_a_search(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        # Forgetting hides the row and leaves the words; purging is where the words go.
        written = await write_note(entries, body="Dr Okonkwo at the Meadow Clinic")
        await forget(entries, written)

        await purge(entries, database, written.entry_id)

        assert (await find(entries, query="Okonkwo", include_forgotten=True)).entries == ()

    async def test_erasing_an_account_leaves_no_orphaned_index_rows(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        """The cascade cannot reach the FTS table, which is why this is asserted here.

        ``entry_search`` rows know their entry by rowid and by nothing else, so entries
        deleted first would leave index rows no statement could reach by account any more
        -- each still holding the words of a note somebody asked to have destroyed.
        """
        scoped = await write_field(entries, scopes=("health",))
        await write_note(entries, body="Dr Okonkwo at the Meadow Clinic")
        await write_note(entries, account_id=OTHER_ACCOUNT, body="Somebody else's note")

        await SqlUserStore(database=database).delete(ACCOUNT)

        assert await entries.index_agrees(ACCOUNT) is True
        assert await entries.index_agrees(OTHER_ACCOUNT) is True
        orphans = await count_rows(
            database,
            "entry_search f LEFT JOIN entries e ON e.seq = f.rowid WHERE e.seq IS NULL",
        )
        assert orphans == 0
        scope_rows = await count_rows(
            database, "entry_scopes WHERE entry_id = ?", (scoped.entry_id,)
        )
        assert scope_rows == 0

    async def test_the_check_notices_an_index_row_that_went_missing(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        # A check that cannot fail is not a check. This corrupts the index the way a
        # write path that forgot to call _index() would, and the check has to say so --
        # otherwise every assertion above passes against a function that returns True.
        written = await write_field(entries)
        seq = await database.fetch_one(
            "SELECT seq FROM entries WHERE entry_id = ?", (written.entry_id,)
        )
        assert seq is not None
        await database.execute("DELETE FROM entry_search WHERE rowid = ?", (seq["seq"],))

        assert await entries.index_agrees(ACCOUNT) is False

    async def test_the_check_notices_index_text_that_went_stale(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        # The other half: a row present in both, holding different words in each. This is
        # what a write that updated the column and skipped the index would leave behind.
        written = await write_field(entries)
        await database.execute(
            "UPDATE entries SET search_text = 'something else' WHERE entry_id = ?",
            (written.entry_id,),
        )

        assert await entries.index_agrees(ACCOUNT) is False


class TestRevise:
    async def test_a_partial_revision_leaves_everything_it_did_not_mention_alone(
        self, entries: SqlEntryStore
    ) -> None:
        # The reason there is no save(entry): writing a whole entry back means writing
        # back everything a caller read some time ago, and the loser of two concurrent
        # revisions never finds out.
        stored = await write_field(
            entries,
            key="timezone",
            value="Europe/Lisbon",
            description="Where they are",
            source_detail="said in conversation",
            sensitivity=Sensitivity.SENSITIVE,
            pinned=True,
            scopes=("home",),
            granted="home",
        )

        revised = await revise(entries, stored, granted="home", value="Europe/Madrid")

        assert revised == replace(stored, value="Europe/Madrid", revision=2, updated_at=LATER)

    async def test_each_revision_bumps_the_revision(self, entries: SqlEntryStore) -> None:
        # A reader who held revision 1 can tell that something changed without having to
        # compare every field of what they held against what they got.
        stored = await write_field(entries)

        once = await revise(entries, stored, value="Samantha")
        twice = await revise(entries, once, value="Sammy", now=MUCH_LATER)

        assert [once.revision, twice.revision] == [2, 3]

    async def test_a_revision_leaves_confirmed_at_where_it_was(
        self, entries: SqlEntryStore
    ) -> None:
        # Correcting is not confirming. Conflating them would make every correction reset
        # the staleness clock, and finding the facts nobody has vouched for lately is the
        # whole point of tracking it.
        stored = await write_field(entries)
        confirmed = await confirm(entries, stored, now=EPOCH)

        revised = await revise(entries, confirmed, value="Samantha", now=MUCH_LATER)

        assert revised.confirmed_at == EPOCH

    async def test_leaving_the_value_out_is_not_the_same_as_setting_it_to_null(
        self, entries: SqlEntryStore
    ) -> None:
        # null is a legal field value, so "unset it" and "leave it alone" are different
        # requests and both have to be expressible. None cannot carry both meanings.
        stored = await write_field(entries, value="Sam")

        untouched = await revise(entries, stored, value=UNSET, description="Still theirs")
        assert untouched.value == "Sam"
        assert untouched.value_type is ValueType.STRING

        cleared = await revise(entries, untouched, value=None, now=MUCH_LATER)
        assert cleared.value is None
        assert cleared.value_type is ValueType.NULL

    async def test_a_revision_moves_who_vouches_for_the_entry_now(
        self, entries: SqlEntryStore
    ) -> None:
        # asserted_by means "the token that vouches for what this says NOW". Leaving it
        # on the original writer would attribute a value somebody else changed to
        # whoever happened to write the old one.
        stored = await write_field(entries, asserted_by="user.home")

        revised = await revise(entries, stored, value="Samantha", asserted_by="user.work")

        assert revised.asserted_by == "user.work"

    async def test_scopes_change_only_when_the_revision_mentions_them(
        self, entries: SqlEntryStore
    ) -> None:
        stored = await write_field(entries, scopes=("home",), granted="home")

        unmentioned = await revise(entries, stored, granted="home", value="Samantha")
        assert unmentioned.scopes == ("home",)

        narrowed = await revise(
            entries, stored, granted="home", scopes=("home", "work"), now=MUCH_LATER
        )
        assert narrowed.scopes == ("home", "work")

    async def test_revising_a_note_changes_its_body_and_leaves_it_a_note(
        self, entries: SqlEntryStore
    ) -> None:
        written = await write_note(entries, note_kind=NoteKind.LESSON)

        revised = await revise(entries, written, body="Do not suggest restaurants unasked.")

        assert revised.body == "Do not suggest restaurants unasked."
        assert revised.note_kind is NoteKind.LESSON
        assert revised.value is None
        assert revised.value_type is None
        assert ids((await find(entries, query="restaurants")).entries) == [written.entry_id]

    async def test_revising_an_entry_this_token_cannot_see_raises(
        self, entries: SqlEntryStore
    ) -> None:
        # The visibility predicate is applied to writes too, so a user.home token cannot
        # revise a health-scoped entry it could not have read.
        hidden = await write_field(entries, scopes=("health",))

        with pytest.raises(EntryNotFoundError):
            await revise(entries, hidden, granted="home", value="Samantha")

        stored = await entries.get(ACCOUNT, hidden.entry_id, granted="health")
        assert stored is not None
        assert stored.value == "Sam"

    async def test_revising_another_accounts_entry_raises(self, entries: SqlEntryStore) -> None:
        theirs = await write_field(entries, account_id=OTHER_ACCOUNT)

        with pytest.raises(EntryNotFoundError):
            await revise(entries, theirs, account_id=ACCOUNT, value="Samantha")

    async def test_revising_an_entry_that_never_existed_raises(
        self, entries: SqlEntryStore
    ) -> None:
        stored = await write_field(entries)

        with pytest.raises(EntryNotFoundError):
            await revise(entries, replace(stored, entry_id=new_entry_id()), value="Samantha")

    async def test_revising_a_forgotten_entry_raises(self, entries: SqlEntryStore) -> None:
        stored = await write_field(entries)
        await forget(entries, stored)

        with pytest.raises(EntryNotFoundError):
            await revise(entries, stored, value="Samantha", now=MUCH_LATER)


class TestConfirm:
    async def test_confirming_sets_confirmed_at_and_changes_nothing_else(
        self, entries: SqlEntryStore
    ) -> None:
        # Not updated_at and not revision: nothing changed. An entry whose updated_at
        # moved every time somebody said "yes, still true" would sort to the top of a
        # recency listing for not changing.
        stored = await write_field(entries)

        confirmed = await confirm(entries, stored, now=LATER)

        assert confirmed == replace(stored, confirmed_at=LATER)

    async def test_confirming_again_moves_the_confirmation_forward(
        self, entries: SqlEntryStore
    ) -> None:
        stored = await write_field(entries)
        await confirm(entries, stored, now=LATER)

        again = await confirm(entries, stored, now=MUCH_LATER)

        assert again.confirmed_at == MUCH_LATER

    async def test_confirming_an_entry_this_token_cannot_see_raises(
        self, entries: SqlEntryStore
    ) -> None:
        hidden = await write_field(entries, scopes=("health",))

        with pytest.raises(EntryNotFoundError):
            await confirm(entries, hidden, granted="home")

    async def test_confirming_another_accounts_entry_raises(self, entries: SqlEntryStore) -> None:
        theirs = await write_field(entries, account_id=OTHER_ACCOUNT)

        with pytest.raises(EntryNotFoundError):
            await confirm(entries, theirs, account_id=ACCOUNT)

    async def test_confirming_a_forgotten_entry_raises(self, entries: SqlEntryStore) -> None:
        stored = await write_field(entries)
        await forget(entries, stored)

        with pytest.raises(EntryNotFoundError):
            await confirm(entries, stored, now=MUCH_LATER)


class TestForget:
    async def test_forgetting_stamps_the_moment_it_happened(self, entries: SqlEntryStore) -> None:
        # Marking only. Whether and when the bytes go is the account's erasure setting.
        stored = await write_field(entries)

        forgotten = await forget(entries, stored, now=LATER)

        assert forgotten.forgotten_at == LATER
        assert forgotten.is_forgotten is True

    async def test_a_forgotten_entry_is_invisible_from_every_read_path(
        self, entries: SqlEntryStore
    ) -> None:
        stored = await write_field(entries, key="blood_type", pinned=True)

        await forget(entries, stored)

        assert await entries.get(ACCOUNT, stored.entry_id, granted=None) is None
        assert (await find(entries)).entries == ()
        assert (await find(entries, query="blood")).entries == ()
        assert await entries.pinned(ACCOUNT, granted=None, limit=10) == ()
        assert await entries.describe(ACCOUNT, granted=None) == ()

    async def test_a_forgotten_field_is_no_longer_returned_by_its_key(
        self, entries: SqlEntryStore
    ) -> None:
        # The port promises "the one LIVE field with this key", and forgetting promises
        # invisibility from every read path. get_field's WHERE carries the account, the
        # key and the entry type, and no liveness predicate -- so this is the one read
        # path that still hands back what somebody asked to have forgotten.
        stored = await write_field(entries, key="blood_type")

        await forget(entries, stored)

        assert await entries.get_field(ACCOUNT, "blood_type", granted=None) is None

    async def test_a_forgotten_entry_still_counts_as_forgotten(
        self, entries: SqlEntryStore
    ) -> None:
        stored = await write_field(entries, pinned=True)

        await forget(entries, stored)

        assert await entries.counts(ACCOUNT, granted=None) == Counts(
            fields=0, notes=0, pinned=0, forgotten=1
        )

    async def test_a_forgotten_entry_comes_back_with_include_forgotten(
        self, entries: SqlEntryStore
    ) -> None:
        stored = await write_field(entries)
        await forget(entries, stored)

        page = await find(entries, include_forgotten=True)

        assert ids(page.entries) == [stored.entry_id]
        assert page.entries[0].forgotten_at == LATER

    async def test_a_forgotten_entry_is_still_in_the_search_index(
        self, entries: SqlEntryStore
    ) -> None:
        # Deliberate, and the reason the index row goes at purge rather than here.
        # Reviewing what you asked to be forgotten has to work with a query in it, and it
        # cannot if forgetting removed the words the review would search for.
        stored = await write_note(entries, body="Dr Okonkwo at the Meadow Clinic")
        await forget(entries, stored)

        page = await find(entries, query="Okonkwo", include_forgotten=True)

        assert ids(page.entries) == [stored.entry_id]

    async def test_a_forgotten_key_can_be_used_again(self, entries: SqlEntryStore) -> None:
        # The uniqueness index is partial on forgotten_at IS NULL, which is what makes
        # "forget that and I will tell you again" work rather than fail on a constraint.
        first = await write_field(entries, key="timezone", value="Europe/Lisbon")
        await forget(entries, first)

        second = await write_field(entries, key="timezone", value="Europe/Madrid", now=MUCH_LATER)

        assert second.entry_id != first.entry_id
        assert second.revision == 1
        assert second.created_at == MUCH_LATER

    async def test_forgetting_the_same_entry_twice_reads_as_absent(
        self, entries: SqlEntryStore
    ) -> None:
        stored = await write_field(entries)
        await forget(entries, stored)

        with pytest.raises(EntryNotFoundError):
            await forget(entries, stored, now=MUCH_LATER)

    async def test_forgetting_an_entry_this_token_cannot_see_raises(
        self, entries: SqlEntryStore
    ) -> None:
        hidden = await write_field(entries, scopes=("health",))

        with pytest.raises(EntryNotFoundError):
            await forget(entries, hidden, granted="home")

        stored = await entries.get(ACCOUNT, hidden.entry_id, granted="health")
        assert stored is not None
        assert stored.forgotten_at is None


class TestPurge:
    async def test_purging_removes_the_row_its_scopes_and_its_index_row(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        stored = await write_field(entries, scopes=("health",), value="O negative")
        await forget(entries, stored, granted="health")

        assert await purge(entries, database, stored.entry_id) is True

        assert await count_rows(database, "entries WHERE entry_id = ?", (stored.entry_id,)) == 0
        assert (
            await count_rows(database, "entry_scopes WHERE entry_id = ?", (stored.entry_id,)) == 0
        )
        assert await entries.index_agrees(ACCOUNT) is True

    async def test_purging_an_entry_that_is_not_there_reports_that_it_was_not(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        assert await purge(entries, database, new_entry_id()) is False

    async def test_purging_another_accounts_entry_reports_nothing_and_leaves_it_alone(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        # The sweeper holds an account and a list of ids, and the account is checked
        # again here: a sweep running for one account must not be able to destroy
        # another's row because an id was passed to the wrong call.
        theirs = await write_field(entries, account_id=OTHER_ACCOUNT)

        assert await purge(entries, database, theirs.entry_id, account_id=ACCOUNT) is False

        assert await entries.get(OTHER_ACCOUNT, theirs.entry_id, granted=None) is not None

    async def test_a_live_entry_can_be_purged_too(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        # Not scope-filtered and not liveness-filtered: this is reached only through
        # erasure, which has already established what it is destroying. A sweeper
        # filtered by something it does not hold would leave rows behind forever.
        stored = await write_field(entries)

        assert await purge(entries, database, stored.entry_id) is True

        assert await entries.get(ACCOUNT, stored.entry_id, granted=None) is None


class TestSweepSupport:
    async def test_only_accounts_holding_a_forgotten_entry_are_reported(
        self, entries: SqlEntryStore
    ) -> None:
        # The sweep is two calls rather than one clever query because how long a
        # forgotten entry survives is a per-account setting, and this layer sits below
        # the one that holds it. It reports who has forgotten something and nothing more.
        mine = await write_field(entries)
        await write_field(entries, account_id=OTHER_ACCOUNT)
        await forget(entries, mine)

        assert await entries.accounts_with_forgotten() == (ACCOUNT,)

    async def test_an_account_is_reported_once_however_much_it_forgot(
        self, entries: SqlEntryStore
    ) -> None:
        first = await write_field(entries, key="a")
        second = await write_field(entries, key="b")
        await forget(entries, first)
        await forget(entries, second)

        assert await entries.accounts_with_forgotten() == (ACCOUNT,)

    async def test_nothing_forgotten_means_nothing_to_sweep(self, entries: SqlEntryStore) -> None:
        await write_field(entries)

        assert await entries.accounts_with_forgotten() == ()

    async def test_due_for_purge_returns_ids_and_no_content(self, entries: SqlEntryStore) -> None:
        # Ids only, so a sweep never holds anybody's data in memory and a sweep that
        # crashes mid-way leaves nothing behind in a traceback.
        stored = await write_field(entries, value="O negative")
        await forget(entries, stored, now=EPOCH)

        assert await entries.due_for_purge(ACCOUNT, before=LATER, limit=10) == (stored.entry_id,)

    async def test_due_for_purge_leaves_anything_forgotten_after_the_cutoff(
        self, entries: SqlEntryStore
    ) -> None:
        old = await write_field(entries, key="a")
        recent = await write_field(entries, key="b")
        await forget(entries, old, now=EPOCH)
        await forget(entries, recent, now=MUCH_LATER)

        due = await entries.due_for_purge(ACCOUNT, before=LATER, limit=10)

        assert due == (old.entry_id,)

    async def test_due_for_purge_honours_its_limit_oldest_first(
        self, entries: SqlEntryStore
    ) -> None:
        first = await write_field(entries, key="a")
        second = await write_field(entries, key="b")
        await forget(entries, first, now=EPOCH)
        await forget(entries, second, now=LATER)

        due = await entries.due_for_purge(ACCOUNT, before=MUCH_LATER, limit=1)

        assert due == (first.entry_id,)

    async def test_due_for_purge_is_scoped_to_the_account_it_was_asked_about(
        self, entries: SqlEntryStore
    ) -> None:
        theirs = await write_field(entries, account_id=OTHER_ACCOUNT)
        await forget(entries, theirs, now=EPOCH)

        assert await entries.due_for_purge(ACCOUNT, before=LATER, limit=10) == ()
        assert await entries.due_for_purge(OTHER_ACCOUNT, before=LATER, limit=10) == (
            theirs.entry_id,
        )


class TestTheRowidNarrowing:
    """The one branch no write path can reach, called directly because of that.

    ``lastrowid`` is typed optional because it is meaningless after a statement that is
    not an INSERT, and it is only ever read immediately after one. An assertion would be
    a branch no test could drive, so it raises an error a test can provoke by hand.
    """

    def test_a_rowid_the_driver_actually_returned_comes_straight_back(self) -> None:
        assert _require_rowid(7) == 7

    def test_an_insert_that_reported_no_rowid_is_a_storage_failure_not_a_silent_zero(
        self,
    ) -> None:
        with pytest.raises(RuntimeError):
            _require_rowid(None)
