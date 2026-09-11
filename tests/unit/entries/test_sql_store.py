"""The widest module in the service, against the database it was written for.

Four things in here are load-bearing, and each has a class named after it.

**Visibility is enforced in SQL and stated in the domain.** ``_VISIBLE`` is the
enforcement and :meth:`~user_api.domain.entries.Entry.visible_to` is the statement of the
rule, so :class:`TestVisibility` builds every combination of scopes and grant and asserts
the two agree. A boundary that exists only as a WHERE clause is a boundary nobody can
read, and one that exists only as a predicate nothing calls is not a boundary at all.

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
import contextlib
import inspect
import threading
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.fakes.clock import EPOCH
from user_api.domain.cursors import Ordering, decode_cursor
from user_api.domain.entries import (
    Entry,
    EntryType,
    NoteKind,
    Sensitivity,
    Source,
    ValueType,
    new_entry_id,
)
from user_api.domain.errors import (
    EntryNotFoundError,
    InvalidCursorError,
    InvalidSearchError,
    LimitExceededError,
    ScopeConflictError,
)
from user_api.entries.sql_store import SqlEntryStore, _require_rowid
from user_api.entries.store import UNSET, Counts, FieldSummary, Filters, Journal, Page
from user_api.users.sql_store import SqlUserStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Sequence

    from user_api.domain.cursors import Cursor
    from user_api.storage.database import Database

SCHEDULER_TURNS = 8
"""Passes through the event loop, enough for every queued call to reach the database."""

ENTRY_CAP = 1_000
FIELD_CAP = 200
PIN_CAP = 10
"""High enough that a test about anything else cannot trip a limit by accident.

A test that is *about* a cap passes a low value for that one cap and leaves the other two
here, so a refusal can only have come from the limit under test.
"""

JOURNAL = Journal(cap=100)

LATER = EPOCH + timedelta(hours=1)
MUCH_LATER = EPOCH + timedelta(hours=2)

SCOPE_SETS: tuple[tuple[str, ...], ...] = ((), ("home",), ("health",), ("home", "work"))
GRANTS: tuple[str | None, ...] = (None, "home", "work", "health")


@pytest.fixture(autouse=True)
async def _accounts(database: Database) -> None:
    """Both accounts exist before any entry does.

    ``entries.account_id`` is a foreign key to ``users``, and the connection refuses to
    open unless foreign keys really came on, so an entry written without this does not
    fail somewhere subtle later: it fails here.
    """
    users = SqlUserStore(database=database)
    await users.ensure(ACCOUNT, now=EPOCH)
    await users.ensure(OTHER_ACCOUNT, now=EPOCH)


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

    Asserting that none of them finished is what makes the concurrent tests mean
    something: it proves each call really is suspended before it has read anything,
    rather than having run to completion before the next one started.
    """
    for _ in range(SCHEDULER_TURNS):
        await asyncio.sleep(0)

    assert [call.done() for call in calls] == [False] * len(calls)


async def write_field(entries: SqlEntryStore, **overrides: Any) -> Entry:
    """Store one field, defaulted everywhere a test is not about."""
    arguments: dict[str, Any] = {
        "account_id": ACCOUNT,
        "key": "preferred_name",
        "value": "Sam",
        "description": "What to call them",
        "source": Source.STATED,
        "source_detail": None,
        "asserted_by": "user",
        "scopes": (),
        "sensitivity": Sensitivity.NORMAL,
        "pinned": False,
        "granted": None,
        "now": EPOCH,
        "entry_cap": ENTRY_CAP,
        "field_cap": FIELD_CAP,
        "pin_cap": PIN_CAP,
        "journal": JOURNAL,
    }
    return await entries.put_field(**{**arguments, **overrides})


async def write_note(entries: SqlEntryStore, **overrides: Any) -> Entry:
    """Append one note, defaulted everywhere a test is not about."""
    arguments: dict[str, Any] = {
        "account_id": ACCOUNT,
        "body": "They mentioned preferring tea to coffee.",
        "note_kind": NoteKind.OBSERVATION,
        "description": "A preference worth remembering",
        "source": Source.OBSERVED,
        "source_detail": None,
        "asserted_by": "user",
        "scopes": (),
        "sensitivity": Sensitivity.NORMAL,
        "pinned": False,
        "now": EPOCH,
        "entry_cap": ENTRY_CAP,
        "pin_cap": PIN_CAP,
        "journal": JOURNAL,
    }
    return await entries.write_note(**{**arguments, **overrides})


async def revise(
    entries: SqlEntryStore, entry: Entry, *, granted: str | None = None, **overrides: Any
) -> Entry:
    """Revise one entry, with everything a test does not care about defaulted."""
    arguments: dict[str, Any] = {
        "account_id": entry.account_id,
        "entry_id": entry.entry_id,
        "granted": granted,
        "now": LATER,
        "asserted_by": entry.asserted_by,
        "pin_cap": PIN_CAP,
        "journal": JOURNAL,
    }
    return await entries.revise(**{**arguments, **overrides})


