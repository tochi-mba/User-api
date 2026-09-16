"""Fixtures shared by the entry SQL store tests.

Both accounts exist before any entry does: ``entries.account_id`` is a foreign key to
``users``, and the connection refuses to open unless foreign keys really came on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT
from tests.fakes.clock import EPOCH
from user_api.users.sql_store import SqlUserStore

if TYPE_CHECKING:
    from user_api.storage.database import Database


@pytest.fixture(autouse=True)
async def _accounts(database: Database) -> None:
    """Both accounts exist before any entry does.

    ``entries.account_id`` is a foreign key to ``users``, and the connection refuses to
    open unless foreign keys really came on, so an entry written without this does not
    fail somewhere subtle later: it fails here.
    """
    users = SqlUserStore(database=database)
    await users.ensure(ACCOUNT, now=EPOCH)
    await users.ensure(OTHER_ACCOUNT, now=EPOCH)
