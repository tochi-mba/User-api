"""The one connection, and the three things it exists to guarantee.

* :class:`TestFileMode`. Nothing in this database is encrypted, so the file mode is not
  defence in depth -- it is the defence. 0600 on the file and on both sidecars, which
  hold the same rows the file does.
* :class:`TestForeignKeys`. ``PRAGMA foreign_keys`` is per-connection, defaults to off,
  and is a silent no-op inside an open transaction. A build where it quietly did nothing
  would report a successful erasure and leave the scope rows behind.
* :class:`TestCheckpointing`. A ``DELETE`` moves a row out of the b-tree and leaves its
  bytes in the ``-wal`` file. The truncating checkpoint is the step that actually erases.
"""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from user_api.domain.errors import EntryNotFoundError
from user_api.storage.database import (
    DATABASE_FILE_MODE,
    SIDECARS,
    Database,
    StorageError,
    make_private,
    require_foreign_keys,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

SENTINEL = "Dr Okonkwo at the Meadow Clinic"
"""A value of the kind this database holds, for the tests that read the bytes."""


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "scratch.db")
    await database.execute("CREATE TABLE t (x INTEGER NOT NULL PRIMARY KEY, y TEXT) STRICT")
    try:
        yield database
    finally:
        await database.aclose()


def sidecar(database: Database, suffix: str) -> Path:
    """One of the database's companion files, whether or not it exists yet."""
    return database.path.with_name(database.path.name + suffix)


def any_sidecar_exists(path: Path) -> bool:
    """Whether any of a database's companion files are on disk."""
    return any(path.with_name(path.name + suffix).exists() for suffix in SIDECARS)


class TestReadsAndWrites:
    async def test_a_written_row_reads_back(self, db: Database) -> None:
        await db.execute("INSERT INTO t (x, y) VALUES (?, ?)", (1, "one"))

        row = await db.fetch_one("SELECT y FROM t WHERE x = ?", (1,))

        assert row is not None
        assert row["y"] == "one"

    async def test_fetching_one_row_that_is_not_there_is_none(self, db: Database) -> None:
        assert await db.fetch_one("SELECT y FROM t WHERE x = ?", (404,)) is None

    async def test_fetching_all_returns_every_row_in_the_order_asked_for(
        self, db: Database
    ) -> None:
        await db.execute("INSERT INTO t (x, y) VALUES (1, 'a'), (2, 'b')")

        rows = await db.fetch_all("SELECT x FROM t ORDER BY x DESC")

        assert [row["x"] for row in rows] == [2, 1]

    async def test_fetching_all_from_an_empty_table_is_an_empty_list(self, db: Database) -> None:
        assert await db.fetch_all("SELECT x FROM t") == []

    async def test_executing_reports_how_many_rows_it_touched(self, db: Database) -> None:
        await db.execute("INSERT INTO t (x, y) VALUES (1, 'a'), (2, 'b')")

        assert await db.execute("DELETE FROM t WHERE x > 0") == 2

    async def test_counting_returns_the_number_rather_than_a_row(self, db: Database) -> None:
        await db.execute("INSERT INTO t (x, y) VALUES (1, 'a'), (2, 'b')")

        assert await db.count("SELECT count(*) AS total FROM t") == 2

    async def test_counting_nothing_is_zero_rather_than_absent(self, db: Database) -> None:
        # An aggregate with no GROUP BY always returns exactly one row, which is why
        # `count` indexes into the result instead of testing for a missing one.
        assert await db.count("SELECT count(*) AS total FROM t WHERE x > ?", (100,)) == 0

    async def test_arbitrary_work_can_be_run_on_the_connection(self, db: Database) -> None:
        await db.execute("INSERT INTO t (x, y) VALUES (1, 'a')")

        # The callable is submitted whole, so it returns a value rather than a cursor:
        # the cursor belongs to the worker thread and cannot be read anywhere else.
        found = await db.run(
            lambda connection: connection.execute("SELECT count(*) FROM t").fetchone()[0]
        )

        assert found == 1

    async def test_work_can_also_be_run_synchronously_for_startup(self, db: Database) -> None:
        # Opening and migrating happen from a synchronous composition root, before there
        # is an event loop whose responsiveness could matter.
        assert db.run_sync(lambda connection: connection.execute("SELECT 7").fetchone()[0]) == 7

    async def test_it_remembers_where_the_file_is(self, tmp_path: Path, db: Database) -> None:
        assert db.path == tmp_path / "scratch.db"