async def confirm(
    entries: SqlEntryStore, entry: Entry, *, granted: str | None = None, **overrides: Any
) -> Entry:
    arguments: dict[str, Any] = {
        "account_id": entry.account_id,
        "entry_id": entry.entry_id,
        "granted": granted,
        "now": LATER,
        "asserted_by": entry.asserted_by,
        "journal": JOURNAL,
    }
    return await entries.confirm(**{**arguments, **overrides})


async def forget(
    entries: SqlEntryStore, entry: Entry, *, granted: str | None = None, **overrides: Any
) -> Entry:
    arguments: dict[str, Any] = {
        "account_id": entry.account_id,
        "entry_id": entry.entry_id,
        "granted": granted,
        "now": LATER,
        "asserted_by": entry.asserted_by,
        "journal": JOURNAL,
    }
    return await entries.forget(**{**arguments, **overrides})


async def purge(
    entries: SqlEntryStore, database: Database, entry_id: str, *, account_id: str = ACCOUNT
) -> bool:
    """Run the connection-taking purge in a transaction of its own, as erasure does."""
    return await database.transact(
        lambda connection: entries.purge_in(connection, account_id=account_id, entry_id=entry_id)
    )


async def find(
    entries: SqlEntryStore,
    *,
    account_id: str = ACCOUNT,
    granted: str | None = None,
    ordering: Ordering = Ordering.RECENT,
    limit: int = 20,
    cursor: Cursor | None = None,
    **filters: Any,
) -> Page:
    """Search with everything defaulted but the filters a test is about."""
    return await entries.search(
        account_id,
        granted=granted,
        filters=Filters(**filters),
        ordering=ordering,
        limit=limit,
        cursor=cursor,
    )


async def walk(
    entries: SqlEntryStore,
    *,
    ordering: Ordering,
    limit: int,
    cursor: Cursor | None = None,
    **filters: Any,
) -> list[str]:
    """Page through every entry, following the cursors until there are none left."""
    seen: list[str] = []
    while True:
        page = await find(entries, ordering=ordering, limit=limit, cursor=cursor, **filters)
        seen.extend(ids(page.entries))
        if page.next_cursor is None:
            return seen
        cursor = page.next_cursor


def ids(found: Iterable[Entry]) -> list[str]:
    return [entry.entry_id for entry in found]


def keys(found: Iterable[Entry]) -> list[str | None]:
    return [entry.key for entry in found]


async def count_rows(database: Database, sql: str, parameters: Sequence[object] = ()) -> int:
    return await database.count(f"SELECT count(*) AS total FROM {sql}", parameters)  # noqa: S608


