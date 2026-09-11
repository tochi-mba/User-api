"""The real clock is four lines, and every expiry in this service trusts all four."""

from __future__ import annotations

from datetime import UTC, timedelta

from tests.fakes.clock import FakeClock
from user_api.core.clock import Clock, SystemClock


def test_the_system_clock_satisfies_the_port() -> None:
    checked: Clock = SystemClock()

    assert isinstance(checked, Clock)


def test_now_is_timezone_aware_utc() -> None:
    # Nothing here ever compares a naive datetime: a grace period, a token expiry and the
    # staleness of a confirmed fact are all arithmetic against an aware one, and mixing
    # the two raises at the comparison rather than where the naive value came from.
    assert SystemClock().now().tzinfo is UTC


def test_monotonic_never_goes_backwards() -> None:
    # The JWKS refetch floor is measured with this rather than with `now`, so that an
    # operator correcting the system clock cannot open a window of unlimited refetches.
    clock = SystemClock()

    first = clock.monotonic()

    assert clock.monotonic() >= first


def test_the_fake_clock_satisfies_the_same_port() -> None:
    # If the fake drifted from the port, every expiry test in this suite would be proving
    # something the running service does not do.
    checked: Clock = FakeClock()

    assert isinstance(checked, Clock)


def test_advancing_the_fake_moves_both_readings_together() -> None:
    clock = FakeClock()
    before = clock.now()

    clock.advance(timedelta(minutes=5))

    assert clock.now() - before == timedelta(minutes=5)
    assert clock.monotonic() == 300.0
