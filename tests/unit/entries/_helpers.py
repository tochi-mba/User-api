"""Builders shared by the entry SQL store tests.

Kept off the test modules so a case about search does not also carry the concurrent-write
harness, and so the defaults a test is *not* about live in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from tests.conftest import ACCOUNT
from tests.fakes.clock import EPOCH
from user_api.domain.cursors import Ordering
from user_api.domain.entries import Entry, NoteKind, Sensitivity, Source
from user_api.entries.store import Filters, Journal, Page

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Sequence

    from user_api.domain.cursors import Cursor
    from user_api.entries.sql_store import SqlEntryStore
    from user_api.storage.database import Database

SCHEDULER_TURNS = 8
"""Passes through the event loop, enough for every queued call to reach the database."""

ENTRY_CAP = 1_000
FIELD_CAP = 200
PIN_CAP = 10
"""High enough that a test about anything else cannot trip a limit by accident.

A test that is *about* a cap passes a low value for that one cap and leaves the other two
here, so a refusal can only have come from the limit under test.
"""

JOURNAL = Journal(cap=100)

LATER = EPOCH + timedelta(hours=1)
MUCH_LATER = EPOCH + timedelta(hours=2)

SCOPE_SETS: tuple[tuple[str, ...], ...] = ((), ("home",), ("health",), ("home", "work"))
GRANTS: tuple[str | None, ...] = (None, "home", "work", "health")


@contextlib.asynccontextmanager
async def database_held(database: Database) -> AsyncIterator[None]:
    """Occupy the database's only worker thread for the duration of the block.

    Everything submitted inside the block queues behind this and cannot begin, so a test
    can line several calls up and know that none of them has read anything yet.
    """
    started = threading.Event()
    release = threading.Event()

    def block(_connection: Any) -> None:
        started.set()
        release.wait(timeout=5)

    holding = asyncio.create_task(database.run(block))
    for _ in range(1000):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set(), "the database never picked the blocking call up"

    try:
        yield
    finally:
        release.set()
        await holding


async def park_behind_the_database(calls: Sequence[asyncio.Future[Any]]) -> None:
    """Let every call start and queue behind the held database.

    Asserting that none of them finished is what makes the concurrent tests mean
    something: it proves each call really is suspended before it has read anything,
    rather than having run to completion before the next one started.
    """
    for _ in range(SCHEDULER_TURNS):
        await asyncio.sleep(0)

    assert [call.done() for call in calls] == [False] * len(calls)


async def write_field(entries: SqlEntryStore, **overrides: Any) -> Entry:
    """Store one field, defaulted everywhere a test is not about."""
    arguments: dict[str, Any] = {
        "account_id": ACCOUNT,
        "key": "preferred_name",
        "value": "Sam",
        "description": "What to call them",
        "source": Source.STATED,
        "source_detail": None,
        "asserted_by": "user",
        "scopes": (),
        "sensitivity": Sensitivity.NORMAL,
        "pinned": False,
        "granted": None,
        "now": EPOCH,
        "entry_cap": ENTRY_CAP,
        "field_cap": FIELD_CAP,
        "pin_cap": PIN_CAP,
        "journal": JOURNAL,
    }
    return await entries.put_field(**{**arguments, **overrides})


async def write_note(entries: SqlEntryStore, **overrides: Any) -> Entry:
    """Append one note, defaulted everywhere a test is not about."""
    arguments: dict[str, Any] = {
        "account_id": ACCOUNT,
        "body": "They mentioned preferring tea to coffee.",
        "note_kind": NoteKind.OBSERVATION,
        "description": "A preference worth remembering",
        "source": Source.OBSERVED,
        "source_detail": None,
        "asserted_by": "user",
        "scopes": (),
        "sensitivity": Sensitivity.NORMAL,
        "pinned": False,
        "now": EPOCH,
        "entry_cap": ENTRY_CAP,
        "pin_cap": PIN_CAP,
        "journal": JOURNAL,
    }
    return await entries.write_note(**{**arguments, **overrides})


async def revise(
    entries: SqlEntryStore, entry: Entry, *, granted: str | None = None, **overrides: Any
) -> Entry:
    """Revise one entry, with everything a test does not care about defaulted."""
    arguments: dict[str, Any] = {
        "account_id": entry.account_id,
        "entry_id": entry.entry_id,
        "granted": granted,
        "now": LATER,
        "asserted_by": entry.asserted_by,
        "pin_cap": PIN_CAP,
        "journal": JOURNAL,
    }
    return await entries.revise(**{**arguments, **overrides})


async def confirm(
    entries: SqlEntryStore, entry: Entry, *, granted: str | None = None, **overrides: Any
) -> Entry:
    arguments: dict[str, Any] = {
        "account_id": entry.account_id,
        "entry_id": entry.entry_id,
        "granted": granted,
        "now": LATER,
        "asserted_by": entry.asserted_by,
        "journal": JOURNAL,
    }
    return await entries.confirm(**{**arguments, **overrides})


async def forget(
    entries: SqlEntryStore, entry: Entry, *, granted: str | None = None, **overrides: Any
) -> Entry:
    arguments: dict[str, Any] = {
        "account_id": entry.account_id,
        "entry_id": entry.entry_id,
        "granted": granted,
        "now": LATER,
        "asserted_by": entry.asserted_by,
        "journal": JOURNAL,
    }
    return await entries.forget(**{**arguments, **overrides})


async def purge(
    entries: SqlEntryStore, database: Database, entry_id: str, *, account_id: str = ACCOUNT
) -> bool:
    """Run the connection-taking purge in a transaction of its own, as erasure does."""
    return await database.transact(
        lambda connection: entries.purge_in(connection, account_id=account_id, entry_id=entry_id)
    )


async def find(
    entries: SqlEntryStore,
    *,
    account_id: str = ACCOUNT,
    granted: str | None = None,
    ordering: Ordering = Ordering.RECENT,
    limit: int = 20,
    cursor: Cursor | None = None,
    **filters: Any,
) -> Page:
    """Search with everything defaulted but the filters a test is about."""
    return await entries.search(
        account_id,
        granted=granted,
        filters=Filters(**filters),
        ordering=ordering,
        limit=limit,
        cursor=cursor,
    )


async def walk(
    entries: SqlEntryStore,
    *,
    ordering: Ordering,
    limit: int,
    cursor: Cursor | None = None,
    **filters: Any,
) -> list[str]:
    """Page through every entry, following the cursors until there are none left."""
    seen: list[str] = []
    while True:
        page = await find(entries, ordering=ordering, limit=limit, cursor=cursor, **filters)
        seen.extend(ids(page.entries))
        if page.next_cursor is None:
            return seen
        cursor = page.next_cursor


def ids(found: Iterable[Entry]) -> list[str]:
    return [entry.entry_id for entry in found]


def keys(found: Iterable[Entry]) -> list[str | None]:
    return [entry.key for entry in found]


async def count_rows(database: Database, sql: str, parameters: Sequence[object] = ()) -> int:
    return await database.count(f"SELECT count(*) AS total FROM {sql}", parameters)  # noqa: S608