class TestPutField:
    async def test_a_new_field_reads_back_exactly_as_it_was_written(
        self, entries: SqlEntryStore
    ) -> None:
        # The returned entry is the one the insert was built from rather than a re-read,
        # so comparing it against a read is what says every column survived the round
        # trip -- including the two that are stored as text and parsed back.
        stored = await write_field(
            entries,
            key="timezone",
            value="Europe/Lisbon",
            description="Where they are",
            source=Source.STATED,
            source_detail="said in conversation",
            asserted_by="user.home",
            scopes=("home",),
            sensitivity=Sensitivity.SENSITIVE,
            pinned=True,
            granted="home",
        )

        assert stored.entry_type is EntryType.FIELD
        assert stored.value == "Europe/Lisbon"
        assert stored.value_type is ValueType.STRING
        assert stored.body is None
        assert stored.note_kind is None
        assert stored.revision == 1
        assert stored.created_at == EPOCH
        assert stored.updated_at == EPOCH
        assert stored.confirmed_at is None
        assert stored.forgotten_at is None
        assert await entries.get(ACCOUNT, stored.entry_id, granted="home") == stored

    async def test_writing_one_key_twice_leaves_one_field_rather_than_two(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        # PUT all the way down: the key is the identity, so a model that retries after a
        # timeout cannot end up with the same fact stored twice.
        first = await write_field(entries, key="timezone", value="Europe/Lisbon")

        second = await write_field(entries, key="timezone", value="Europe/Lisbon")

        assert second.entry_id == first.entry_id
        assert await count_rows(database, "entries WHERE key = 'timezone'") == 1

    async def test_replacing_keeps_created_at_and_bumps_the_revision(
        self, entries: SqlEntryStore
    ) -> None:
        # "Known since" must not jump because a value was corrected.
        first = await write_field(entries, value="Sam")

        replaced = await write_field(entries, value="Samantha", now=LATER)

        assert replaced.created_at == first.created_at
        assert replaced.updated_at == LATER
        assert replaced.revision == 2

    async def test_writing_the_same_value_again_counts_as_confirming_it(
        self, entries: SqlEntryStore
    ) -> None:
        # Writing a value again *is* somebody saying it is still true, so an assistant
        # that re-states what it was told keeps the staleness clock honest without
        # anybody calling confirm.
        await write_field(entries, value="Sam")

        again = await write_field(entries, value="Sam", now=LATER)

        assert again.confirmed_at == LATER

    async def test_writing_a_different_value_clears_the_confirmation_it_had(
        self, entries: SqlEntryStore
    ) -> None:
        # The new value has never been vouched for. Carrying the old confirmation across
        # would report a fact corrected this morning as confirmed last March.
        stored = await write_field(entries, value="Sam")
        await confirm(entries, stored)

        corrected = await write_field(entries, value="Samantha", now=MUCH_LATER)

        assert corrected.confirmed_at is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Sam", ValueType.STRING),
            (42, ValueType.NUMBER),
            (True, ValueType.BOOLEAN),
            (None, ValueType.NULL),
            (["tea", "toast"], ValueType.LIST),
            ({"city": "Lisbon"}, ValueType.OBJECT),
        ],
    )
    async def test_the_value_type_is_derived_from_the_value_and_cannot_be_supplied(
        self, entries: SqlEntryStore, value: object, expected: ValueType
    ) -> None:
        # There is no value_type parameter at all, which is the only arrangement in which
        # the stored pair cannot disagree with itself. A caller-supplied one is a
        # caller-supplied rendering bug.
        assert "value_type" not in inspect.signature(entries.put_field).parameters

        stored = await write_field(entries, value=value)

        assert stored.value_type is expected
        assert await entries.get_field(ACCOUNT, "preferred_name", granted=None) == stored

    async def test_replacing_a_field_replaces_its_scopes_wholesale(
        self, entries: SqlEntryStore
    ) -> None:
        # Not merged: a PUT says what the entry is now, and a scope that survived a write
        # that did not mention it would keep an entry visible to a token the last writer
        # meant to exclude.
        await write_field(entries, scopes=("home", "work"), granted="home")

        replaced = await write_field(entries, scopes=("home",), granted="home")

        assert replaced.scopes == ("home",)
        assert await entries.get(ACCOUNT, replaced.entry_id, granted="work") is None

    async def test_a_write_that_hides_the_entry_from_its_writer_still_returns_it(
        self, entries: SqlEntryStore
    ) -> None:
        # You may always read what you just wrote. Reading the row back through the
        # visibility filter asks a different question from the one the write already
        # answered, and after a write that narrows the scopes it gets a different answer:
        # the store reported "no such entry" for a write that had just succeeded.
        #
        # Refusing this write is the SERVICE's job, and it does refuse it -- a token
        # granting `home` cannot tag an entry `health`, see domain.scopes.check_writable.
        # An authorisation rule belongs in one place, so the store does not keep a second
        # copy of it; it just declines to lose track of its own write.
        await write_field(entries, scopes=("home", "work"), granted="home")

        replaced = await write_field(entries, scopes=("health",), granted="home")

        assert replaced.scopes == ("health",)
        assert replaced.revision == 2
        # And it really is where the write put it: invisible to the token that wrote it.
        assert await entries.get(ACCOUNT, replaced.entry_id, granted="home") is None
        assert await entries.get(ACCOUNT, replaced.entry_id, granted="health") is not None

    async def test_scopes_are_stored_deduplicated_and_in_a_stable_order(
        self, entries: SqlEntryStore
    ) -> None:
        stored = await write_field(entries, scopes=("work", "home", "home"))

        read_back = await entries.get(ACCOUNT, stored.entry_id, granted="home")

        assert read_back is not None
        assert read_back.scopes == ("home", "work")

    async def test_a_replacement_can_pin_a_field_that_was_not_pinned(
        self, entries: SqlEntryStore
    ) -> None:
        await write_field(entries, pinned=False)

        replaced = await write_field(entries, pinned=True, now=LATER)

        assert replaced.pinned is True
        assert ids(await entries.pinned(ACCOUNT, granted=None, limit=10)) == [replaced.entry_id]

    async def test_two_accounts_can_hold_the_same_key_independently(
        self, entries: SqlEntryStore
    ) -> None:
        # The uniqueness of a key is per account. There is no call that could read across
        # accounts, so isolation is not a check anybody has to remember to write.
        mine = await write_field(entries, value="Sam")
        theirs = await write_field(entries, account_id=OTHER_ACCOUNT, value="Alex")

        assert mine.entry_id != theirs.entry_id
        assert await entries.get(ACCOUNT, theirs.entry_id, granted=None) is None
        stored = await entries.get_field(ACCOUNT, "preferred_name", granted=None)
        assert stored is not None
        assert stored.value == "Sam"


