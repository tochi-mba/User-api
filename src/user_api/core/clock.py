"""Time as an injectable dependency.

Nothing in this codebase calls :func:`datetime.now` or :func:`time.monotonic` directly.
It matters here for two reasons that are worth separating. The first is the ordinary one:
a token expires, a JWKS cache goes stale, and neither rule is testable if time is what
the wall clock happens to say.

The second is particular to this service. A user record is *dated* data -- ``confirmed_at``
says when a human last vouched for a fact, and the whole staleness story is arithmetic on
it. A grace period before erasure is arithmetic on it too. Tests that had to wait thirty
days would not be written, and rules that are not tested are rules that are not true.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Reads the current time.

    Two readings are exposed because they answer different questions: :meth:`now` is a
    timestamp fit to record and compare against an expiry, while :meth:`monotonic`
    measures elapsed duration and is immune to system clock adjustments.
    """

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Return a monotonically increasing number of seconds from an arbitrary origin."""
        ...


class SystemClock:
    """The real clock, used everywhere outside tests."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
