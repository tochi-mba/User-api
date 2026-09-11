"""How a moment becomes a column and comes back unchanged.

SQLite has no date type, so this is a decision rather than a detail, and both properties
it preserves fail silently when they break.

**Awareness survives the round trip**, because every staleness comparison and every
grace-period calculation is against a UTC-aware datetime, and mixing a naive one in raises
at the comparison rather than anywhere near the store that produced it.

**The format is fixed width**, because cursor pagination, the ``updated_at`` ordering,
``?stale_before=`` and the purge sweep all sort and compare these columns in SQL, as text.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from user_api.storage.times import (
    from_column,
    from_column_optional,
    to_column,
    to_column_optional,
)

STAMP_WIDTH = 32
"""``2026-01-01T00:00:00.000000+00:00``. Every stamp, always, whatever it holds."""


class TestRoundTrip:
    def test_it_preserves_the_instant(self) -> None:
        moment = datetime(2026, 9, 10, 21, 43, 7, 123456, tzinfo=UTC)

        assert from_column(to_column(moment)) == moment

    def test_it_comes_back_timezone_aware(self) -> None:
        # A naive datetime out of the store would raise at the first comparison against
        # the clock, in a code path with nothing to say where the naive value came from.
        restored = from_column(to_column(datetime(2026, 1, 1, tzinfo=UTC)))

        assert restored.tzinfo is not None
        assert restored.utcoffset() == timedelta(0)

    def test_an_offset_that_is_not_utc_is_normalised_on_the_way_in(self) -> None:
        # Two stores writing the same instant in different offsets would produce two texts
        # that compare unequal and sort apart, which is the whole ordering story gone.
        tokyo = datetime(2026, 9, 10, 6, 0, tzinfo=timezone(timedelta(hours=9)))

        assert to_column(tokyo) == "2026-09-09T21:00:00.000000+00:00"

    def test_an_offset_that_is_not_utc_survives_as_the_same_instant(self) -> None:
        tokyo = datetime(2026, 9, 10, 6, 0, tzinfo=timezone(timedelta(hours=9)))

        assert from_column(to_column(tokyo)) == tokyo

    def test_a_naive_datetime_is_refused_rather_than_guessed_at(self) -> None:
        # A naive datetime here means somebody read the wall clock instead of taking the
        # injected one, so guessing a zone for it would bury the bug rather than find it.
        with pytest.raises(ValueError, match="naive datetime"):
            to_column(datetime(2026, 1, 1))  # noqa: DTZ001 -- the point of the test


class TestFixedWidth:
    """Lexicographic order has to agree with chronological order, in SQL, as text."""

    def test_a_stamp_with_no_microseconds_keeps_its_full_width(self) -> None:
        # `isoformat()` drops the fractional part when it is zero. `timespec` is what
        # stops that, and this assertion is what would fail if somebody dropped it.
        assert to_column(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00.000000+00:00"

    def test_every_stamp_is_the_same_width_whatever_it_holds(self) -> None:
        widths = {
            len(to_column(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(microseconds=step)))
            for step in (0, 1, 999_999, 1_000_000, 86_400_000_000)
        }

        assert widths == {STAMP_WIDTH}

    def test_a_zero_microsecond_stamp_still_sorts_against_one_that_has_them(self) -> None:
        """The silent failure this format exists to prevent.

        A cursor asks SQL for the rows after a stored stamp. If one row's text were
        shorter than another's, the comparison would land on a different character than
        the one that decides the instant, and the page would skip a row or repeat one --
        on exactly the moments where the two encodings disagree, and nowhere else.
        """
        second = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        moments = [
            second - timedelta(microseconds=1),
            second,
            second + timedelta(microseconds=1),
            second + timedelta(seconds=1),
        ]

        assert [to_column(moment) for moment in moments] == sorted(
            to_column(moment) for moment in moments
        )

    def test_text_order_matches_chronological_order_across_a_shuffled_set(self) -> None:
        moments = [
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2025, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
            datetime(2026, 2, 1, tzinfo=UTC),
            datetime(2100, 1, 1, tzinfo=UTC),
        ]

        assert [to_column(moment) for moment in sorted(moments)] == sorted(
            to_column(moment) for moment in moments
        )


class TestOptional:
    def test_absent_stays_absent_in_both_directions(self) -> None:
        # `confirmed_at` and `forgotten_at` are genuinely absent most of the time, and an
        # absent one must not become the epoch on the way through.
        assert to_column_optional(None) is None
        assert from_column_optional(None) is None

    def test_present_round_trips_exactly_as_the_required_form_does(self) -> None:
        moment = datetime(2026, 3, 4, 5, 6, 7, 8, tzinfo=UTC)

        assert to_column_optional(moment) == to_column(moment)
        assert from_column_optional(to_column_optional(moment)) == moment

    def test_the_optional_form_refuses_a_naive_datetime_too(self) -> None:
        # Otherwise the nullable columns would be a way round the rule that the required
        # ones enforce, which is the sort of gap nobody notices until it is in the data.
        with pytest.raises(ValueError, match="naive datetime"):
            to_column_optional(datetime(2026, 1, 1))  # noqa: DTZ001 -- the point of the test