class TestTransactions:
    async def test_a_transaction_that_returns_commits_everything_in_it(self, db: Database) -> None:
        def write_two(connection: sqlite3.Connection) -> int:
            connection.execute("INSERT INTO t (x, y) VALUES (1, 'a')")
            connection.execute("INSERT INTO t (x, y) VALUES (2, 'b')")
            return 2

        assert await db.transact(write_two) == 2
        assert await db.count("SELECT count(*) AS total FROM t") == 2

    async def test_a_failed_transaction_leaves_nothing_behind(self, db: Database) -> None:
        def write_then_fail(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO t (x, y) VALUES (1, 'a')")
            msg = "changed my mind"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="changed my mind"):
            await db.transact(write_then_fail)

        assert await db.fetch_all("SELECT x FROM t") == []

    async def test_a_domain_error_rolls_back_exactly_like_a_crash_does(self, db: Database) -> None:
        """Every cap in this service is a count and a write inside one transaction.

        The refusal is raised *after* the write that provoked it, so a domain error that
        did not roll back would leave the write it was refusing.
        """

        def write_then_refuse(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO t (x, y) VALUES (1, 'a')")
            raise EntryNotFoundError

        with pytest.raises(EntryNotFoundError):
            await db.transact(write_then_refuse)

        assert await db.fetch_all("SELECT x FROM t") == []

    async def test_the_connection_is_usable_after_a_rollback(self, db: Database) -> None:
        # A rollback that left the transaction open would hold a write lock, and every
        # later call would fail or hang rather than reporting the original problem.
        await db.execute("INSERT INTO t (x, y) VALUES (1, 'a')")

        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("INSERT INTO t (x, y) VALUES (1, 'again')")

        await db.execute("INSERT INTO t (x, y) VALUES (2, 'b')")

        rows = await db.fetch_all("SELECT y FROM t ORDER BY x")
        assert [row["y"] for row in rows] == ["a", "b"]


class TestForeignKeys:
    async def test_they_read_back_as_on(self, db: Database) -> None:
        # Read back rather than assumed: the pragma that switches them on is a silent
        # no-op inside an open transaction, so issuing it proves nothing on its own.
        row = await db.fetch_one("PRAGMA foreign_keys")

        assert row is not None
        assert row[0] == 1

    async def test_a_cascade_actually_cascades(self, db: Database) -> None:
        """The other half of the proof, because a pragma reading ``1`` is only a claim.

        This is what erasure depends on: deleting a person's row has to take their
        entries, scopes and events with it rather than leaving orphans nobody reads and
        nobody deletes.
        """
        await db.execute("CREATE TABLE parent (id TEXT NOT NULL PRIMARY KEY) STRICT")
        await db.execute(
            "CREATE TABLE child ("
            "  id TEXT NOT NULL PRIMARY KEY,"
            "  parent_id TEXT NOT NULL REFERENCES parent(id) ON DELETE CASCADE"
            ") STRICT"
        )
        await db.execute("INSERT INTO parent (id) VALUES ('p')")
        await db.execute("INSERT INTO child (id, parent_id) VALUES ('c', 'p')")

        await db.execute("DELETE FROM parent WHERE id = 'p'")

        assert await db.fetch_all("SELECT id FROM child") == []

    def test_the_guard_refuses_a_connection_where_they_are_off(self) -> None:
        # A real connection with the real default, rather than a stand-in: off is what
        # SQLite gives everybody who does not ask, which is the case being guarded.
        with (
            closing(sqlite3.connect(":memory:")) as connection,
            pytest.raises(StorageError, match="foreign keys are not enabled"),
        ):
            require_foreign_keys(connection)

    def test_the_guard_accepts_a_connection_where_they_are_on(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("PRAGMA foreign_keys = ON")

            require_foreign_keys(connection)


class TestPragmas:
    async def test_the_journal_is_write_ahead(self, db: Database) -> None:
        row = await db.fetch_one("PRAGMA journal_mode")

        assert row is not None
        assert row[0] == "wal"

    async def test_writes_are_synchronous_normal_rather_than_full(self, db: Database) -> None:
        """The stated trade: one fsync per checkpoint rather than one per commit.

        NORMAL under WAL cannot corrupt the database; it can lose the last few commits on
        power loss. A lost note is recoverable by saying it again, and an assistant writes
        as it learns, so the write volume is what pays for the change.
        """
        row = await db.fetch_one("PRAGMA synchronous")

        assert row is not None
        assert row[0] == 1

    async def test_freed_pages_are_overwritten_rather_than_merely_released(
        self, db: Database
    ) -> None:
        # Defence in depth for the freelist case: without it, a forgotten value can sit
        # in a page marked free until something else happens to need that page.
        row = await db.fetch_one("PRAGMA secure_delete")

        assert row is not None
        assert row[0] == 1


class TestFileMode:
    """0600, on the database and on both sidecars.

    Nothing in this file is encrypted, so this is the only thing between a person's record
    and every other process on the box. SQLite creates its files 0644.
    """

    async def test_the_database_is_readable_only_by_its_owner(self, db: Database) -> None:
        assert stat.S_IMODE(db.path.stat().st_mode) == DATABASE_FILE_MODE

    @pytest.mark.parametrize("suffix", SIDECARS)
    async def test_each_sidecar_is_too(self, db: Database, suffix: str) -> None:
        # The write-ahead log holds committed rows that have not been checkpointed yet,
        # and the shared-memory index maps them. A readable sidecar is a readable database
        # with an extra step.
        await db.execute("INSERT INTO t (x, y) VALUES (1, ?)", (SENTINEL,))
        companion = sidecar(db, suffix)

        assert companion.exists()
        assert stat.S_IMODE(companion.stat().st_mode) == DATABASE_FILE_MODE

    async def test_reopening_an_existing_database_tightens_it_again(self, tmp_path: Path) -> None:
        # A file restored from a backup, or copied between hosts, arrives with whatever
        # mode the copy gave it.
        path = tmp_path / "reopened.db"
        first = Database(path)
        await first.aclose()
        path.chmod(0o644)

        second = Database(path)
        try:
            assert stat.S_IMODE(path.stat().st_mode) == DATABASE_FILE_MODE
        finally:
            await second.aclose()

    async def test_a_directory_it_creates_is_owner_only(self, tmp_path: Path) -> None:
        # A world-readable directory says which files exist even when none can be read.
        database = Database(tmp_path / "fresh" / "user.db")
        try:
            assert stat.S_IMODE((tmp_path / "fresh").stat().st_mode) == 0o700
        finally:
            await database.aclose()

    async def test_it_leaves_a_directory_it_did_not_create_alone(self, tmp_path: Path) -> None:
        # The configured path names a file, so its parent may be somewhere shared that
        # this service has no business tightening -- /var/lib, at the extreme.
        existing = tmp_path / "shared"
        existing.mkdir(mode=0o755)

        database = Database(existing / "user.db")
        try:
            assert stat.S_IMODE(existing.stat().st_mode) == 0o755
        finally:
            await database.aclose()

    def test_making_a_database_private_skips_sidecars_that_are_not_there(
        self, tmp_path: Path
    ) -> None:
        # Called on a database with no write-ahead log yet, which is what a database that
        # has been opened and not written to looks like.
        lonely = tmp_path / "lonely.db"
        lonely.touch(mode=0o644)

        make_private(lonely)

        assert stat.S_IMODE(lonely.stat().st_mode) == DATABASE_FILE_MODE
        assert not any_sidecar_exists(lonely)


class TestCheckpointing:
    async def test_a_truncating_checkpoint_empties_the_write_ahead_log(self, db: Database) -> None:
        """This is the step that actually erases.

        A ``DELETE`` takes the row out of the b-tree and leaves the bytes it held in the
        ``-wal`` file, where they stay until a checkpoint moves them. A purge that stopped
        at the ``DELETE`` would leave the forgotten value in a sidecar next to the
        database, readable with ``grep``.
        """
        await db.execute("INSERT INTO t (x, y) VALUES (1, ?)", (SENTINEL,))
        wal = sidecar(db, "-wal")
        assert wal.stat().st_size > 0

        await db.checkpoint_truncate()

        assert wal.stat().st_size == 0

    async def test_the_rows_survive_the_checkpoint(self, db: Database) -> None:
        # Truncating the log is not a way of losing the log's contents: the checkpoint
        # moves them into the database first, which is the whole point of the mode.
        await db.execute("INSERT INTO t (x, y) VALUES (1, 'kept')")

        await db.checkpoint_truncate()

        row = await db.fetch_one("SELECT y FROM t WHERE x = 1")
        assert row is not None
        assert row["y"] == "kept"


class TestClosing:
    async def test_closing_twice_is_harmless(self, tmp_path: Path) -> None:
        # Shutdown runs from a lifespan handler that may itself be unwinding an error, so
        # a second close is an ordinary thing to happen rather than a caller's mistake.
        database = Database(tmp_path / "twice.db")

        await database.aclose()
        await database.aclose()

    async def test_what_was_written_survives_the_close(self, tmp_path: Path) -> None:
        path = tmp_path / "survivor.db"
        first = Database(path)
        await first.execute("CREATE TABLE t (x TEXT NOT NULL) STRICT")
        await first.execute("INSERT INTO t (x) VALUES ('kept')")
        await first.aclose()

        second = Database(path)
        try:
            rows = await second.fetch_all("SELECT x FROM t")
        finally:
            await second.aclose()

        assert [row["x"] for row in rows] == ["kept"]
