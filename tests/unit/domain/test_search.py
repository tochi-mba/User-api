"""Turning what a person typed into something FTS5 will accept.

The table in this file is the hostile-query table from the module docstring, plus the
punctuation that turned up alongside it. Every row asserts one of exactly two outcomes: a
``MATCH`` expression, or an :class:`InvalidSearchError`. There is no third outcome, and
the reason there is no third outcome is that the third outcome is a
``sqlite3.OperationalError`` raised from inside a read endpoint, which is a 500.

So the assertion that matters most is not on the strings. It is
:meth:`TestAgainstRealFts5.test_every_expression_this_builds_is_accepted_by_fts5`, which
runs each produced expression against a real FTS5 table built the same way the migration
builds it.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from user_api.domain.errors import InvalidSearchError
from user_api.domain.search import MAX_QUERY_CHARS, MAX_TOKENS, to_match_query

if TYPE_CHECKING:
    from collections.abc import Iterator

HOSTILE: list[tuple[str, str | None]] = [
    ('"', None),
    ('a"b', '"a" OR "b"'),
    ("x AND OR y", '"x" OR "AND" OR "OR" OR "y"'),
    ("*", None),
    ("NEAR(", '"NEAR"'),
    ("^", None),
    ("-", None),
    ("()", None),
    ("", None),
    ("   ", None),
    ("a:b", '"a" OR "b"'),
    ("AND", '"AND"'),
    ('"unclosed', '"unclosed"'),
    ("col:*", '"col"'),
    ("x NEAR(y z)", '"x" OR "NEAR" OR "y" OR "z"'),
    ('sam "the boss" okonkwo', '"sam" OR "the" OR "boss" OR "okonkwo"'),
    ("{}", None),
    ("- -", None),
    ("tea*", '"tea"'),
    ("NOT tea", '"NOT" OR "tea"'),
]
"""What a person types, and what it must become. ``None`` means a clean refusal."""

BUILDABLE = [(raw, expected) for raw, expected in HOSTILE if expected is not None]
REFUSED = [raw for raw, expected in HOSTILE if expected is None]

CORPUS = [
    "They prefer tea to coffee and lived in Lisbon in March.",
    "Allergic to shellfish, and will say so before anybody asks.",
]


def raises_a_syntax_error(connection: sqlite3.Connection, query: str) -> bool:
    """Whether FTS5 refuses this string outright.

    A free function rather than a try/except in a loop, so the tests below can talk about
    *how many* of the hostile queries break, which is the measurement the module records.
    """
    try:
        connection.execute("SELECT rowid FROM notes WHERE notes MATCH ?", (query,)).fetchall()
    except sqlite3.OperationalError:
        return True
    return False


class TestBuildingAnExpression:
    @pytest.mark.parametrize(("raw", "expected"), HOSTILE)
    def test_a_query_becomes_an_expression_or_a_refusal_and_nothing_else(
        self, raw: str, expected: str | None
    ) -> None:
        if expected is None:
            with pytest.raises(InvalidSearchError, match="at least one letter or digit"):
                to_match_query(raw)
        else:
            assert to_match_query(raw) == expected

    def test_a_token_is_quoted_so_an_operator_word_is_a_word(self) -> None:
        # Inside double quotes FTS5 reads a token as a literal phrase. Without them a
        # person searching for what somebody said about an AND gate is writing syntax.
        assert to_match_query("AND") == '"AND"'
        assert to_match_query("NEAR") == '"NEAR"'

    def test_tokens_are_joined_with_or_so_a_near_miss_still_returns_something(self) -> None:
        # bm25 does the ranking, so every token matching outranks one token matching.
        # AND would return nothing for a query with one wrong word in it, and a query
        # with one wrong word in it is the normal case for half-remembered prose.
        assert to_match_query("tea lisbon") == '"tea" OR "lisbon"'

    def test_an_underscore_splits_a_field_key_into_the_words_it_is_made_of(self) -> None:
        # Somebody typing a field key is hoping to find the prose that says the same
        # thing in words.
        assert to_match_query("preferred_name") == '"preferred" OR "name"'

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("café", '"café"'),
            ("Ελλάδα", '"Ελλάδα"'),
            ("お茶", '"お茶"'),
            ("naïve résumé", '"naïve" OR "résumé"'),
        ],
    )
    def test_a_word_outside_the_latin_alphabet_survives_tokenising(
        self, raw: str, expected: str
    ) -> None:
        # The name somebody actually goes by is frequently not spelled in ASCII, and a
        # tokeniser that dropped it would refuse the query as having no word characters.
        assert to_match_query(raw) == expected


class TestLimits:
    def test_a_query_at_the_character_limit_is_accepted(self) -> None:
        assert to_match_query("a" * MAX_QUERY_CHARS) == f'"{"a" * MAX_QUERY_CHARS}"'

    def test_a_query_past_the_character_limit_is_refused(self) -> None:
        with pytest.raises(InvalidSearchError, match="at most 200 characters"):
            to_match_query("a" * (MAX_QUERY_CHARS + 1))

    def test_a_query_of_more_tokens_than_the_cap_is_truncated_rather_than_refused(self) -> None:
        # More tokens than this is a paste rather than a query, and each one costs an
        # index walk. Truncating keeps the paste working; refusing it would not.
        raw = " ".join(f"word{index}" for index in range(MAX_TOKENS + 5))

        expression = to_match_query(raw)

        assert expression.count(" OR ") == MAX_TOKENS - 1
        assert '"word0"' in expression
        assert '"word16"' not in expression


class TestAgainstRealFts5:
    @pytest.fixture
    def notes(self) -> Iterator[sqlite3.Connection]:
        """A real FTS5 table, tokenised the way the migration tokenises the real one."""
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE VIRTUAL TABLE notes USING fts5(body, tokenize='porter unicode61')"
        )
        connection.executemany("INSERT INTO notes(body) VALUES (?)", [(row,) for row in CORPUS])
        try:
            yield connection
        finally:
            connection.close()

    @pytest.mark.parametrize(("raw", "expected"), BUILDABLE)
    def test_every_expression_this_builds_is_accepted_by_fts5(
        self, notes: sqlite3.Connection, raw: str, expected: str
    ) -> None:
        # The assertion the whole module exists for. Everything else in this file is a
        # statement about strings; this is the one that would have caught the eight of
        # ten plausible queries that raised before the module was written.
        assert not raises_a_syntax_error(notes, to_match_query(raw))
        assert to_match_query(raw) == expected

    def test_the_same_queries_break_fts5_when_handed_to_it_untouched(
        self, notes: sqlite3.Connection
    ) -> None:
        # The measurement, re-taken. If this ever stops failing, FTS5 has changed its
        # query language and the trade this module makes is worth revisiting.
        broken = [raw for raw, _ in HOSTILE if raises_a_syntax_error(notes, raw)]

        assert len(broken) >= 8

    def test_none_of_the_queries_this_refuses_would_have_worked_anyway(
        self, notes: sqlite3.Connection
    ) -> None:
        # The refusals are not lost functionality: every one of them is punctuation FTS5
        # would have rejected too, turned from a 500 into a 422.
        assert [raw for raw in REFUSED if not raises_a_syntax_error(notes, raw)] == []

    def test_an_operator_word_matches_the_word_rather_than_operating(
        self, notes: sqlite3.Connection
    ) -> None:
        rows = notes.execute(
            "SELECT rowid FROM notes WHERE notes MATCH ?", (to_match_query("AND"),)
        ).fetchall()

        assert len(rows) == 2

    def test_a_query_where_only_one_word_is_right_still_finds_the_row(
        self, notes: sqlite3.Connection
    ) -> None:
        # Recall on half-remembered prose is the reason for OR, and this is what it buys.
        rows = notes.execute(
            "SELECT rowid FROM notes WHERE notes MATCH ?", (to_match_query("tea in porto"),)
        ).fetchall()

        assert len(rows) == 1

    def test_a_quoted_token_cannot_reopen_the_query_language(
        self, notes: sqlite3.Connection
    ) -> None:
        # The injection shape: a caller trying to close the quote the builder opened and
        # append an operator of their own.
        rows = notes.execute(
            "SELECT rowid FROM notes WHERE notes MATCH ?",
            (to_match_query('tea" OR body:"'),),
        ).fetchall()

        assert len(rows) == 1
