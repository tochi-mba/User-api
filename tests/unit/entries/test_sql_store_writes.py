"""Writes against the entry SQL store: fields, notes, and the visibility boundary.

**Visibility is enforced in SQL and stated in the domain.** ``_VISIBLE`` is the
enforcement and :meth:`~user_api.domain.entries.Entry.visible_to` is the statement of the
rule, so :class:`TestVisibility` builds every combination of scopes and grant and asserts
the two agree. A boundary that exists only as a WHERE clause is a boundary nobody can
read, and one that exists only as a predicate nothing calls is not a boundary at all.

**Nothing here sleeps.** :data:`~tests.fakes.clock.EPOCH` and offsets from it are passed
in as ``now``, so an entry confirmed an hour ago is a parameter rather than a wait.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.fakes.clock import EPOCH
from tests.unit.entries._helpers import (
    GRANTS,
    LATER,
    MUCH_LATER,
    SCOPE_SETS,
    confirm,
    count_rows,
    find,
    ids,
    write_field,
    write_note,
)
from user_api.domain.entries import (
    Entry,
    EntryType,
    NoteKind,
    Sensitivity,
    Source,
    ValueType,
    new_entry_id,
)
from user_api.domain.errors import ScopeConflictError
from user_api.entries.store import Counts

if TYPE_CHECKING:
    from user_api.entries.sql_store import SqlEntryStore
    from user_api.storage.database import Database


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
