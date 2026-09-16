"""Search, filters and pagination against the entry SQL store.

**Nothing here sleeps.** :data:`~tests.fakes.clock.EPOCH` and offsets from it are passed
in as ``now``, so an entry confirmed an hour ago is a parameter rather than a wait.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.fakes.clock import EPOCH
from tests.unit.entries._helpers import (
    LATER,
    MUCH_LATER,
    confirm,
    find,
    forget,
    ids,
    keys,
    walk,
    write_field,
    write_note,
)
from user_api.domain.cursors import Ordering, decode_cursor
from user_api.domain.entries import EntryType, NoteKind, Sensitivity, Source
from user_api.domain.errors import InvalidCursorError, InvalidSearchError
from user_api.entries.store import FieldSummary

if TYPE_CHECKING:
    from user_api.entries.sql_store import SqlEntryStore


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
