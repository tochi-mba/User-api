"""How a moment in time becomes a column, and comes back unchanged.

SQLite has no date type, so this is a decision rather than a detail, and there are two
properties to preserve:

**Timezone awareness survives the round trip.** Nothing in this service reads a naive
datetime -- every staleness comparison and every grace-period calculation is against a
UTC-aware one -- so a column that returned a naive datetime would raise at the first
comparison, in a code path far from the store that produced it.

**Lexicographic order matches chronological order.** Cursor pagination, the ``updated_at``
ordering, ``?stale_before=`` and the purge sweep all sort and compare on these columns, in
SQL, as text, and a cursor that compared them wrongly would skip a row or return one
twice. That only works because the format is fixed width.

It is worth being precise about why, because the obvious example is wrong. In *this*
encoding, a stamp that omitted its zero microseconds would still sort correctly: the
offset suffix follows, ``+`` (0x2B) sorts below ``.`` (0x2E), and ``.000000`` is the
smallest fraction of its second either way. The guarantee is not that this particular
omission is harmless; it is that **no** variation in width can arise, so nobody has to
work out whether the next one is. Change the suffix to ``Z`` and the omission breaks the
ordering immediately.

Both are pinned by tests, because both fail silently.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["from_column", "from_column_optional", "to_column", "to_column_optional"]


def to_column(value: datetime) -> str:
    """Render a timezone-aware datetime as a sortable, fixed-width UTC stamp.

    Raises:
        ValueError: if the datetime is naive. A naive datetime here means a caller read
            the wall clock directly instead of taking the injected one, which is a bug
            worth failing on rather than guessing a zone for.
    """
    if value.tzinfo is None:
        msg = "refusing to store a naive datetime: it has no unambiguous instant"
        raise ValueError(msg)
    # timespec is explicit because isoformat() drops the fractional part when it is zero,
    # which would break the fixed width the ordering depends on.
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def from_column(raw: str) -> datetime:
    """Parse a stamp written by :func:`to_column` back into a UTC datetime."""
    return datetime.fromisoformat(raw).astimezone(UTC)


def to_column_optional(value: datetime | None) -> str | None:
    """Render a datetime that may be absent."""
    return None if value is None else to_column(value)


def from_column_optional(raw: str | None) -> datetime | None:
    """Parse a stamp that may be absent."""
    return None if raw is None else from_column(raw)
