"""One SQLite connection, owned by one thread, reached only through this class.

Lifted from keyring with three changes, each argued below. The reasoning that has not
changed is reproduced rather than referenced, because the next person to edit this file
should not have to read another repository to know why it is shaped this way.

## Why a single thread and not a lock

The obvious design is ``asyncio.to_thread`` guarded by an ``asyncio.Lock``::

    async with self._lock:                    # DO NOT
        return await asyncio.to_thread(work)

It is broken. Cancelling the task while it awaits ``to_thread`` unwinds the ``async with``
and releases the lock, but **does not cancel the thread**: ``work`` is still running on
the shared connection, mid-transaction, when the next task takes the lock and calls into
that same connection from a different thread. A client disconnecting cancels its request
task, so this is an ordinary Tuesday rather than a thought experiment.

A single-worker executor removes the failure instead of patching it. Serialization stops
depending on a lock that cancellation can drop, and becomes a property of there being
exactly one thread that may touch the connection at all.

Every database call is submitted as *one whole callable*, so a transaction is indivisible
by construction rather than by convention -- which is what every cap in this service is
built on. The cost, and it belongs in the open: **a cancelled request's write may still
commit**, because the queued callable runs to completion regardless of who is waiting.

## The pragma that lies

``PRAGMA foreign_keys`` defaults to **off**, is per-connection rather than stored in the
file, and -- the part that costs an afternoon -- is a **silent no-op when a transaction is
open**. Issued through a driver that opens implicit transactions around DML, it can report
success and do nothing, leaving every foreign key decorative and every cascade absent.
Here that would mean a forgotten entry keeping its scope rows and a deleted user record
keeping its entries: erasure that reports success and erases nothing. That is why the
connection is opened with ``isolation_level=None`` (this class issues its own ``BEGIN
IMMEDIATE``) and why the setting is read back and verified rather than assumed.

## The file mode matters more here than it did there

keyring's database holds password hashes and encrypted credential material; the encryption
defends against a stolen disk and the mode defends against every other process on the box.
This database holds **no encryption at all** -- see ADR-0003 for why, and what it costs.
The mode is therefore not defence in depth, it is the defence. 0600 on the file and on its
``-wal``/``-shm`` sidecars, which hold the same data the file does.

## The three changes from keyring

``synchronous = NORMAL`` rather than ``FULL``. keyring pays one fsync per commit because
a lost transaction there is a credential somebody believes is saved. A lost transaction
here is a note somebody believes was written -- worse than nothing, but recoverable by
saying it again -- and the write volume is an order of magnitude higher, because an
assistant writes as it learns. WAL with ``NORMAL`` cannot *corrupt* the database; it can
only lose the last few commits on power loss. That is the trade, stated plainly.

``PRAGMA secure_delete = ON``, which overwrites freed pages rather than merely marking
them free. It is recommended as defence in depth and the honest measurement is in
ADR-0005: across every case constructed for it, it made **no measurable difference** --
the truncating checkpoint below was doing the work in all of them. It is on because the
freelist case it covers is real and it costs almost nothing, not because it was observed
to help.

:meth:`Database.checkpoint_truncate` is new, and it is the step that actually erases. A
``DELETE`` removes a row from the b-tree and leaves the bytes in the ``-wal`` file, where
they stay until a checkpoint. ``PRAGMA wal_checkpoint(TRUNCATE)`` is what removes them.
See :mod:`user_api.users.erasure`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, TypeVar

from user_api.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

logger = get_logger(__name__)

T = TypeVar("T")

DATABASE_FILE_MODE = 0o600
"""Owner-only. SQLite would otherwise create these 0644; see the module docstring."""

SIDECARS = ("-wal", "-shm")
"""The write-ahead log and its shared-memory index, which hold data like the file does.

SQLite gives a WAL file the permissions of the database it belongs to, so these only need
setting for sidecars that already exist by the time the mode is applied.
"""

CONNECT_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA secure_delete = ON",
    "PRAGMA busy_timeout = 5000",
)
"""Applied to the connection, in this order, before anything else runs on it.

