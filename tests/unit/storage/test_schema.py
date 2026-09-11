"""The checked-in schema snapshot, and what it is for.

``scripts/dump_schema.py`` renders every object a fresh migration run produces into
``src/user_api/storage/schema.sql``. This test asserts the file still matches.

The point is not that a snapshot is authoritative -- the migrations are. The point is that
**a change to the schema shows up as a diff in a review**. Without this, a migration that
drops an index, loosens a CHECK, or forgets a cascade is a few lines of SQL that nobody
reads carefully; with it, the same change also rewrites a file whose entire content is the
schema, and the reviewer sees exactly what moved.

That matters more here than in most services, because three of the things in that file are
load-bearing in a way SQL does not announce:

* the ``ON DELETE CASCADE`` from ``entries`` to ``entry_scopes``, which is what makes a
  purge take an entry's scopes with it;
* the ``CHECK`` that keeps the field/note discriminator honest;
* the partial indexes, which every retrieval path depends on to serve its ORDER BY.

A migration that quietly changed any of them would still pass every other test in this
suite, because every other test goes through code that would simply get slower or looser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from user_api.storage.migrator import MIGRATIONS_DIR

if TYPE_CHECKING:
    from pathlib import Path

    from user_api.storage.database import Database

SNAPSHOT = MIGRATIONS_DIR.parent / "schema.sql"

LOAD_BEARING = (
    # Named individually rather than only compared wholesale, so a failure says WHICH
    # guarantee went rather than "the schema changed".
    "ON DELETE CASCADE",
    "WHERE forgotten_at IS NULL",
    "USING fts5",
    "porter unicode61",
    "STRICT",
)


async def _render(database: Database) -> str:
    """Every object in a freshly migrated database, in the snapshot's format."""
    rows = await database.fetch_all(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
    )
    return "".join(f"{row['sql']};\n" for row in rows)


class TestSchemaSnapshot:
    async def test_the_snapshot_matches_what_the_migrations_produce(
        self, database: Database
    ) -> None:
        # The database fixture is already migrated, which is the whole comparison: run the
        # migrations, render the result, and it must equal the file in the repository.
        assert await _render(database) == SNAPSHOT.read_text(), (
            "the schema drifted from its snapshot; run `make schema` and review the diff"
        )

    @pytest.mark.parametrize("fragment", LOAD_BEARING)
    async def test_a_guarantee_the_schema_carries_is_still_in_it(
        self, database: Database, fragment: str
    ) -> None:
        # Each of these is a promise made in code that only the schema can keep. A
        # migration that dropped one would pass every other test in this suite, because
        # every other test reaches the schema through code that would merely get looser.
        assert fragment in await _render(database)

    async def test_every_table_the_service_writes_to_exists(self, database: Database) -> None:
        rendered = await _render(database)

        for table in (
            "users",
            "user_settings",
            "entries",
            "entry_scopes",
            "entry_search",
            "events",
        ):
            assert f"CREATE TABLE {table}" in rendered or f"TABLE {table}" in rendered


class TestSnapshotIsRegenerable:
    async def test_the_dump_script_reproduces_the_checked_in_file(self, tmp_path: Path) -> None:
        # Not a tautology: the script builds its own database in a temporary directory,
        # so this also proves the migrations are runnable from nothing rather than only
        # from whatever state a developer's file happens to be in.
        from scripts.dump_schema import dump

        assert await dump() == SNAPSHOT.read_text()
