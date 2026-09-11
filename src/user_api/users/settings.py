"""What a settings store promises. The SQL adapter is :mod:`user_api.users.sql_settings`.

This port exists on day one for a reason that has nothing to do with today: **a separate
settings-api is the next service in this family, and it becomes a second adapter.** When
it does, nothing above this line changes -- not the service, not the routers, not a single
test that is written against the port rather than against the table.

That is worth one file now because the alternative is well understood: settings that begin
as three columns on a user table and are still three columns on a user table when four
services need them, at which point moving them is a migration, a backfill, a dual-write
window and a rollback plan. A port is the cheap moment to make that decision, and the
cheap moment is now.

The values themselves are :class:`~user_api.domain.settings.UserSettings`, which is a
domain type, so the port traffics in the domain's vocabulary rather than in rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from user_api.domain.settings import ErasureMode, UserSettings


@runtime_checkable
class SettingsStore(Protocol):
    """One account's choices about their own data."""

    async def get(self, account_id: str, *, default_grace_days: int) -> UserSettings:
        """This account's settings, or the defaults if they have never set any.

        Never ``None``. An account that has expressed no preference has the default
        preference, and a caller forced to handle ``None`` is a caller with a branch in
        which erasure mode is undefined -- which is the one place an unhandled ``None``
        would mean "quietly kept the data".

        ``default_grace_days`` is passed in rather than read here because it is
        deployment configuration, and a store that read configuration would be a store
        that needs configuration injected to be tested.
        """
        ...

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
        """Change some settings and leave the rest. Returns the whole updated set.

        ``None`` means "leave alone" rather than "set to null", which is unambiguous here
        because none of these three has a meaningful null.

        ``default_grace_days`` is required for the same reason :meth:`get` takes it, and
        for one that is easy to miss: this call may be the one that *creates* the row, and
        the settings it was not asked to change have to come from somewhere. Without it
        they come from the domain defaults, so an account on a deployment with a seven-day
        window whose first ever settings change was ``log_values`` would silently acquire
        a thirty-day one. Passing it here means an untouched setting keeps the value the
        account already had, whether that value was stored or merely configured.

        Changing the mode is **not retroactive**. Switching from ``tombstone`` to
        ``grace`` does not schedule everything already tombstoned for destruction, and
        switching to ``immediate`` does not purge what is already waiting out a grace
        period. A settings change that silently destroyed data would be the worst possible
        surprise in this service, and there is a test named after it.
        """
        ...