See the module docstring for why ``synchronous`` and ``secure_delete`` differ from
keyring's, and for what each one does and does not buy.
"""


class StorageError(RuntimeError):
    """The database cannot be used as configured.

    Deliberately not a :class:`~user_api.domain.errors.DomainError`: nothing here is
    about a person or their record, and nothing above should be catching it. It means
    the process should not have started.
    """


def require_foreign_keys(connection: sqlite3.Connection) -> None:
    """Refuse a connection whose foreign keys did not actually come on.

    Read back rather than trusted. See the module docstring for how a ``PRAGMA
    foreign_keys`` can succeed and do nothing; the consequence here is an erasure that
    leaves the scope rows and the search index behind.
    """
    (enabled,) = connection.execute("PRAGMA foreign_keys").fetchone()
    if not enabled:
        msg = "foreign keys are not enabled on this connection; refusing to continue"
        raise StorageError(msg)


def make_private(path: Path) -> None:
    """Make the database and its sidecars readable only by the account running us.

    Applied after opening rather than before, because there is nothing to chmod until
    SQLite has created the file -- which leaves a window where a fresh database exists at
    0644. It is one open() wide, on a file with nothing in it yet, and closing it properly
    would mean pre-creating the file ourselves and hoping SQLite agreed with the result.
    """
    path.chmod(DATABASE_FILE_MODE)
    for suffix in SIDECARS:
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.chmod(DATABASE_FILE_MODE)


class Database:
    """The one way into the SQLite file.

    Constructing this opens the connection. Nothing is migrated -- see
    :func:`user_api.storage.migrator.migrate`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="user-db")
        self._closed = False
        # Submitted rather than called, so the connection is created on the worker thread
        # and is therefore only ever touched by it.
        self._connection: sqlite3.Connection = self._executor.submit(self._connect).result()

    @property
    def path(self) -> Path:
        """Where the file is. For diagnostics and for the backup instructions."""
        return self._path

    def _connect(self) -> sqlite3.Connection:
        # mode applies only to directories this creates; an existing one is left alone,
        # because the configured path names a file and its parent may not be ours.
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self._path, isolation_level=None)
        connection.row_factory = sqlite3.Row

        journal_mode = "unknown"
        for pragma in CONNECT_PRAGMAS:
            row = connection.execute(pragma).fetchone()
            if row is not None and pragma.startswith("PRAGMA journal_mode"):
                journal_mode = str(row[0])

        require_foreign_keys(connection)
        # After the pragmas, because switching to WAL is what creates the sidecars.
        make_private(self._path)
        logger.info("database_opened", journal_mode=journal_mode)
        return connection

    def run_sync(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``work`` on the worker thread, blocking the caller until it finishes.

        For startup only -- opening the database and migrating it happen before there is
        an event loop to keep responsive, and the composition root is synchronous. Inside
        a request this would block the loop, which is what :meth:`run` is for.
        """
        return self._executor.submit(work, self._connection).result()

    async def run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``work`` on the worker thread, outside any transaction.

        For reads. A single SQLite statement is atomic by itself, so a read needs no
        explicit transaction; a *sequence* of reads that must agree with each other does,
        and belongs in :meth:`transact`.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, work, self._connection)

    async def transact(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``work`` as one ``BEGIN IMMEDIATE`` transaction.

        The whole transaction is one submitted callable, which is what makes it
        indivisible: there is no point inside it at which another caller can be
        interleaved, because there is no other thread that could run one. Every cap in
        this service -- entries, fields, pins, events -- is counted and enforced inside
        one of these, which is why "count, then write" cannot go stale here.

        Anything ``work`` raises -- a domain error refusing the write included -- rolls
        the transaction back and propagates.
        """
        return await self.run(lambda connection: _in_transaction(connection, work))

    async def fetch_all(self, sql: str, parameters: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Run one read statement and return every row."""
        return await self.run(lambda connection: connection.execute(sql, parameters).fetchall())

    async def fetch_one(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Row | None:
        """Run one read statement and return the first row, or ``None``."""
        row: sqlite3.Row | None = await self.run(
            lambda connection: connection.execute(sql, parameters).fetchone()
        )
        return row

    async def count(self, sql: str, parameters: Sequence[object] = ()) -> int:
        """Run a ``SELECT count(*) AS total`` and return the number.

        Indexes into the result rather than testing for a missing row. An aggregate with
        no GROUP BY always returns exactly one row, so a "what if it did not" branch would
        be unreachable code -- which the coverage gate could then never cover, and which
        somebody would eventually satisfy by weakening the gate.
        """
        rows = await self.fetch_all(sql, parameters)
        return int(rows[0]["total"])

    async def execute(self, sql: str, parameters: Sequence[object] = ()) -> int:
        """Run one write statement in its own transaction. Returns rows affected."""
        return await self.transact(lambda connection: connection.execute(sql, parameters).rowcount)

    async def checkpoint_truncate(self) -> None:
        """Flush the write-ahead log into the database and truncate it to nothing.

        **This is the step that actually erases.** A ``DELETE`` removes a row from the
        b-tree; the bytes it held stay in the ``-wal`` file until a checkpoint moves them,
        and ``TRUNCATE`` is the only checkpoint mode that then empties the file rather
        than leaving its pages to be overwritten eventually. A purge that stops at the
        ``DELETE`` leaves the forgotten value sitting in a sidecar next to the database,
        readable with ``grep``.

        ``VACUUM`` would also do it and is far more expensive -- it rewrites the entire
        database under a write lock. The measurement behind choosing the checkpoint is in
        ADR-0005.

        Not inside the purge transaction: a checkpoint cannot run with a transaction open,
        and doing it once per sweep rather than once per row is the difference between a
        cheap step and a pathological one.
        """
        await self.run(lambda connection: connection.execute("PRAGMA wal_checkpoint(TRUNCATE)"))

    async def aclose(self) -> None:
        """Close the connection and stop the worker thread. Safe to call twice."""
        if self._closed:
            return
        self._closed = True

        await self.run(lambda connection: connection.close())
        # The queue is empty by now -- the close above was the last thing on it -- so this
        # returns immediately rather than blocking the event loop.
        self._executor.shutdown(wait=True)


def _in_transaction(connection: sqlite3.Connection, work: Callable[[sqlite3.Connection], T]) -> T:
    """Run ``work`` between ``BEGIN IMMEDIATE`` and ``COMMIT``, rolling back on anything."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = work(connection)
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    return result