class TestScopeConflict:
    async def test_a_key_held_outside_this_tokens_scope_is_refused_not_overwritten(
        self, entries: SqlEntryStore
    ) -> None:
        # Refused and silently overwritten both return quickly, so the value afterwards is
        # the only assertion that tells them apart. A user.home token must not be able to
        # clobber a health-scoped value it cannot read.
        await write_field(entries, key="blood_type", value="O negative", scopes=("health",))

        with pytest.raises(ScopeConflictError):
            await write_field(entries, key="blood_type", value="A positive", granted="home")

        stored = await entries.get_field(ACCOUNT, "blood_type", granted="health")
        assert stored is not None
        assert stored.value == "O negative"
        assert stored.revision == 1

    async def test_a_token_with_no_scope_at_all_is_refused_the_same_way(
        self, entries: SqlEntryStore
    ) -> None:
        # granted binds NULL and `scope = NULL` is never true, so the predicate collapses
        # to "unscoped only" without a second query shape.
        await write_field(entries, key="blood_type", value="O negative", scopes=("health",))

        with pytest.raises(ScopeConflictError):
            await write_field(entries, key="blood_type", value="A positive", granted=None)

    async def test_the_refusal_names_the_key_and_nothing_else_about_the_entry(
        self, entries: SqlEntryStore
    ) -> None:
        # What leaks is that a key is taken. Not its value, and not the scope holding it.
        await write_field(entries, key="blood_type", value="O negative", scopes=("health",))

        with pytest.raises(ScopeConflictError) as refusal:
            await write_field(entries, key="blood_type", value="A positive", granted="home")

        assert "blood_type" in str(refusal.value)
        assert "O negative" not in str(refusal.value)
        assert "health" not in str(refusal.value)

    async def test_a_token_holding_the_scope_replaces_the_field_normally(
        self, entries: SqlEntryStore
    ) -> None:
        await write_field(entries, key="blood_type", value="O negative", scopes=("health",))

        replaced = await write_field(
            entries,
            key="blood_type",
            value="A positive",
            scopes=("health",),
            granted="health",
            now=LATER,
        )

        assert replaced.value == "A positive"
        assert replaced.revision == 2


class TestWriteNote:
    async def test_every_note_is_a_new_note_even_when_it_says_the_same_thing(
        self, entries: SqlEntryStore, database: Database
    ) -> None:
        # Notes have no natural key: two identical observations a week apart are two
        # things that happened, not one thing written twice.
        first = await write_note(entries)

        second = await write_note(entries, now=LATER)

        assert first.entry_id != second.entry_id
        assert await count_rows(database, "entries WHERE entry_type = 'note'") == 2

    @pytest.mark.parametrize("kind", list(NoteKind))
    async def test_a_notes_kind_and_body_round_trip(
        self, entries: SqlEntryStore, kind: NoteKind
    ) -> None:
        written = await write_note(entries, note_kind=kind, body="We went to Lisbon in March.")

        read_back = await entries.get(ACCOUNT, written.entry_id, granted=None)

        assert read_back is not None
        assert read_back.note_kind is kind
        assert read_back.body == "We went to Lisbon in March."
        assert read_back.description == "A preference worth remembering"

    async def test_a_note_has_no_key_and_no_value(self, entries: SqlEntryStore) -> None:
        # The discriminator is a CHECK constraint rather than a convention, so a reader
        # never has to defend against half a field.
        written = await write_note(entries)

        assert written.key is None
        assert written.value is None
        assert written.value_type is None


class TestVisibility:
    """A scoped entry reads back to the wrong token exactly as one that never existed."""

    @pytest.fixture
    async def hidden(self, entries: SqlEntryStore) -> Entry:
        return await write_field(
            entries, key="blood_type", value="O negative", scopes=("health",), pinned=True
        )

    async def test_get_reports_a_scoped_entry_as_absent(
        self, entries: SqlEntryStore, hidden: Entry
    ) -> None:
        hidden_from_home = await entries.get(ACCOUNT, hidden.entry_id, granted="home")

        assert hidden_from_home is None
        assert hidden_from_home == await entries.get(ACCOUNT, new_entry_id(), granted="home")

    async def test_get_field_reports_a_scoped_key_as_absent(
        self, entries: SqlEntryStore, hidden: Entry
    ) -> None:
        hidden_from_home = await entries.get_field(ACCOUNT, "blood_type", granted="home")

        assert hidden_from_home is None
        assert hidden_from_home == await entries.get_field(ACCOUNT, "never_written", granted="home")

    async def test_search_does_not_return_a_scoped_entry(
        self, entries: SqlEntryStore, hidden: Entry
    ) -> None:
        assert (await find(entries, granted="home")).entries == ()
        assert (await find(entries, granted="home", query="negative")).entries == ()

    async def test_the_always_load_set_does_not_return_a_scoped_entry(
        self, entries: SqlEntryStore, hidden: Entry
    ) -> None:
        assert await entries.pinned(ACCOUNT, granted="home", limit=10) == ()

    async def test_describe_does_not_mention_a_scoped_key(
        self, entries: SqlEntryStore, hidden: Entry
    ) -> None:
        # describe returns no values, but a key is a fact about a person too: knowing
        # that somebody has a blood_type is knowing something.
        assert await entries.describe(ACCOUNT, granted="home") == ()

    async def test_counts_do_not_include_a_scoped_entry(
        self, entries: SqlEntryStore, hidden: Entry
    ) -> None:
        assert await entries.counts(ACCOUNT, granted="home") == Counts(
            fields=0, notes=0, pinned=0, forgotten=0
        )
        assert await entries.counts(ACCOUNT, granted="health") == Counts(
            fields=1, notes=0, pinned=1, forgotten=0
        )

    async def test_a_fresh_account_counts_zero_of_everything(self, entries: SqlEntryStore) -> None:
        # sum() over no rows is NULL rather than 0, so an account that has written
        # nothing is the case that turns four counts into four Nones.
        assert await entries.counts(OTHER_ACCOUNT, granted=None) == Counts(
            fields=0, notes=0, pinned=0, forgotten=0
        )

    async def test_the_sql_and_the_domain_predicate_agree_for_every_combination(
        self, entries: SqlEntryStore
    ) -> None:
        """The WHERE clause is the enforcement; ``visible_to`` is the statement of it.

        Asserted against every scope set crossed with every grant, because the two are
        written in different languages in different files and nothing but this keeps a
        change to one from quietly diverging from the other.
        """
        built = [
            await write_field(entries, key=f"field_{index}", scopes=scopes)
            for index, scopes in enumerate(SCOPE_SETS)
        ]

        for granted in GRANTS:
            expected = {entry.entry_id for entry in built if entry.visible_to(granted)}

            page = await find(entries, granted=granted, limit=50)
            assert {entry.entry_id for entry in page.entries} == expected

            for entry in built:
                found = await entries.get(ACCOUNT, entry.entry_id, granted=granted)
                assert (found is not None) == entry.visible_to(granted)


