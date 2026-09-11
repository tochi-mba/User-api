"""Settings as a row: v1 of the port, and the first of what will be two adapters.

The port above this file exists for a reason that has nothing to do with SQLite. The next
service in this family is a settings-api, and when it lands it becomes a second
implementation of :class:`~user_api.users.settings.SettingsStore` -- chosen at the
composition root, with nothing above the port changing: not the service, not the routers,
not a test written against the port rather than against the table. This module is the
version for as long as there is only one service, and it is deliberately the dull one.

That is the whole argument for three values behind a port on day one rather than three
columns somebody has to prise out of a user table later, once four services want them and
moving them costs a migration, a backfill, a dual-write window and a rollback plan.

**There is no row until somebody sets something, and that is not an error.** An account
that has expressed no preference has the default preference, so :meth:`SqlSettingsStore.get`
answers with a whole :class:`~user_api.domain.settings.UserSettings` and never with
``None``. A caller made to handle ``None`` is a caller with a branch in which the erasure
mode is undefined, and that is the one branch where forgetting to decide means quietly
keeping the data.

**The row needs its user.** ``user_settings.account_id`` is a foreign key into ``users``,
so an upsert for an account with no record fails the constraint rather than creating one.
The service calls :meth:`~user_api.users.store.UserStore.ensure` first and this module
leans on that instead of inserting a users row of its own, because a store that creates
the thing it points at is a store that can resurrect an account ``DELETE /v1/user`` has
just erased.

**Both methods take ``default_grace_days``, and ``update`` needs it as much as ``get``.**
An update may be the call that creates the row, and the settings it was not asked to change
have to come from somewhere. Taking the domain's own defaults instead looks harmless while
the two numbers agree -- and on a deployment configured for a seven-day window, an account
whose first ever settings change was to ``log_values`` would come away with a thirty-day
one it never asked for and nothing would say so. An untouched setting keeps the value the
account already had, and for a row that does not exist yet that value is the deployment's,
not the domain's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from user_api.domain.settings import ErasureMode, UserSettings
from user_api.storage.times import to_column

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from user_api.storage.database import Database

SETTINGS_COLUMNS = "account_id, erasure_mode, grace_days, log_values, updated_at"

SELECT_SETTINGS = f"SELECT {SETTINGS_COLUMNS} FROM user_settings WHERE account_id = ?"  # noqa: S608

UPSERT_SETTINGS = (
    f"INSERT INTO user_settings ({SETTINGS_COLUMNS}) VALUES (?, ?, ?, ?, ?)"  # noqa: S608
    " ON CONFLICT (account_id) DO UPDATE SET"
    "   erasure_mode = COALESCE(?, erasure_mode),"
    "   grace_days = COALESCE(?, grace_days),"
    "   log_values = COALESCE(?, log_values),"
    "   updated_at = ?"
)
"""Upsert where an omitted setting keeps whatever is stored.

The COALESCE is against the *column*, which in a DO UPDATE means the value already in the
row rather than the one this statement proposed -- so a caller changing one setting cannot
overwrite the other two with the nulls it passed for them. Doing the same by reading the
row, merging in Python and writing it back would work inside this transaction too; this
way "``None`` means leave alone" is said once, in the place that does it.
"""


class SqlSettingsStore:
    """One account's choices, in one row, defaulted rather than absent."""

    def __init__(self, *, database: Database) -> None:
        self._db = database

    async def get(self, account_id: str, *, default_grace_days: int) -> UserSettings:
        row = await self._db.fetch_one(SELECT_SETTINGS, (account_id,))
        # The mode and the logging default live on UserSettings, so they are not restated
        # here; only the grace window is the deployment's to choose.
        if row is None:
            return UserSettings(grace_days=default_grace_days)
        return _settings_of(row)

    # account, clock, the deployment default, and one parameter per setting. Every
    # one is independent, and an options object would only be unpacked again here.
    async def update(  # noqa: PLR0913
        self,
        account_id: str,
        *,
        now: datetime,
        default_grace_days: int,
        erasure_mode: ErasureMode | None = None,
        grace_days: int | None = None,
        log_values: bool | None = None,
    ) -> UserSettings:
        mode = None if erasure_mode is None else erasure_mode.value
        flag = None if log_values is None else int(log_values)
        # What an account that had never set anything would have been reading. The INSERT
        # arm falls back to these, so creating the row cannot change a setting the caller
        # did not mention.
        fresh = UserSettings(grace_days=default_grace_days)

        def write(connection: sqlite3.Connection) -> UserSettings:
            stamp = to_column(now)
            connection.execute(
                UPSERT_SETTINGS,
                (
                    account_id,
                    fresh.erasure_mode.value if mode is None else mode,
                    fresh.grace_days if grace_days is None else grace_days,
                    int(fresh.log_values) if flag is None else flag,
                    stamp,
                    mode,
                    grace_days,
                    flag,
                    stamp,
                ),
            )
            # One transaction with the write, so the row is certain to be here and there
            # is no None to branch on. Read back rather than assembled from the arguments,
            # because what the caller wants to be told is what is stored.
            row = connection.execute(SELECT_SETTINGS, (account_id,)).fetchone()
            return _settings_of(row)

        return await self._db.transact(write)


def _settings_of(row: sqlite3.Row) -> UserSettings:
    return UserSettings(
        erasure_mode=ErasureMode(row["erasure_mode"]),
        grace_days=int(row["grace_days"]),
        log_values=bool(row["log_values"]),
    )
