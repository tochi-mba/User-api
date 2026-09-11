"""Where forgetting becomes gone.

This module is the reason the measurement at the top of ADR-0005 was taken, and it is
worth restating the finding here because the code looks wrong without it.

``DELETE`` does not erase. With the row's page still holding live neighbours, the deleted
value survives in the ``-wal`` file -- and ``PRAGMA wal_checkpoint(FULL)`` does not remove
it either. Only ``TRUNCATE`` does. So the purge recipe is two steps that look like one
redundant step too many:

1. ``DELETE`` the entry, its scopes (by cascade) and its search row, and strip the values
   out of the events about it, **all in one transaction**;
2. ``PRAGMA wal_checkpoint(TRUNCATE)``, **once**, afterwards.

Step two is not housekeeping. Without it the purge reports success and the forgotten value
is still on disk next to the database, findable with ``grep``. There is a test that scans
the bytes of both files for a sentinel, and it is the one test in this suite that cannot
be replaced by a unit test, because it is about the file rather than about the code.

The checkpoint is deliberately once per *sweep* rather than once per row. It cannot run
inside a transaction, and running it per row turns a cheap step into a pathological one.
``VACUUM`` would also work and rewrites the whole database under a write lock to achieve
the same thing.

## What this cannot reach, and must say so

Backups taken before a purge still contain what was purged, and nothing here can reach into
them. Restoring an old backup resurrects forgotten entries. That is a property of backups
rather than a bug to fix here, and it is in ADR-0005 and in the operations guide because
the operator is the only person who can do anything about it.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

from user_api.core.logging import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import datetime

    from user_api.core.clock import Clock
    from user_api.entries.store import EntryStore
    from user_api.events.log import EventLog
    from user_api.storage.database import Database
    from user_api.users.settings import SettingsStore

logger = get_logger(__name__)

SWEEP_BATCH = 500
"""Entries purged per account per sweep.

Bounded so one account with a very large backlog cannot hold the single database thread
for an unbounded stretch. Whatever is left is picked up on the next sweep, which is the
right trade for a step that runs hourly.
"""


class Erasure:
    """Destroying what was forgotten, on the schedule the account chose."""

    # Six collaborators, which is what this needs: the two stores it destroys through, the
    # settings that say whether and when, the clock, and the transaction boundary itself.
    def __init__(  # noqa: PLR0913
        self,
        *,
        database: Database,
        entries: EntryStore,
        events: EventLog,
        settings: SettingsStore,
        clock: Clock,
        default_grace_days: int,
    ) -> None:
        self._db = database
        self._entries = entries
        self._events = events
        self._settings = settings
        self._clock = clock
        self._default_grace_days = default_grace_days

    async def purge_now(self, account_id: str, entry_id: str) -> None:
        """Destroy one entry immediately, bytes and all.

        The ``immediate`` erasure mode's half of ``DELETE /entries/{id}``: the caller has
        already marked the entry forgotten, and this makes that irreversible before the
        response goes out. Pays for its own checkpoint, which is the price of the promise
        that mode makes.
        """
        await self._db.transact(
            partial(self._purge_in, account_id=account_id, entry_ids=(entry_id,))
        )
        await self._db.checkpoint_truncate()

    async def sweep_once(self) -> int:
        """Purge everything whose grace period has run out. Returns how many entries went.

        Two passes rather than one clever query, because how long a forgotten entry
        survives is a *per-account* setting and whether it is ever destroyed at all is too.
        A single global "everything older than N" would apply one account's grace period to
        another's data and would purge a tombstone account's entries, which is the one
        thing that setting exists to prevent.
        """
        now = self._clock.now()
        purged = 0

        for account_id in await self._entries.accounts_with_forgotten():
            settings = await self._settings.get(
                account_id, default_grace_days=self._default_grace_days
            )
            if not settings.ever_purges:
                # Tombstone. The account has asked for the record of what it changed its
                # mind about to outlive the change, and a sweep is not a place to overrule
                # somebody's settings.
                continue

            due = await self._entries.due_for_purge(
                account_id,
                before=_cutoff(now, days=settings.grace_days),
                limit=SWEEP_BATCH,
            )
            if not due:
                continue

            # partial rather than a lambda closing over the loop variable: a lambda would
            # capture the name and every queued callable would purge the last account's
            # ids. The default-argument workaround binds correctly and defeats inference.
            await self._db.transact(partial(self._purge_in, account_id=account_id, entry_ids=due))
            purged += len(due)
            # Counts and an account id. Never an entry id, a key or anything from a row:
            # "we destroyed the entry about your diagnosis" is the log line this avoids.
            logger.info("entries_purged", account_id=account_id, purged=len(due))

        if purged:
            # Once, after everything. This is the step that actually removes the bytes;
            # see the module docstring for the measurement that says so.
            await self._db.checkpoint_truncate()

        return purged

    def _purge_in(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        entry_ids: Sequence[str],
    ) -> None:
        """Destroy a batch of entries and the values logged about them, as one unit.

        Both halves in one transaction: between two, there is a moment where the entry is
        gone and its old value is still sitting in the event that recorded the change.
        """
        for entry_id in entry_ids:
            self._entries.purge_in(connection, account_id=account_id, entry_id=entry_id)
            self._events.purge_entry_values_in(connection, entry_id=entry_id)


def _cutoff(now: datetime, *, days: int) -> datetime:
    """The instant before which a forgotten entry is due to be destroyed.

    A grace of zero days means the cutoff is ``now``, so an entry forgotten in an earlier
    request goes on the next sweep -- which is what somebody who set it to zero meant, and
    is still not the same thing as the ``immediate`` mode, which does not wait for a sweep
    at all.
    """
    return now - timedelta(days=days)