class TestSearch:
    async def test_the_better_match_comes_first(self, entries: SqlEntryStore) -> None:
        # bm25 is why the tokens are joined with OR rather than AND: an entry matching
        # every token outranks one matching a single token, and the near-miss is still
        # returned instead of being dropped for containing one wrong word.
        both = await write_note(entries, body="They prefer tea in the afternoon.")
        one = await write_note(entries, body="They prefer coffee in the morning.")

        page = await find(entries, query="prefer tea")

        assert ids(page.entries) == [both.entry_id, one.entry_id]

    async def test_only_a_ranked_read_carries_a_relevance_score(
        self, entries: SqlEntryStore
    ) -> None:
        await write_note(entries)

        ranked = await find(entries, query="tea")
        ordered = await find(entries)

        assert ranked.entries[0].rank is not None
        assert ordered.entries[0].rank is None

    async def test_a_search_for_preferring_finds_prefer(self, entries: SqlEntryStore) -> None:
        # porter stemming, which is what makes recall work on prose somebody typed
        # months ago in a tense they will not remember choosing.
        written = await write_note(entries, body="They prefer to be asked first.")

        page = await find(entries, query="preferring")

        assert ids(page.entries) == [written.entry_id]

    async def test_a_field_is_found_by_a_word_in_its_key(self, entries: SqlEntryStore) -> None:
        # The word somebody searches for is at least as often in the name of the thing as
        # in the thing. The underscore is indexed as a space, so blood_type is two words.
        written = await write_field(
            entries, key="blood_type", value="O negative", description="Group and rhesus"
        )

        page = await find(entries, query="blood")

        assert ids(page.entries) == [written.entry_id]

    async def test_a_field_is_found_by_a_word_in_its_description(
        self, entries: SqlEntryStore
    ) -> None:
        written = await write_field(
            entries, key="blood_type", value="O negative", description="Group and rhesus"
        )

        page = await find(entries, query="rhesus")

        assert ids(page.entries) == [written.entry_id]

    async def test_a_field_is_found_by_a_word_inside_a_structured_value(
        self, entries: SqlEntryStore
    ) -> None:
        # Structure is dropped and the leaves are kept: nobody searches for braces.
        written = await write_field(entries, key="home", value={"city": "Lisbon"})

        page = await find(entries, query="Lisbon")

        assert ids(page.entries) == [written.entry_id]

    @pytest.mark.parametrize("query", ['"', "*", "()", "   ", "^"])
    async def test_a_query_with_nothing_matchable_is_refused_before_sqlite_sees_it(
        self, entries: SqlEntryStore, query: str
    ) -> None:
        # MATCH takes a query language, not a string, and most punctuation is a syntax
        # error in it. This is the difference between a 422 and a 500 on a read endpoint.
        with pytest.raises(InvalidSearchError):
            await find(entries, query=query)

    @pytest.mark.parametrize("query", ['a"b', "x AND OR y", "NEAR(", "a:b"])
    async def test_fts5_operators_are_matched_as_words_rather_than_obeyed(
        self, entries: SqlEntryStore, query: str
    ) -> None:
        # Each of these raised OperationalError from inside SQLite before the tokens were
        # quoted. A token that happens to spell OR is a word to look for, not an operator.
        written = await write_note(entries, body="a b and or near", description="Punctuation")

        page = await find(entries, query=query)

        assert ids(page.entries) == [written.entry_id]

    async def test_the_fts_index_is_shared_so_the_account_filter_lives_in_the_outer_where(
        self, entries: SqlEntryStore
    ) -> None:
        """One index holds every account's text, and MATCH alone finds other people's rows.

        Account isolation under search is therefore a predicate in the WHERE around the
        MATCH rather than anything the index does, which is the one place in this store
        where isolation could be lost by deleting a line that looks redundant.
        """
        mine = await write_note(entries, body="A holiday in Lisbon.")
        theirs = await write_note(entries, account_id=OTHER_ACCOUNT, body="A holiday in Lisbon.")

        assert ids((await find(entries, query="Lisbon")).entries) == [mine.entry_id]
        assert ids((await find(entries, account_id=OTHER_ACCOUNT, query="Lisbon")).entries) == [
            theirs.entry_id
        ]

    async def test_a_query_matching_nothing_returns_an_empty_page(
        self, entries: SqlEntryStore
    ) -> None:
        await write_note(entries)

        page = await find(entries, query="orthogonal")

        assert page.entries == ()
        assert page.next_cursor is None


