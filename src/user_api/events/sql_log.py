"""The change log, as rows.

Three shapes here are decisions rather than details, and the schema records two of them
next to the table because they are the ones a later reader tries to "fix".

**Nothing in this module references another table.** Every other table in the schema
cascades from ``users``; ``events`` names no foreign key at all. An event has to outlive
the entry it describes, and the event that matters most is the one saying something was
forgotten -- a foreign key would delete that record along with the thing it recorded, so
the account would be told "nothing was ever here" about a thing it had asked us to
forget. An event whose entry is long gone keeps its ``entry_id`` as a dangling string,
which is the intended state and not a gap somebody should tidy up.

**Everything is ordered and paged by ``sequence``, never by ``at``.** The clock is
injected rather than read, so two events written in one tick carry the same stamp to the
microsecond. Ordered by ``at``, their relative order is whatever SQLite happened to
choose, and a page boundary falling between them drops one from the results or hands it
back twice -- intermittently, and only under the fixed clock the tests use, which is the
worst place to find out. ``sequence`` is an ``AUTOINCREMENT`` key: total, and monotonic
even when the clock is not.

**The trim is one statement, in the caller's transaction.** The version that suggests
itself counts the account's events and deletes the excess when there is any, and it is
wrong twice over. As two statements it races: two appends both read the same count, both
conclude they are under the cap, and the log settles above it. As a separate transaction
it commits or rolls back independently of the append it was trimming for, which is the
same failure the port refuses for the append itself. :data:`TRIM_TO_CAP` names the rows
to delete with a subselect instead, so there is no count that can go stale.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from user_api.domain.entries import Action, EntryType
from user_api.events.log import Event
from user_api.storage.times import from_column, to_column

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from user_api.storage.database import Database

APPENDED_COLUMNS = "account_id, at, action, entry_id, entry_type, key, asserted_by, source, detail"
"""Everything an append writes. ``sequence`` is missing because SQLite assigns it."""

EVENT_COLUMNS = f"sequence, {APPENDED_COLUMNS}"
"""Everything a read needs, including the sequence a caller pages by."""

BEFORE_SEQUENCE = " AND sequence < ?"
"""The cursor, exclusive, so a page starts after the last row of the one before it."""

TRIM_TO_CAP = (
    "DELETE FROM events WHERE account_id = ? AND sequence <= ("
    "SELECT sequence FROM events WHERE account_id = ? ORDER BY sequence DESC LIMIT 1 OFFSET ?"
    ")"
)
"""Delete everything below the newest ``cap`` events of one account, in one statement.

The subselect skips ``cap`` rows and returns the sequence of the first one past them, so
the ``DELETE`` removes that row and everything older. Below the cap the subselect returns
no row at all, the comparison is ``NULL`` rather than false, and nothing is deleted --
which is why this needs no count and no guard around it.
"""


class SqlEventLog:
    """An account's events in one table, capped at the moment they are appended."""

    def __init__(self, *, database: Database) -> None:
        self._db = database

    # One parameter per column, matching the port. See :class:`user_api.events.log.EventLog`
    # for why grouping them into an object would only add a type to unpack again.
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
        connection.execute(
            f"INSERT INTO events ({APPENDED_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",  # noqa: S608
            (
                account_id,
                to_column(at),
                action.value,
                entry_id,
                None if entry_type is None else entry_type.value,
                key,
                asserted_by,
                source,
                None if detail is None else json.dumps(detail),
            ),
        )
        connection.execute(TRIM_TO_CAP, (account_id, account_id, cap))

    def purge_entry_values_in(self, connection: sqlite3.Connection, *, entry_id: str) -> None:
        # Not account-scoped, and does not need to be: an entry id is unique across the
        # database, and the only way a caller holds one is an account-scoped read.
        connection.execute("UPDATE events SET detail = NULL WHERE entry_id = ?", (entry_id,))

    def delete_for_account_in(self, connection: sqlite3.Connection, *, account_id: str) -> int:
        deleted = connection.execute("DELETE FROM events WHERE account_id = ?", (account_id,))
        return deleted.rowcount

    async def read(
        self, account_id: str, *, limit: int, before_sequence: int | None = None
    ) -> list[Event]:
        parameters: list[object] = [account_id]
        page = ""
        if before_sequence is not None:
            page = BEFORE_SEQUENCE
            parameters.append(before_sequence)
        parameters.append(limit)

        rows = await self._db.fetch_all(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE account_id = ?{page}"  # noqa: S608
            " ORDER BY sequence DESC LIMIT ?",
            parameters,
        )
        return [_event_of(row) for row in rows]

    async def count_for_account(self, account_id: str) -> int:
        return await self._db.count(
            "SELECT count(*) AS total FROM events WHERE account_id = ?", (account_id,)
        )


def _event_of(row: sqlite3.Row) -> Event:
    raw_type = row["entry_type"]
    raw_detail = row["detail"]
    # Annotated rather than returned straight out of json.loads: what comes back is Any,
    # and the annotation is where the shape the column was written with is asserted.
    detail: dict[str, object] | None = None if raw_detail is None else json.loads(raw_detail)
    return Event(
        sequence=row["sequence"],
        account_id=row["account_id"],
        at=from_column(row["at"]),
        action=Action(row["action"]),
        asserted_by=row["asserted_by"],
        entry_id=row["entry_id"],
        entry_type=None if raw_type is None else EntryType(raw_type),
        key=row["key"],
        source=row["source"],
        detail=detail,
    )
