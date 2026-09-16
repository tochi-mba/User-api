"""Every adapter satisfies the port it claims to.

These look like tautologies and are not, for two reasons.

**A Protocol is structural, so nothing checks it unless something asks.** An adapter can
drift from its port -- a renamed parameter, a dropped keyword, a return type that narrowed
-- and nothing fails until a caller happens to hit the changed method. The annotated
assignment below is what makes mypy compare the two, and the ``isinstance`` is what makes
the runtime agree at the shape level.

**A port module is otherwise never imported at runtime.** Every consumer imports its port
under ``TYPE_CHECKING``, which is correct -- the whole point is that the adapter is chosen
at the composition root -- but it means the module's own class body never executes. Under
the 100% branch coverage gate that reads as an untested file, which is misleading in the
one direction that matters: the port is the contract, and a contract nothing imports is a
contract nothing checks.

So this file is also the answer to "why is the settings port at 0%". It is the only place
that imports these at runtime, and it exists to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from user_api.entries.sql_store import SqlEntryStore
from user_api.entries.store import EntryStore
from user_api.events.log import EventLog
from user_api.events.sql_log import SqlEventLog
from user_api.users.settings import SettingsStore
from user_api.users.sql_settings import SqlSettingsStore
from user_api.users.sql_store import SqlUserStore
from user_api.users.store import UserStore

if TYPE_CHECKING:
    from user_api.storage.database import Database


class TestAdaptersSatisfyTheirPorts:
    def test_the_event_log_adapter_is_an_event_log(self, database: Database) -> None:
        # Annotated first so mypy checks the assignment, then isinstance so the runtime
        # agrees. Either alone catches half of a drift: mypy misses a method a
        # runtime_checkable Protocol would notice is absent, and isinstance misses a
        # signature that changed shape without changing its name.
        checked: EventLog = SqlEventLog(database=database)

        assert isinstance(checked, EventLog)

    def test_the_entry_store_adapter_is_an_entry_store(self, database: Database) -> None:
        checked: EntryStore = SqlEntryStore(
            database=database, events=SqlEventLog(database=database)
        )

        assert isinstance(checked, EntryStore)

    def test_the_user_store_adapter_is_a_user_store(self, database: Database) -> None:
        checked: UserStore = SqlUserStore(database=database)

        assert isinstance(checked, UserStore)

    def test_the_settings_adapter_is_a_settings_store(self, database: Database) -> None:
        # erasure_mode, grace_days and log_values stay behind this port because the
        # sweeper has no user token to present to settings-api. Request-path caps
        # (max_pinned, search_default_limit) are read from settings-api in
        # core.preferences instead, and never through this store.
        checked: SettingsStore = SqlSettingsStore(database=database)

        assert isinstance(checked, SettingsStore)
