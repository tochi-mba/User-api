"""What the change log promises. The adapter is :mod:`user_api.events.sql_log`.

The log answers "what did you change, and when did I tell you that?" -- which is the
question a person asks when an assistant says something surprising about them. It is not
an audit log in keyring's sense: there is no privileged actor here and nothing to hold to
account. It is the person's own history of their own record.

Three properties are worth stating before the methods.

**An event outlives the entry it describes.** There are no foreign keys. Purging an entry
removes the entry and, if values were being logged, the values from its events -- but the
event row itself stays, so "you forgot something on the 3rd" survives the thing that was
forgotten. That is the whole point of keeping a log at all.

**The write methods are synchronous and take a live connection.** Unusual for a port in
this codebase, and the entire reason this one is shaped like this: an event written in its
own transaction is an event that can be absent when the write it describes succeeded, or
present when that write rolled back. A log that is usually right is not a log.

**The log is capped and trimmed in the same transaction that appends.** A caller that
appended and then trimmed would leave a window in which the log is over its cap, and two
concurrent appends would both read the same count and both decline to trim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from user_api.domain.entries import Action, EntryType


@dataclass(frozen=True, slots=True)
class Event:
    """One recorded change."""

    sequence: int
    account_id: str
    at: datetime
    action: Action
    asserted_by: str
    entry_id: str | None = None
    entry_type: EntryType | None = None
    key: str | None = None
    source: str | None = None
    detail: dict[str, object] | None = field(default=None, repr=False)
    """The old and new values, present only when the account turned ``log_values`` on.

    ``repr=False`` because this is the one attribute that can hold what somebody told us,
    and a repr ends up in test output, in a debugger, and -- the one that matters -- in an
    exception's context when something upstream logs the object it was working on.
    """


@runtime_checkable
class EventLog(Protocol):
    """The append-only record of what changed."""

    # One parameter per column. Grouping them into an object would add a type whose only
    # job is to be constructed on one line and unpacked on the next.
    def append_in(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        at: datetime,
        action: Action,
        asserted_by: str,
        cap: int,
        entry_id: str | None = None,
        entry_type: EntryType | None = None,
        key: str | None = None,
        source: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Append one event **inside a transaction the caller already holds**.

        Args:
            connection: the open transaction to write into.
            account_id: whose log.
            at: the injected clock's reading, not the wall clock's.
            action: what happened.
            asserted_by: the audience of the token that did it -- verified, not claimed.
            cap: the most events one account keeps. Trimmed here, in this transaction.
            entry_id: which entry, when there is one.
            entry_type: field or note.
            key: the field key, when the entry was a field.
            source: what the writer claimed about where the change came from.
            detail: the values, and **only** when the account has turned ``log_values``
                on. The caller decides that, because the caller is the one holding the
                settings; this method records what it is given.
        """
        ...

    def purge_entry_values_in(self, connection: sqlite3.Connection, *, entry_id: str) -> None:
        """Strip the recorded values from every event about one entry, keeping the events.

        Called by the purge, in the purge's transaction. This is the half of erasure that
        is easy to forget: with ``log_values`` on, the old value of a forgotten field is
        sitting in the event that recorded the change, and a purge that only deleted the
        entry would leave it there -- in the same file, findable with ``grep``.
        """
        ...

    def delete_for_account_in(self, connection: sqlite3.Connection, *, account_id: str) -> int:
        """Remove an account's entire log. Returns how many events went.

        Only ``DELETE /v1/user`` reaches this. "Delete everything you know about me" has
        one honest meaning, and a surviving log saying what used to be there is not it.
        """
        ...

    async def read(
        self, account_id: str, *, limit: int, before_sequence: int | None = None
    ) -> list[Event]:
        """One page of an account's log, newest first.

        Paged by ``sequence`` rather than by timestamp, because the clock is injectable and
        two events in one tick share a timestamp -- which would make the boundary between
        two pages non-deterministic in exactly the tests that care about it.
        """
        ...

    async def count_for_account(self, account_id: str) -> int:
        """How many events this account has. For the always-load block's counts."""
        ...
