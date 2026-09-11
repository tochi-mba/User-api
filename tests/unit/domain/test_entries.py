"""What an entry is: one type wearing two hats, and the rule about who may see it.

:meth:`Entry.visible_to` is the statement of the scope rule, and the SQL in the entry
store is its enforcement. Both exist on purpose: a boundary that lives only in a WHERE
clause is a boundary nobody can read, and this is the version a person can check by eye.
"""

from __future__ import annotations

import string
from dataclasses import replace
from typing import Any

import pytest

from tests.fakes.clock import EPOCH
from user_api.domain.entries import (
    MAX_DESCRIPTION_CHARS,
    Action,
    Entry,
    EntryType,
    NoteKind,
    Sensitivity,
    Source,
    ValueType,
    new_entry_id,
    validate_description,
    validate_note_body,
)
from user_api.domain.errors import InvalidDescriptionError, InvalidNoteError

MAX_NOTE_CHARS = 2_000
SECRET = "They are seeing a consultant about their heart."
"""Stand-in for the personal data in a rejected note. A refusal that echoed it would put
it in a 422 body and a log line.
"""


def make_entry(**overrides: Any) -> Entry:
    defaults: dict[str, Any] = {
        "entry_id": "8d5f2c1b9a7e4d3c2b1a0f9e8d7c6b5a",
        "account_id": "account-a",
        "entry_type": EntryType.NOTE,
        "description": "A preference worth remembering",
        "source": Source.STATED,
        "asserted_by": "assistant-1",
        "created_at": EPOCH,
        "updated_at": EPOCH,
        "body": "They mentioned preferring tea to coffee.",
        "note_kind": NoteKind.OBSERVATION,
    }
    return Entry(**{**defaults, **overrides})


class TestVisibility:
    def test_an_unrestricted_entry_is_visible_to_a_token_holding_no_scope(self) -> None:
        assert make_entry(scopes=()).visible_to(None)

    def test_an_unrestricted_entry_is_visible_to_a_token_holding_a_scope(self) -> None:
        # Holding a scope widens what a token can see. It never narrows it, or an
        # assistant given the health scope would stop knowing the person's name.
        assert make_entry(scopes=()).visible_to("health")

    def test_a_scoped_entry_is_invisible_to_a_token_holding_no_scope(self) -> None:
        assert not make_entry(scopes=("health",)).visible_to(None)

    def test_a_scoped_entry_is_invisible_to_a_token_holding_a_different_scope(self) -> None:
        # The compartment boundary. A home assistant holding a perfectly valid token
        # sees nothing of what the person told their health assistant.
        assert not make_entry(scopes=("health",)).visible_to("home")

    def test_a_scoped_entry_is_visible_to_a_token_holding_that_scope(self) -> None:
        assert make_entry(scopes=("health",)).visible_to("health")

    @pytest.mark.parametrize("granted", ["home", "health"])
    def test_an_entry_in_two_compartments_is_visible_from_either(self, granted: str) -> None:
        assert make_entry(scopes=("home", "health")).visible_to(granted)

    def test_a_scope_that_merely_looks_similar_does_not_match(self) -> None:
        assert not make_entry(scopes=("health",)).visible_to("healthcare")


class TestRestriction:
    def test_an_entry_with_no_scopes_is_not_restricted(self) -> None:
        assert make_entry(scopes=()).is_restricted is False

    def test_an_entry_with_a_scope_is_restricted(self) -> None:
        assert make_entry(scopes=("health",)).is_restricted is True


class TestForgetting:
    def test_a_live_entry_is_not_forgotten(self) -> None:
        assert make_entry(forgotten_at=None).is_forgotten is False

    def test_an_entry_with_a_forgotten_timestamp_is_forgotten(self) -> None:
        # Forgotten is a fact about the row, not about the sweeper: the entry is filtered
        # out of every read path the moment this is set, whatever happens to the bytes.
        assert make_entry(forgotten_at=EPOCH).is_forgotten is True


class TestEntryIds:
    def test_two_ids_are_never_the_same(self) -> None:
        assert len({new_entry_id() for _ in range(1_000)}) == 1_000

    def test_an_id_is_opaque_hex_rather_than_a_counter(self) -> None:
        # An entry id appears in URLs and in an assistant's transcript. A sequential one
        # would say how much this person has told us and roughly when, which is not much,
        # but it is not nothing and it is free to avoid.
        entry_id = new_entry_id()

        assert len(entry_id) == 32
        assert set(entry_id) <= set(string.hexdigits.lower())

    def test_consecutive_ids_do_not_run_in_sequence(self) -> None:
        first, second = new_entry_id(), new_entry_id()

        assert first[:8] != second[:8]


