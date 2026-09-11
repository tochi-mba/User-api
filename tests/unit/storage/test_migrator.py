"""Numbered SQL files, applied in order, once each, atomically.

Two of these are about failures that are silent rather than loud. Ordering by the number
rather than by the filename is what stops a tenth migration from sorting between the first
and the second and building a schema in the wrong order. The rollback is what stops a
migration that failed halfway from leaving half a schema behind with no version row to say
it was ever tried -- after which every later start tries to create the same table again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.fakes.clock import EPOCH
from user_api.storage.database import Database
from user_api.storage.migrator import Migration, discover, migrate
from user_api.storage.times import from_column

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def blank(tmp_path: Path) -> AsyncIterator[Database]:
    """An open database with no schema at all, which is what a first start meets."""
    database = Database(tmp_path / "blank.db")
    try:
        yield database
    finally:
        await database.aclose()


async def table_names(database: Database) -> set[str]:
    rows = await database.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


class TestDiscovery:
    def test_it_finds_the_migrations_shipped_inside_the_package(self) -> None:
        found = discover()

        assert len(found) > 0
        assert found[0].version == 1
        assert found[0].name == "0001_initial.sql"

    def test_it_orders_by_the_number_rather_than_by_the_filename(self, tmp_path: Path) -> None:
        # "10_tenth" sorts between "1_first" and "2_second" as text, which would run the
        # tenth migration against the schema of the first and fail in a way that reads
        # like a broken migration rather than a broken ordering.
        for name in ("2_second.sql", "10_tenth.sql", "1_first.sql"):
            (tmp_path / name).write_text("SELECT 1;")

        assert [migration.version for migration in discover(tmp_path)] == [1, 2, 10]

    def test_a_migration_knows_its_own_file_name_for_the_log_line(self, tmp_path: Path) -> None:
        assert Migration(tmp_path / "0007_seventh.sql").name == "0007_seventh.sql"


class TestApplying:
    async def test_it_builds_the_schema_and_says_how_much_it_applied(self, blank: Database) -> None:
        applied = migrate(blank, now=EPOCH)

        assert applied == len(discover())
        assert {"users", "entries", "entry_scopes", "events"} <= await table_names(blank)

    async def test_it_records_what_it_applied_and_when(self, blank: Database) -> None:
        # The version row and the DDL are written in one transaction, so this is also how
        # a half-applied migration would announce itself.
        migrate(blank, now=EPOCH)

        rows = await blank.fetch_all("SELECT version, applied_at FROM schema_version")

        assert [row["version"] for row in rows] == [step.version for step in discover()]
        assert from_column(rows[0]["applied_at"]) == EPOCH

    async def test_running_it_again_applies_nothing(self, blank: Database) -> None:
        # Which is what makes it safe to call on every start rather than only on the first.
        migrate(blank, now=EPOCH)

        assert migrate(blank, now=EPOCH) == 0

    async def test_running_it_again_changes_nothing(self, blank: Database) -> None:
        migrate(blank, now=EPOCH)
        before = await table_names(blank)

        migrate(blank, now=EPOCH)

        assert await table_names(blank) == before

    async def test_it_applies_only_the_steps_that_are_missing(
        self, blank: Database, tmp_path: Path
    ) -> None:
        directory = tmp_path / "steps"
        directory.mkdir()
        (directory / "0001_one.sql").write_text("CREATE TABLE one (x TEXT NOT NULL) STRICT;")
        migrate(blank, now=EPOCH, directory=directory)

        (directory / "0002_two.sql").write_text("CREATE TABLE two (x TEXT NOT NULL) STRICT;")

        assert migrate(blank, now=EPOCH, directory=directory) == 1
        assert {"one", "two"} <= await table_names(blank)


class TestAtomicity:
    async def test_a_migration_that_fails_partway_leaves_the_schema_untouched(
        self, blank: Database, tmp_path: Path
    ) -> None:
        """SQLite has transactional DDL, and this is the test that says we rely on it.

        Without the rollback the first table would exist, unrecorded, and every later
        start would try to create it again and fail forever on a database nobody can
        migrate forwards or backwards.
        """
        directory = tmp_path / "broken"
        directory.mkdir()
        (directory / "0001_half.sql").write_text(
            "CREATE TABLE good (x TEXT NOT NULL) STRICT;\nCREATE TABLE bad (;"
        )

        with pytest.raises(Exception, match="syntax error"):
            migrate(blank, now=EPOCH, directory=directory)

        assert "good" not in await table_names(blank)
        assert await blank.fetch_all("SELECT version FROM schema_version") == []

    async def test_a_failing_version_row_rolls_the_schema_back_with_it(
        self, blank: Database, tmp_path: Path
    ) -> None:
        """The DDL and the version row commit together or not at all.

        Forced here by having the migration insert its own conflicting version row, so the
        failure lands on the ``INSERT`` after a script that succeeded.
        """
        directory = tmp_path / "conflict"
        directory.mkdir()
        (directory / "0001_clash.sql").write_text(
            "CREATE TABLE good (x TEXT NOT NULL) STRICT;\n"
            "INSERT INTO schema_version (version, applied_at) VALUES (1, 'earlier');"
        )
        blank.run_sync(
            lambda connection: connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "  version INTEGER NOT NULL PRIMARY KEY, applied_at TEXT NOT NULL) STRICT"
            )
        )

        with pytest.raises(Exception, match="UNIQUE constraint"):
            migrate(blank, now=EPOCH, directory=directory)

        assert "good" not in await table_names(blank)

    async def test_the_database_is_still_usable_after_a_failed_migration(
        self, blank: Database, tmp_path: Path
    ) -> None:
        # A rollback that left the transaction open would hold the write lock, and the
        # operator's next attempt after fixing the file would hang rather than work.
        directory = tmp_path / "broken"
        directory.mkdir()
        (directory / "0001_half.sql").write_text("CREATE TABLE bad (;")

        with pytest.raises(Exception, match="syntax error"):
            migrate(blank, now=EPOCH, directory=directory)

        assert migrate(blank, now=EPOCH) == len(discover())
