"""What the user store promises. The adapter is :mod:`user_api.users.sql_store`.

The row this store manages deliberately holds no content -- an account id and two
timestamps. A preferred name is a field; duplicating it here would create two places to
change it and one of them would eventually be wrong.

Its job is existence and erasure. :meth:`UserStore.ensure` is called on the way into every
write, so there is no "create your record first" step for an assistant to forget and no
404 on an account that has simply not written anything yet -- ``GET /v1/user`` on a fresh
account returns an empty record, which is the truth.

:meth:`UserStore.delete` is the other half, and is the one method in this service that is
allowed to be thorough rather than careful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class UserRecord:
    """One account's record. Content lives in entries, not here."""

    account_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Erased:
    """What ``DELETE /v1/user`` destroyed. Counts only -- never what was in them."""

    entries: int
    events: int


@runtime_checkable
class UserStore(Protocol):
    """The record itself, and its destruction."""

    async def ensure(self, account_id: str, *, now: datetime) -> UserRecord:
        """Return this account's record, creating it if this is the first write.

        Idempotent, and called on the way into every write path. The alternative -- a
        ``create_user`` endpoint -- is a step a model forgets, then a 404 it does not
        understand, then a retry loop.
        """
        ...

    async def get(self, account_id: str) -> UserRecord | None:
        """This account's record, or ``None`` if nothing has ever been written."""
        ...

    async def touch(self, account_id: str, *, now: datetime) -> None:
        """Move the record's ``updated_at`` forward. Called by every write."""
        ...

    async def delete(self, account_id: str) -> Erased:
        """Destroy everything this account has: entries, scopes, search rows, events,
        settings and the record itself, in one transaction.

        **Always a hard purge, whatever the erasure mode says.** "Delete everything you
        know about me" has one honest meaning, and a tombstone is not it. The setting
        governs what forgetting one entry means; it does not get a say in this.

        The bytes are not gone when this returns -- see
        :meth:`~user_api.storage.database.Database.checkpoint_truncate`, which the caller
        runs immediately afterwards. A test scans the file and its ``-wal`` for a sentinel
        to prove the pair of them is enough.

        Returns what went, as counts, so the response can say "4 entries, 17 events" and
        the person can tell whether it was what they expected.
        """
        ...
