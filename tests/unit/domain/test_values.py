"""What may be stored in a field, and what the refusal is allowed to say.

Two properties run through the whole file. A value's type is *derived* rather than
accepted, so a stored pair cannot disagree with itself; and a refusal names the rule that
failed and never the value, because the message ends up in a 422 body and in a log line
and the value is the thing that must not be in either.
"""

from __future__ import annotations

from typing import Any

import pytest

from user_api.domain.entries import ValueType
from user_api.domain.errors import InvalidValueError
from user_api.domain.values import (
    MAX_LIST_ITEMS,
    MAX_OBJECT_KEYS,
    derive_value_type,
    searchable_text,
    validate_value,
)

GENEROUS_BYTES = 100_000
GENEROUS_DEPTH = 10
"""Limits a test that is not about them cannot trip by accident."""

SECRET = "Flat 3, 14 Rua das Flores"
"""A stand-in for the personal data in a rejected value. If it turns up in a message it
has turned up in a log aggregator.
"""


class Unstorable:
    """Not JSON, and its repr is the sort of thing a field holds."""

    def __repr__(self) -> str:
        return SECRET


class TestDerivingTheType:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Lisbon", ValueType.STRING),
            ("", ValueType.STRING),
            (42, ValueType.NUMBER),
            (0, ValueType.NUMBER),
            (-1.5, ValueType.NUMBER),
            (None, ValueType.NULL),
            (["tea", "coffee"], ValueType.LIST),
            ([], ValueType.LIST),
            ({"city": "Lisbon"}, ValueType.OBJECT),
            ({}, ValueType.OBJECT),
        ],
    )
    def test_a_value_reports_the_type_it_actually_is(
        self, value: object, expected: ValueType
    ) -> None:
        assert derive_value_type(value) == expected

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_is_a_boolean_and_not_a_number(self, value: bool) -> None:
        # In Python `True` *is* an `int`, so a type check in the obvious order reports a
        # boolean field as a number -- and a consumer switching on `value_type` then
        # renders "they are vegetarian" as `1`.
        assert derive_value_type(value) == ValueType.BOOLEAN

    def test_a_number_that_happens_to_equal_one_is_still_a_number(self) -> None:
        assert derive_value_type(1) == ValueType.NUMBER

    @pytest.mark.parametrize("value", [object(), {"a", "b"}, b"bytes", (1, 2), Unstorable()])
    def test_a_value_that_is_not_json_at_all_is_refused(self, value: object) -> None:
        with pytest.raises(InvalidValueError, match="must be JSON"):
            derive_value_type(value)

    def test_the_refusal_names_the_type_and_not_the_value(self) -> None:
        with pytest.raises(InvalidValueError) as caught:
            derive_value_type(Unstorable())

        assert "Unstorable" in str(caught.value)
        assert SECRET not in str(caught.value)


class TestValidation:
    def test_validation_hands_back_the_type_so_nobody_derives_it_twice(self) -> None:
        # Two passes are two things that can disagree after somebody edits one of them.
        value_type = validate_value(
            ["tea", "coffee"], max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH
        )

        assert value_type == ValueType.LIST

    @pytest.mark.parametrize(
        "value",
        [
            "Sam",
            42,
            True,
            None,
            ["tea", "coffee"],
            {"home": {"city": "Lisbon"}},
            [{"city": "Lisbon"}, {"city": "Porto"}],
        ],
    )
    def test_an_ordinary_fact_about_a_person_is_accepted(self, value: object) -> None:
        validate_value(value, max_bytes=GENEROUS_BYTES, max_depth=3)