class TestFilters:
    async def test_entry_type_narrows_to_one_half_of_the_table(
        self, entries: SqlEntryStore
    ) -> None:
        field = await write_field(entries)
        note = await write_note(entries)

        assert ids((await find(entries, entry_type=EntryType.FIELD)).entries) == [field.entry_id]
        assert ids((await find(entries, entry_type=EntryType.NOTE)).entries) == [note.entry_id]

    async def test_keys_fetches_a_batch_in_one_read(self, entries: SqlEntryStore) -> None:
        await write_field(entries, key="timezone", value="Europe/Lisbon", now=EPOCH)
        await write_field(entries, key="preferred_name", value="Sam", now=LATER)
        await write_field(entries, key="blood_type", value="O negative", now=MUCH_LATER)

        page = await find(entries, keys=("timezone", "blood_type"), ordering=Ordering.OLDEST)

        assert keys(page.entries) == ["timezone", "blood_type"]

    async def test_asking_for_no_keys_at_all_returns_nothing(self, entries: SqlEntryStore) -> None:
        # An empty IN list is legal in SQLite and a syntax error almost everywhere else,
        # so what it does here is worth pinning: an empty batch asks for nothing.
        await write_field(entries)

        assert (await find(entries, keys=())).entries == ()

    async def test_a_key_prefix_containing_an_underscore_is_not_a_wildcard(
        self, entries: SqlEntryStore
    ) -> None:
        # Unescaped, LIKE reads "contact_" as "contact" plus any character, which quietly
        # turns a prefix filter into a filter that matches keys nobody asked for.
        wanted = await write_field(entries, key="contact_email", value="sam@example.com")
        await write_field(entries, key="contactxemail", value="decoy")

        page = await find(entries, key_prefix="contact_")

        assert ids(page.entries) == [wanted.entry_id]

    async def test_a_key_prefix_containing_a_percent_is_not_a_wildcard(
        self, entries: SqlEntryStore
    ) -> None:
        wanted = await write_field(entries, key="50%off", value="a discount code")
        await write_field(entries, key="50xoff", value="decoy")

        page = await find(entries, key_prefix="50%")

        assert ids(page.entries) == [wanted.entry_id]

    async def test_note_kind_narrows_to_one_kind(self, entries: SqlEntryStore) -> None:
        lesson = await write_note(entries, note_kind=NoteKind.LESSON)
        await write_note(entries, note_kind=NoteKind.EPISODE)

        page = await find(entries, note_kind=NoteKind.LESSON)

        assert ids(page.entries) == [lesson.entry_id]

    async def test_source_narrows_to_what_the_writer_claimed(self, entries: SqlEntryStore) -> None:
        # "Show me only what I actually told you" is the query that makes source worth
        # storing, and it only works because the vocabulary is a small fixed set.
        stated = await write_field(entries, key="timezone", source=Source.STATED)
        await write_field(entries, key="commute", source=Source.INFERRED)

        page = await find(entries, source=Source.STATED)

        assert ids(page.entries) == [stated.entry_id]

    async def test_asserted_by_narrows_to_one_token_audience(self, entries: SqlEntryStore) -> None:
        home = await write_field(entries, key="timezone", asserted_by="user.home")
        await write_field(entries, key="commute", asserted_by="user.work")

        page = await find(entries, asserted_by="user.home")

        assert ids(page.entries) == [home.entry_id]

    async def test_sensitivity_narrows_to_the_entries_marked_sensitive(
        self, entries: SqlEntryStore
    ) -> None:
        sensitive = await write_field(entries, key="diagnosis", sensitivity=Sensitivity.SENSITIVE)
        await write_field(entries, key="timezone")

        page = await find(entries, sensitivity=Sensitivity.SENSITIVE)

        assert ids(page.entries) == [sensitive.entry_id]

    async def test_pinned_false_means_only_the_unpinned_ones(self, entries: SqlEntryStore) -> None:
        # Not "do not filter". None is what means that, and the distinction is the reason
        # every field on Filters is optional rather than defaulted to a falsy value.
        await write_field(entries, key="timezone", pinned=True)
        unpinned = await write_field(entries, key="commute", pinned=False)

        page = await find(entries, pinned=False)

        assert ids(page.entries) == [unpinned.entry_id]
        assert len((await find(entries)).entries) == 2

    async def test_scope_narrows_within_what_the_token_already_grants(
        self, entries: SqlEntryStore
    ) -> None:
        work = await write_field(entries, key="commute", scopes=("home", "work"), granted="home")
        await write_field(entries, key="timezone", scopes=("home",), granted="home")

        page = await find(entries, granted="home", scope="work")

        assert ids(page.entries) == [work.entry_id]

    async def test_since_and_until_bracket_the_last_touch(self, entries: SqlEntryStore) -> None:
        early = await write_field(entries, key="timezone", now=EPOCH)
        late = await write_field(entries, key="commute", now=LATER)

        assert ids((await find(entries, since=LATER)).entries) == [late.entry_id]
        assert ids((await find(entries, until=LATER)).entries) == [early.entry_id]

    async def test_a_never_confirmed_entry_is_the_stalest_thing_there_is(
        self, entries: SqlEntryStore
    ) -> None:
        # A plain `confirmed_at < ?` would silently exclude exactly the entries this
        # filter exists to surface, because NULL compares false against everything.
        confirmed = await write_field(entries, key="timezone")
        await confirm(entries, confirmed, now=EPOCH)
        never = await write_field(entries, key="commute")

        page = await find(entries, stale_before=EPOCH)

        assert ids(page.entries) == [never.entry_id]

    async def test_stale_before_also_surfaces_what_was_confirmed_long_enough_ago(
        self, entries: SqlEntryStore
    ) -> None:
        confirmed = await write_field(entries, key="timezone")
        await confirm(entries, confirmed, now=EPOCH)
        never = await write_field(entries, key="commute")

        page = await find(entries, stale_before=LATER, ordering=Ordering.OLDEST)

        # Compared as a SET. Both entries were written at the same instant on the fixed
        # clock, so the ordering falls through created_at to the entry_id tiebreak, which
        # is a random hex string -- and this test is about WHICH entries the filter
        # surfaces, not the order they come back in. Asserting a list here passes or fails
        # on a coin toss.
        assert set(ids(page.entries)) == {confirmed.entry_id, never.entry_id}

    async def test_include_forgotten_is_the_only_way_to_see_a_forgotten_entry(
        self, entries: SqlEntryStore
    ) -> None:
        live = await write_field(entries, key="timezone")
        gone = await write_field(entries, key="commute")
        await forget(entries, gone)

        assert ids((await find(entries)).entries) == [live.entry_id]
        assert set(ids((await find(entries, include_forgotten=True)).entries)) == {
            live.entry_id,
            gone.entry_id,
        }

    async def test_several_filters_narrow_together(self, entries: SqlEntryStore) -> None:
        wanted = await write_field(
            entries,
            key="contact_email",
            value="sam@example.com",
            sensitivity=Sensitivity.SENSITIVE,
            pinned=True,
            source=Source.STATED,
        )
        await write_field(entries, key="contact_phone", pinned=False)
        await write_field(entries, key="timezone", pinned=True, sensitivity=Sensitivity.SENSITIVE)
        await write_note(entries, pinned=True, sensitivity=Sensitivity.SENSITIVE)

        page = await find(
            entries,
            entry_type=EntryType.FIELD,
            key_prefix="contact_",
            sensitivity=Sensitivity.SENSITIVE,
            pinned=True,
            source=Source.STATED,
        )

        assert ids(page.entries) == [wanted.entry_id]

    async def test_a_filter_combines_with_a_query_rather_than_replacing_it(
        self, entries: SqlEntryStore
    ) -> None:
        # The ranked shape and the ordered shape build their WHERE from the same helper,
        # so a filter that worked in a listing has to keep working in a search.
        note = await write_note(entries, body="They prefer tea.")
        await write_field(entries, key="drink", value="tea", description="What they prefer")

        page = await find(entries, query="tea", entry_type=EntryType.NOTE)

        assert ids(page.entries) == [note.entry_id]


