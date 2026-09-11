"""One account's choices about their own data.

The store is dull on purpose and gets tested anyway, because two of its three behaviours
are the sort that look right and are not: a missing row means the *deployment's* defaults
rather than the domain's, and an update that touches one setting must not quietly restate
the other two.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.fakes.clock import EPOCH, FakeClock
from user_api.domain.settings import ErasureMode, UserSettings
from user_api.users.settings import SettingsStore
from user_api.users.sql_settings import SqlSettingsStore
from user_api.users.sql_store import SqlUserStore

if TYPE_CHECKING:
    from user_api.storage.database import Database

DEPLOYMENT_GRACE = 7
"""Deliberately not the domain default of 30, so a test cannot pass by coincidence."""


@pytest.fixture
async def settings_store(database: Database) -> SqlSettingsStore:
    """A settings store with both accounts' records already in place.

    The settings row has a foreign key to the record, and the service calls ``ensure``
    before it writes. Doing it here rather than inside the store is the point: a store
    that created the thing it points at could resurrect an account ``DELETE /v1/user``
    had just erased.
    """
    users = SqlUserStore(database=database)
    await users.ensure(ACCOUNT, now=EPOCH)
    await users.ensure(OTHER_ACCOUNT, now=EPOCH)
    return SqlSettingsStore(database=database)


class TestReading:
    async def test_an_account_that_has_set_nothing_gets_the_defaults(
        self, settings_store: SqlSettingsStore
    ) -> None:
        held = await settings_store.get(ACCOUNT, default_grace_days=DEPLOYMENT_GRACE)

        assert held == UserSettings(
            erasure_mode=ErasureMode.GRACE, grace_days=DEPLOYMENT_GRACE, log_values=False
        )

    async def test_the_grace_window_comes_from_the_deployment_not_the_domain(
        self, settings_store: SqlSettingsStore
    ) -> None:
        # Configuration is passed in rather than read here, so the store needs nothing
        # injected to be tested -- and so a deployment that chose a week does not silently
        # get the domain's month.
        held = await settings_store.get(ACCOUNT, default_grace_days=DEPLOYMENT_GRACE)

        assert held.grace_days == DEPLOYMENT_GRACE != UserSettings().grace_days

    async def test_it_never_answers_with_nothing(self, settings_store: SqlSettingsStore) -> None:
        # A caller made to handle None is a caller with a branch in which the erasure mode
        # is undefined, and that is the one branch where forgetting to decide means
        # quietly keeping the data.
        assert isinstance(
            await settings_store.get("an-account-that-has-never-existed", default_grace_days=1),
            UserSettings,
        )


class TestWriting:
    @pytest.mark.parametrize("mode", list(ErasureMode))
    async def test_every_erasure_mode_survives_the_round_trip(
        self, settings_store: SqlSettingsStore, mode: ErasureMode
    ) -> None:
        await settings_store.update(
            ACCOUNT, now=EPOCH, default_grace_days=DEPLOYMENT_GRACE, erasure_mode=mode
        )

        assert (
            await settings_store.get(ACCOUNT, default_grace_days=DEPLOYMENT_GRACE)
        ).erasure_mode is mode

    @pytest.mark.parametrize("flag", [True, False])
    async def test_log_values_survives_the_round_trip_as_a_bool(
        self, settings_store: SqlSettingsStore, flag: bool
    ) -> None:
        # Stored as an INTEGER, so a store that forgot to rebuild the bool would hand back
        # 0 and 1 -- both truthy-looking in the wrong places.
        updated = await settings_store.update(
            ACCOUNT, now=EPOCH, default_grace_days=DEPLOYMENT_GRACE, log_values=flag
        )

        assert updated.log_values is flag

    async def test_updating_one_setting_leaves_the_others_exactly_as_they_were(
        self, settings_store: SqlSettingsStore
    ) -> None:
        await settings_store.update(
            ACCOUNT,
            now=EPOCH,
            default_grace_days=DEPLOYMENT_GRACE,
            erasure_mode=ErasureMode.TOMBSTONE,
            grace_days=90,
            log_values=True,
        )

        after = await settings_store.update(
            ACCOUNT, now=EPOCH, default_grace_days=DEPLOYMENT_GRACE, grace_days=14
        )

        assert after == UserSettings(
            erasure_mode=ErasureMode.TOMBSTONE, grace_days=14, log_values=True
        )

    async def test_creating_the_row_uses_the_deployments_window_for_what_was_not_asked_for(
        self, settings_store: SqlSettingsStore
    ) -> None:
        # This was a real defect. An update may be the call that CREATES the row, and the
        # settings it was not asked to change have to come from somewhere; they came from
        # the domain's defaults, so an account on a seven-day deployment whose first ever
        # settings change was log_values silently acquired a thirty-day window.
        created = await settings_store.update(
            ACCOUNT, now=EPOCH, default_grace_days=DEPLOYMENT_GRACE, log_values=True
        )

        assert created.grace_days == DEPLOYMENT_GRACE

    async def test_an_update_that_asks_for_nothing_changes_nothing(
        self, settings_store: SqlSettingsStore
    ) -> None:
        before = await settings_store.update(
            ACCOUNT,
            now=EPOCH,
            default_grace_days=DEPLOYMENT_GRACE,
            erasure_mode=ErasureMode.IMMEDIATE,
        )

        after = await settings_store.update(ACCOUNT, now=EPOCH, default_grace_days=DEPLOYMENT_GRACE)

        assert after == before

    async def test_a_zero_day_grace_is_a_value_rather_than_an_omission(
        self, settings_store: SqlSettingsStore
    ) -> None:
        # None means "leave alone" and 0 is a legal window, so the two have to be
        # distinguishable. A merge that tested truthiness would turn "destroy at the next
        # sweep" back into whatever was there before.
        updated = await settings_store.update(
            ACCOUNT, now=EPOCH, default_grace_days=DEPLOYMENT_GRACE, grace_days=0
        )

        assert updated.grace_days == 0

    async def test_the_stamp_it_records_is_the_injected_one(
        self, database: Database, settings_store: SqlSettingsStore
    ) -> None:
        clock = FakeClock()
        clock.advance(3600)

        await settings_store.update(
            ACCOUNT, now=clock.now(), default_grace_days=DEPLOYMENT_GRACE, grace_days=3
        )

        row = await database.fetch_one(
            "SELECT updated_at FROM user_settings WHERE account_id = ?", (ACCOUNT,)
        )
        assert row is not None
        assert row["updated_at"].startswith(clock.now().date().isoformat())


class TestIsolation:
    async def test_one_accounts_settings_are_not_anothers(
        self, settings_store: SqlSettingsStore
    ) -> None:
        await settings_store.update(
            ACCOUNT,
            now=EPOCH,
            default_grace_days=DEPLOYMENT_GRACE,
            erasure_mode=ErasureMode.IMMEDIATE,
        )

        theirs = await settings_store.get(OTHER_ACCOUNT, default_grace_days=DEPLOYMENT_GRACE)

        assert theirs.erasure_mode is ErasureMode.GRACE


class TestThePort:
    async def test_the_adapter_satisfies_it(self, settings_store: SqlSettingsStore) -> None:
        # The port is the thing settings-api will implement next. An adapter that drifted
        # from it would still work today and fail on the day of the swap.
        checked: SettingsStore = settings_store

        assert isinstance(checked, SettingsStore)