class TestSizeLimit:
    def test_a_value_over_the_byte_limit_is_refused(self) -> None:
        with pytest.raises(InvalidValueError, match="at most 32 bytes"):
            validate_value("x" * 64, max_bytes=32, max_depth=GENEROUS_DEPTH)

    def test_a_value_at_the_byte_limit_is_accepted(self) -> None:
        # Ten characters plus the two quotes json.dumps puts round them.
        validate_value("x" * 10, max_bytes=12, max_depth=GENEROUS_DEPTH)

    def test_the_limit_counts_bytes_rather_than_characters(self) -> None:
        # Otherwise a name written in a non-Latin script silently gets half the budget of
        # the same name in English, which is the kind of limit nobody can explain.
        with pytest.raises(InvalidValueError, match="at most 12 bytes"):
            validate_value("é" * 10, max_bytes=12, max_depth=GENEROUS_DEPTH)

    def test_the_refusal_reports_the_size_but_not_the_value(self) -> None:
        with pytest.raises(InvalidValueError) as caught:
            validate_value(SECRET * 10, max_bytes=32, max_depth=GENEROUS_DEPTH)

        assert "bytes" in str(caught.value)
        assert SECRET not in str(caught.value)


class TestDepthLimit:
    def test_a_structure_at_the_depth_limit_is_accepted(self) -> None:
        validate_value({"home": {"city": "Lisbon"}}, max_bytes=GENEROUS_BYTES, max_depth=3)

    def test_an_object_nested_past_the_limit_is_refused(self) -> None:
        # A deeply nested value is a caller using this service as a document store: it
        # cannot be rendered into a prompt, its text is buried from search, and the schema
        # endpoint can say nothing about it beyond "object".
        with pytest.raises(InvalidValueError, match="at most 3 levels"):
            validate_value(
                {"a": {"b": {"c": {"d": "too deep"}}}}, max_bytes=GENEROUS_BYTES, max_depth=3
            )

    def test_a_list_nested_past_the_limit_is_refused_the_same_way(self) -> None:
        with pytest.raises(InvalidValueError, match="at most 2 levels"):
            validate_value([[["too deep"]]], max_bytes=GENEROUS_BYTES, max_depth=2)

    def test_depth_is_counted_through_lists_and_objects_alike(self) -> None:
        with pytest.raises(InvalidValueError, match="at most 3 levels"):
            validate_value(
                {"trips": [{"places": ["Lisbon"]}]}, max_bytes=GENEROUS_BYTES, max_depth=3
            )

    def test_the_refusal_reports_the_depth_but_not_the_value(self) -> None:
        with pytest.raises(InvalidValueError) as caught:
            validate_value({"a": {"b": {SECRET: 1}}}, max_bytes=GENEROUS_BYTES, max_depth=2)

        assert SECRET not in str(caught.value)


class TestBreadthLimits:
    def test_a_list_at_the_item_limit_is_accepted(self) -> None:
        validate_value(["x"] * MAX_LIST_ITEMS, max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH)

    def test_a_list_past_the_item_limit_is_refused(self) -> None:
        with pytest.raises(InvalidValueError, match="at most 100 items"):
            validate_value(
                ["x"] * (MAX_LIST_ITEMS + 1), max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH
            )

    def test_an_object_at_the_key_limit_is_accepted(self) -> None:
        value = {f"k{index}": index for index in range(MAX_OBJECT_KEYS)}

        validate_value(value, max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH)

    def test_an_object_past_the_key_limit_is_refused(self) -> None:
        value = {f"k{index}": index for index in range(MAX_OBJECT_KEYS + 1)}

        with pytest.raises(InvalidValueError, match="at most 50 keys"):
            validate_value(value, max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH)

    def test_breadth_is_checked_at_every_depth_and_not_only_at_the_top(self) -> None:
        # A data dump hidden one level down is still a data dump.
        value = {"tags": ["x"] * (MAX_LIST_ITEMS + 1)}

        with pytest.raises(InvalidValueError, match="at most 100 items"):
            validate_value(value, max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH)

    def test_an_object_inside_a_list_is_checked_too(self) -> None:
        value = [{f"k{index}": index for index in range(MAX_OBJECT_KEYS + 1)}]

        with pytest.raises(InvalidValueError, match="at most 50 keys"):
            validate_value(value, max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH)

    def test_an_object_with_a_key_that_is_not_a_string_is_refused(self) -> None:
        # json.dumps would coerce `1` to `"1"` and store something the caller did not
        # write, and the next read would hand back a key that was never set.
        value: dict[Any, str] = {1: "yes"}

        with pytest.raises(InvalidValueError, match="string keys"):
            validate_value(value, max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH)

    def test_the_refusal_reports_the_rule_but_not_the_members(self) -> None:
        with pytest.raises(InvalidValueError) as caught:
            validate_value(
                [SECRET] * (MAX_LIST_ITEMS + 1),
                max_bytes=GENEROUS_BYTES,
                max_depth=GENEROUS_DEPTH,
            )

        assert SECRET not in str(caught.value)