class TestPagination:
    async def test_recent_returns_the_most_recently_touched_first(
        self, entries: SqlEntryStore
    ) -> None:
        first = await write_field(entries, key="a", now=EPOCH)
        second = await write_field(entries, key="b", now=LATER)
        third = await write_field(entries, key="c", now=MUCH_LATER)

        page = await find(entries, ordering=Ordering.RECENT)

        assert ids(page.entries) == [third.entry_id, second.entry_id, first.entry_id]

    async def test_oldest_returns_them_in_the_order_they_were_created(
        self, entries: SqlEntryStore
    ) -> None:
        first = await write_field(entries, key="a", now=EPOCH)
        second = await write_field(entries, key="b", now=LATER)
        third = await write_field(entries, key="c", now=MUCH_LATER)

        page = await find(entries, ordering=Ordering.OLDEST)

        assert ids(page.entries) == [first.entry_id, second.entry_id, third.entry_id]

    @pytest.mark.parametrize("ordering", [Ordering.RECENT, Ordering.OLDEST])
    async def test_a_walk_hands_out_every_row_once(
        self, entries: SqlEntryStore, ordering: Ordering
    ) -> None:
        written = [
            await write_field(entries, key=f"k{index}", now=EPOCH + timedelta(hours=index))
            for index in range(4)
        ]

        walked = await walk(entries, ordering=ordering, limit=2)

        assert sorted(walked) == sorted(ids(written))

    async def test_a_relevance_walk_hands_out_every_match_once(
        self, entries: SqlEntryStore
    ) -> None:
        written = [
            await write_note(entries, body=f"They prefer tea, reason {index}.")
            for index in range(3)
        ]

        walked = await walk(entries, ordering=Ordering.RELEVANCE, limit=2, query="prefer tea")

        assert sorted(walked) == sorted(ids(written))

    async def test_the_last_page_has_no_cursor_even_when_it_is_full(
        self, entries: SqlEntryStore
    ) -> None:
        # One row more than asked for is fetched so that "is there a next page" is an
        # observation rather than a guess from whether this page came out full. A caller
        # comparing counts to a limit gets exactly this case wrong, every time.
        for index in range(4):
            await write_field(entries, key=f"k{index}", now=EPOCH + timedelta(hours=index))

        first = await find(entries, ordering=Ordering.OLDEST, limit=2)
        assert first.next_cursor is not None

        second = await find(entries, ordering=Ordering.OLDEST, limit=2, cursor=first.next_cursor)

        assert len(second.entries) == 2
        assert second.next_cursor is None

    async def test_a_cursor_from_another_ordering_is_refused_rather_than_misread(
        self, entries: SqlEntryStore
    ) -> None:
        # The store stamps the ordering into the cursor it issues, and that stamp is the
        # only thing between a bm25 score and an ISO timestamp being compared to each
        # other -- which SQLite does happily and meaninglessly.
        await write_field(entries, key="a", now=EPOCH)
        await write_field(entries, key="b", now=LATER)

        page = await find(entries, ordering=Ordering.RECENT, limit=1)

        assert page.next_cursor is not None
        assert page.next_cursor.ordering is Ordering.RECENT
        with pytest.raises(InvalidCursorError):
            decode_cursor(page.next_cursor.encode(), expected=Ordering.OLDEST)

    async def test_a_relevance_cursor_is_stamped_with_its_own_ordering(
        self, entries: SqlEntryStore
    ) -> None:
        await write_note(entries, body="They prefer tea.")
        await write_note(entries, body="They prefer coffee.")

        page = await find(entries, query="prefer", limit=1)

        assert page.next_cursor is not None
        assert page.next_cursor.ordering is Ordering.RELEVANCE

    async def test_an_oldest_walk_across_a_concurrent_insert_skips_nothing(
        self, entries: SqlEntryStore
    ) -> None:
        """``created_at`` never changes, so a row cannot move out from under a walk.

        This is the guarantee export is built on, and the reason OFFSET is not used
        anywhere: a row inserted above your position shifts every later row down, and the
        page after it repeats one row and drops another.
        """
        original = [
            await write_field(entries, key=f"k{index}", now=EPOCH + timedelta(hours=index))
            for index in range(4)
        ]

        first = await find(entries, ordering=Ordering.OLDEST, limit=2)
        arrived = await write_field(entries, key="late_arrival", now=MUCH_LATER)
        rest = await walk(entries, ordering=Ordering.OLDEST, limit=2, cursor=first.next_cursor)

        walked = ids(first.entries) + rest
        assert len(walked) == len(set(walked))
        assert set(ids(original)) <= set(walked)
        assert arrived.entry_id in walked


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


