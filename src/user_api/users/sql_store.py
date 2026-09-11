"""The account's own row, and the transaction that destroys everything hanging off it.

The row holds three columns and nothing about the person -- :mod:`user_api.users.store`
argues that part. What is left is two shapes that were arrived at the hard way.

**:meth:`SqlUserStore.ensure` is one transaction rather than two statements.** Every write
path calls it on the way in, so it runs constantly and almost always finds the record
already there. Spelled the obvious way -- insert if absent, then read it back -- it is two
callables through :class:`~user_api.storage.database.Database`, and the queue that
serialises them is free to run somebody else's in the gap between the two. That somebody
else can be ``DELETE /v1/user``, and the read-back then comes home empty for a record this
method has already promised to return. As one ``transact`` there is no gap, which is also
why nothing below tests the selected row for ``None``: it cannot be missing, and a branch
guarding against it would be code no test could ever reach.

**The purge deletes in an order, and the order is load-bearing.** ``entry_search`` is a
plain FTS5 table keyed by ``entries.seq``, holding a second copy of every entry's
``search_text``, and its rows know their entry by rowid and by nothing else. Delete the
entries first and those rowids are gone, leaving index rows that no statement can reach by
account any more -- each one still holding the words of a note somebody asked to have
destroyed, in the same file, findable with ``grep``. So the seq values are collected while
the entries are still there to name them, and the index rows go before the rows they were
keyed by.

The erasure is unconditional. The account's erasure mode decides what forgetting *one*
entry means and gets no say here: "delete everything you know about me" has one honest
reading, and a tombstone is not it.

What is deliberately missing is the checkpoint. A ``DELETE`` moves bytes into the
write-ahead log rather than out of the database, and
:meth:`~user_api.storage.database.Database.checkpoint_truncate` is what takes them out --
run once by the caller afterwards, because a checkpoint cannot run with a transaction open
and once per sweep rather than once per account is the difference between a cheap step and
a pathological one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from user_api.storage.times import from_column, to_column
from user_api.users.store import Erased, UserRecord

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from user_api.storage.database import Database

USER_COLUMNS = "account_id, created_at, updated_at"


class SqlUserStore:
    """One row per account, and the one method allowed to be thorough."""

    def __init__(self, *, database: Database) -> None:
        self._db = database

    async def ensure(self, account_id: str, *, now: datetime) -> UserRecord:
        def write(connection: sqlite3.Connection) -> UserRecord:
            stamp = to_column(now)
            # DO NOTHING rather than DO UPDATE: an account that has been here before keeps
            # the created_at it arrived with, and `now` reaches the row only when the row
            # is a new one. Moving updated_at on is touch()'s job, and every write does it.
            connection.execute(
                f"INSERT INTO users ({USER_COLUMNS}) VALUES (?, ?, ?)"  # noqa: S608
                " ON CONFLICT (account_id) DO NOTHING",
                (account_id, stamp, stamp),
            )
            row = connection.execute(
                f"SELECT {USER_COLUMNS} FROM users WHERE account_id = ?",  # noqa: S608
                (account_id,),
            ).fetchone()
            return _record_of(row)

        return await self._db.transact(write)

    async def get(self, account_id: str) -> UserRecord | None:
        row = await self._db.fetch_one(
            f"SELECT {USER_COLUMNS} FROM users WHERE account_id = ?",  # noqa: S608
            (account_id,),
        )
        return None if row is None else _record_of(row)

    async def touch(self, account_id: str, *, now: datetime) -> None:
        """Move ``updated_at`` on. An account with no row is a no-op, not an error.

        Every caller has been through :meth:`ensure` in the same request, so the only way
        to reach an absent record is for the account to have been erased between the two
        -- a race whose loser cannot do anything useful with a failure, and which is not
        worth a rowcount check that no test could then provoke.
        """
        await self._db.execute(
            "UPDATE users SET updated_at = ? WHERE account_id = ?",
            (to_column(now), account_id),
        )

    async def delete(self, account_id: str) -> Erased:
        def purge(connection: sqlite3.Connection) -> Erased:
            events = int(
                connection.execute(
                    "SELECT count(*) AS total FROM events WHERE account_id = ?",
                    (account_id,),
                ).fetchone()["total"]
            )
            connection.execute("DELETE FROM events WHERE account_id = ?", (account_id,))

            rows = connection.execute(
                "SELECT seq FROM entries WHERE account_id = ?", (account_id,)
            ).fetchall()
            connection.executemany(
                "DELETE FROM entry_search WHERE rowid = ?",
                [(row["seq"],) for row in rows],
            )
            connection.execute("DELETE FROM entries WHERE account_id = ?", (account_id,))
            # entry_scopes has no statement of its own: it cascades from entries. That is
            # a guarantee rather than a hope, because the connection refuses to open at all
            # unless PRAGMA foreign_keys reads back as on -- see
            # user_api.storage.database.require_foreign_keys, which exists for this line.
            # Everything hanging off users is named anyway, cascade or not, so the
            # transaction reads as the list of what an account is made of.
            connection.execute("DELETE FROM user_settings WHERE account_id = ?", (account_id,))
            connection.execute("DELETE FROM users WHERE account_id = ?", (account_id,))

            return Erased(entries=len(rows), events=events)

        return await self._db.transact(purge)


def _record_of(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        account_id=row["account_id"],
        created_at=from_column(row["created_at"]),
        updated_at=from_column(row["updated_at"]),
    )