class TestValuesJsonCannotRepresent:
    def test_a_leaf_that_is_not_json_is_refused_even_inside_a_legal_container(self) -> None:
        # The top-level type check only sees the list. Everything below it reaches
        # json.dumps, which is the last thing standing between a set and a write.
        with pytest.raises(InvalidValueError, match="JSON-serializable"):
            validate_value(
                [{"tags": {"a", "b"}}], max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH
            )

    def test_the_refusal_for_an_unserializable_leaf_does_not_echo_it(self) -> None:
        with pytest.raises(InvalidValueError) as caught:
            validate_value([Unstorable()], max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH)

        assert SECRET not in str(caught.value)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_float_json_cannot_represent_is_refused_before_it_reaches_the_store(
        self, value: float
    ) -> None:
        """NaN and the infinities are floats, and are not JSON.

        ``json.dumps`` renders them as the bare tokens ``NaN`` and ``Infinity`` unless it
        is told not to, and Python reads those back happily -- so a value stored this way
        looks correct from inside this service and breaks in every other reader, starting
        with the response serializer, which refuses them. This is the arm of the
        try/except that the comment above it always believed it was catching.
        """
        with pytest.raises(InvalidValueError):
            validate_value(value, max_bytes=GENEROUS_BYTES, max_depth=GENEROUS_DEPTH)


class TestSearchableText:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Lisbon", "Lisbon"),
            ("", ""),
            (42, "42"),
            (-1.5, "-1.5"),
        ],
    )
    def test_a_scalar_indexes_as_itself(self, value: object, expected: str) -> None:
        assert searchable_text(value) == expected

    @pytest.mark.parametrize("value", [True, False, None])
    def test_a_boolean_or_a_null_contributes_nothing(self, value: object) -> None:
        # `true` is not a word anybody searches for, and indexing it would make every
        # boolean field in the account a hit for it.
        assert searchable_text(value) == ""

    def test_a_list_indexes_its_items_and_drops_the_brackets(self) -> None:
        assert searchable_text(["tea", "coffee"]) == "tea coffee"

    def test_a_list_drops_the_members_that_index_as_nothing(self) -> None:
        # Otherwise the join leaves double spaces where the booleans were.
        assert searchable_text(["tea", True, None, "coffee"]) == "tea coffee"

    def test_a_nested_list_flattens(self) -> None:
        assert searchable_text([["tea", "coffee"], ["cake"]]) == "tea coffee cake"

    def test_an_object_indexes_its_keys_as_well_as_its_values(self) -> None:
        # One of "city" and "Lisbon" is what somebody types, and the module cannot know
        # which, so both are in the index.
        assert searchable_text({"city": "Lisbon"}) == "city Lisbon"

    def test_a_key_whose_value_indexes_as_nothing_still_contributes_the_key(self) -> None:
        assert searchable_text({"vegetarian": True}) == "vegetarian"

    def test_structure_is_dropped_and_the_leaves_are_kept(self) -> None:
        # Searching for "Lisbon" should find {"home": {"city": "Lisbon"}}, and nobody
        # wants to match on braces.
        assert searchable_text({"home": {"city": "Lisbon"}}) == "home city Lisbon"

    @pytest.mark.parametrize("value", [[], {}])
    def test_an_empty_container_indexes_as_nothing(self, value: object) -> None:
        assert searchable_text(value) == ""

    def test_a_value_that_is_not_json_indexes_as_nothing_rather_than_raising(self) -> None:
        # Unreachable through the write path, where validation has already refused this.
        # It is here because the indexer runs on the read path too, and an indexer that
        # raised would take out a search that has nothing to do with the bad row.
        assert searchable_text(Unstorable()) == ""