class TestDescribe:
    async def test_describe_returns_every_live_key_with_no_values(
        self, entries: SqlEntryStore
    ) -> None:
        # The endpoint exists to be cheap enough to call BEFORE inventing a key. One that
        # returned values would be a full export wearing a different name.
        stored = await write_field(
            entries,
            key="timezone",
            value="Europe/Lisbon",
            description="Where they are",
            scopes=("home",),
            pinned=True,
            granted="home",
        )
        await confirm(entries, stored, granted="home", now=LATER)

        described = await entries.describe(ACCOUNT, granted="home")

        assert described == (
            FieldSummary(
                key="timezone",
                description="Where they are",
                value_type="string",
                updated_at=EPOCH,
                confirmed_at=LATER,
                scopes=("home",),
                pinned=True,
            ),
        )

    async def test_describe_is_ordered_by_key_and_ignores_notes(
        self, entries: SqlEntryStore
    ) -> None:
        await write_field(entries, key="timezone")
        await write_field(entries, key="blood_type")
        await write_note(entries)

        described = await entries.describe(ACCOUNT, granted=None)

        assert [summary.key for summary in described] == ["blood_type", "timezone"]


class TestPinned:
    async def test_the_always_load_set_is_most_recently_touched_first(
        self, entries: SqlEntryStore
    ) -> None:
        first = await write_field(entries, key="a", pinned=True, now=EPOCH)
        second = await write_field(entries, key="b", pinned=True, now=LATER)

        assert ids(await entries.pinned(ACCOUNT, granted=None, limit=10)) == [
            second.entry_id,
            first.entry_id,
        ]

    async def test_the_always_load_set_is_capped_rather_than_unbounded(
        self, entries: SqlEntryStore
    ) -> None:
        # Read at the start of every conversation and put straight into a context window,
        # so the limit is a token budget rather than a pagination detail.
        await write_field(entries, key="a", pinned=True, now=EPOCH)
        newest = await write_field(entries, key="b", pinned=True, now=LATER)

        assert ids(await entries.pinned(ACCOUNT, granted=None, limit=1)) == [newest.entry_id]

    async def test_unpinned_entries_are_not_in_it(self, entries: SqlEntryStore) -> None:
        await write_field(entries, key="a", pinned=False)

        assert await entries.pinned(ACCOUNT, granted=None, limit=10) == ()


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
