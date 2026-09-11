"""Field key normalisation: the whole anti-sprawl mechanism on the write side.

The table below is the specification. Every row is a name an assistant might reasonably
pick, and the assertion is the *exact* key it folds to -- because "it did not raise" would
pass just as happily for a normaliser that returned the input unchanged, which is the one
behaviour this module exists to prevent.
"""

from __future__ import annotations

import pytest

from user_api.domain.errors import InvalidKeyError
from user_api.domain.keys import KEY_PATTERN, MAX_KEY_LENGTH, WELL_KNOWN_KEYS, normalize_key

SPELLINGS_OF_ONE_KEY = ("Preferred Name", "preferred-name", "PREFERRED_NAME", "  preferred name  ")
"""Four ways a conversation might name the same thing, a month apart."""

FOLDINGS: list[tuple[str, str]] = [
    ("Preferred Name", "preferred_name"),
    ("preferred-name", "preferred_name"),
    ("PREFERRED_NAME", "preferred_name"),
    ("  spaced  ", "spaced"),
    ("multiple___underscores", "multiple_underscores"),
    ("trailing_", "trailing"),
    ("_leading", "leading"),
    ("mixed Case-And_Separators", "mixed_case_and_separators"),
    ("favourite  colour", "favourite_colour"),
    ("--hyphen--", "hyphen"),
    ("name\tand\nnewline", "name_and_newline"),
    ("blood type 2", "blood_type_2"),
]


class TestFolding:
    @pytest.mark.parametrize(("raw", "expected"), FOLDINGS)
    def test_a_name_folds_to_exactly_one_canonical_key(self, raw: str, expected: str) -> None:
        assert normalize_key(raw) == expected

    def test_the_three_spellings_of_preferred_name_are_one_key_and_not_three(self) -> None:
        # This is the point of the module. Without it an account ends up holding three
        # fields, the newest of which is right and the older two of which are wrong and
        # will be read by something.
        assert len({normalize_key(spelling) for spelling in SPELLINGS_OF_ONE_KEY}) == 1

    @pytest.mark.parametrize(("raw", "expected"), FOLDINGS)
    def test_folding_an_already_folded_key_changes_nothing(self, raw: str, expected: str) -> None:
        # A key is normalised on write and again wherever one is looked up, so a
        # normaliser that was not idempotent would make a stored key unfindable.
        assert normalize_key(expected) == expected

    @pytest.mark.parametrize(("raw", "expected"), FOLDINGS)
    def test_every_key_this_produces_is_a_legal_identifier(self, raw: str, expected: str) -> None:
        assert KEY_PATTERN.match(normalize_key(raw)) is not None

    @pytest.mark.parametrize("key", WELL_KNOWN_KEYS)
    def test_a_well_known_key_is_already_in_canonical_form(self, key: str) -> None:
        # These are advertised by the schema endpoint for a model to copy. One that did
        # not survive its own normaliser would be advice that stores something else.
        assert normalize_key(key) == key


class TestUnicode:
    def test_letters_outside_the_ascii_range_are_dropped_rather_than_transliterated(self) -> None:
        # Transliteration needs a judgement about language that this module has no way to
        # make, so an accented letter is simply not a key character. The fold is still
        # deterministic, which is all the anti-sprawl argument needs.
        assert normalize_key("Café Preference") == "caf_preference"

    def test_a_name_whose_script_leaves_a_usable_remainder_keeps_the_remainder(self) -> None:
        assert normalize_key("Ελλάδα timezone") == "timezone"

    def test_a_name_with_no_ascii_at_all_is_refused_rather_than_stored_empty(self) -> None:
        with pytest.raises(InvalidKeyError, match="no characters"):
            normalize_key("日本語")


class TestRefusals:
    @pytest.mark.parametrize("raw", ["", "   ", "!!!", "?", "---", "___", "€ ©"])
    def test_a_name_with_nothing_legal_in_it_is_refused(self, raw: str) -> None:
        with pytest.raises(InvalidKeyError, match="no characters"):
            normalize_key(raw)

    @pytest.mark.parametrize("raw", ["2fa", "2fa_backup", "3rd party contact", "1"])
    def test_a_key_that_would_start_with_a_digit_is_refused(self, raw: str) -> None:
        # Refused rather than quietly prefixed, so `2fa_backup` fails loudly instead of
        # becoming something subtly different that the next write will not find.
        with pytest.raises(InvalidKeyError, match="must start with a letter"):
            normalize_key(raw)

    def test_a_key_at_the_length_limit_is_still_accepted(self) -> None:
        assert normalize_key("a" * MAX_KEY_LENGTH) == "a" * MAX_KEY_LENGTH

    def test_a_key_past_the_length_limit_is_refused_rather_than_truncated(self) -> None:
        # Truncation is the dangerous alternative: two long keys sharing a 64-character
        # prefix would silently become one field, and neither caller would be told.
        with pytest.raises(InvalidKeyError, match="at most"):
            normalize_key("a" * (MAX_KEY_LENGTH + 1))

    def test_the_length_is_measured_after_folding_not_before(self) -> None:
        # A key at the limit, wrapped in punctuation that does not survive the fold.
        # Measuring the raw string would refuse a key that is perfectly legal.
        raw = f"  ({'a' * MAX_KEY_LENGTH})!  "

        assert normalize_key(raw) == "a" * MAX_KEY_LENGTH


class TestRefusalMessages:
    @pytest.mark.parametrize("raw", ["", "2fa", "a" * (MAX_KEY_LENGTH + 1)])
    def test_a_refusal_names_the_rule_that_failed(self, raw: str) -> None:
        # The caller here is a model that will otherwise try again with a different
        # spelling of the same illegal name.
        with pytest.raises(InvalidKeyError) as caught:
            normalize_key(raw)

        assert "field key" in str(caught.value)