class TestEquality:
    def test_a_searched_entry_equals_the_same_entry_fetched_by_id(self) -> None:
        # `rank` is excluded from equality because two reads of the same entry are the
        # same entry whether or not one of them arrived through a search. Without that,
        # every test comparing a searched entry to a fetched one would fail for a reason
        # that has nothing to do with what it is testing.
        fetched = make_entry()
        searched = replace(fetched, rank=-1.2345)

        assert searched == fetched
        assert searched.rank == -1.2345

    def test_a_searched_entry_hashes_like_the_fetched_one(self) -> None:
        # So a set built from a search page and a fetch does not hold the same entry
        # twice.
        fetched = make_entry()

        assert hash(replace(fetched, rank=-1.0)) == hash(fetched)

    def test_a_field_that_really_did_change_still_breaks_equality(self) -> None:
        # The rank exclusion is narrow, and this is what says so.
        fetched = make_entry()

        assert replace(fetched, revision=2) != fetched

    def test_an_entry_cannot_be_tweaked_in_place(self) -> None:
        # An entry that came out of the store is a snapshot of a row. Code that "just
        # tweaks" one before writing it back is code that writes back stale neighbours.
        entry = make_entry()

        with pytest.raises(AttributeError):
            entry.description = "something else"  # type: ignore[misc]


class TestClosedVocabularies:
    @pytest.mark.parametrize(
        ("member", "spelling"),
        [
            (EntryType.FIELD, "field"),
            (NoteKind.LESSON, "lesson"),
            (ValueType.BOOLEAN, "boolean"),
            (Sensitivity.SENSITIVE, "sensitive"),
            (Source.INFERRED, "inferred"),
            (Action.ENTRY_FORGOTTEN, "entry.forgotten"),
        ],
    )
    def test_a_vocabulary_member_is_the_string_it_is_stored_as(
        self, member: str, spelling: str
    ) -> None:
        # These are bound straight into SQL and rendered straight into JSON. A free
        # string would be a typo nobody could ever filter for again; a StrEnum that did
        # not compare equal to its spelling would be a migration.
        assert member == spelling


class TestDescriptions:
    def test_a_description_comes_back_trimmed(self) -> None:
        assert validate_description("  What to call them  ") == "What to call them"

    @pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
    def test_a_missing_description_is_refused(self, raw: str) -> None:
        # Required on create, which is the whole anti-sprawl mechanism on the read side:
        # the schema endpoint is only worth calling before inventing a key if what it
        # returns says what each key means.
        with pytest.raises(InvalidDescriptionError, match="required"):
            validate_description(raw)

    def test_a_description_at_the_limit_is_accepted(self) -> None:
        assert len(validate_description("d" * MAX_DESCRIPTION_CHARS)) == MAX_DESCRIPTION_CHARS

    def test_a_description_past_the_limit_is_refused(self) -> None:
        # The cap keeps `describe_schema` cheap enough to call before inventing a key,
        # which it only is if its response cannot grow without bound.
        with pytest.raises(InvalidDescriptionError, match="at most 200 characters"):
            validate_description("d" * (MAX_DESCRIPTION_CHARS + 1))

    def test_the_limit_is_measured_after_trimming(self) -> None:
        raw = f"  {'d' * MAX_DESCRIPTION_CHARS}  "

        assert len(validate_description(raw)) == MAX_DESCRIPTION_CHARS

    @pytest.mark.parametrize("raw", ["", SECRET * 20])
    def test_a_refusal_does_not_repeat_the_description(self, raw: str) -> None:
        with pytest.raises(InvalidDescriptionError) as caught:
            validate_description(raw)

        assert SECRET not in str(caught.value)


class TestNoteBodies:
    def test_a_body_comes_back_trimmed(self) -> None:
        assert validate_note_body("  They prefer tea.  ", max_chars=MAX_NOTE_CHARS) == (
            "They prefer tea."
        )

    @pytest.mark.parametrize("raw", ["", "   ", "\n"])
    def test_a_note_with_no_body_is_refused(self, raw: str) -> None:
        with pytest.raises(InvalidNoteError, match="needs a body"):
            validate_note_body(raw, max_chars=MAX_NOTE_CHARS)

    def test_a_body_at_the_limit_is_accepted(self) -> None:
        assert len(validate_note_body("b" * 50, max_chars=50)) == 50

    def test_a_body_past_the_limit_is_refused_rather_than_truncated(self) -> None:
        # A note cut off mid-sentence says something other than what somebody wrote,
        # which is worse than no note at all.
        with pytest.raises(InvalidNoteError, match="at most 50 characters"):
            validate_note_body("b" * 51, max_chars=50)

    def test_the_limit_is_measured_after_trimming(self) -> None:
        assert len(validate_note_body(f"  {'b' * 50}  ", max_chars=50)) == 50

    def test_a_refusal_does_not_repeat_the_note(self) -> None:
        with pytest.raises(InvalidNoteError) as caught:
            validate_note_body(SECRET, max_chars=10)

        assert SECRET not in str(caught.value)
