"""A clock that only moves when a test tells it to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Deterministic :class:`~user_api.core.clock.Clock` implementation.

    Three rules in this service are arithmetic on a date -- a token's expiry, a JWKS
    cache's age, and how long a forgotten entry survives -- and the third is measured in
    days. Without this, testing the grace period would mean waiting a month.
    """

    def __init__(self, start: datetime = EPOCH) -> None:
        self._start = start
        self._now = start

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return (self._now - self._start).total_seconds()

    def advance(self, delta: timedelta | float) -> None:
        """Move time forward by a timedelta or a number of seconds."""
        if not isinstance(delta, timedelta):
            delta = timedelta(seconds=delta)
        self._now += delta
