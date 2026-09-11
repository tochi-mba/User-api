"""Shared fixtures.

Every test that touches the app builds its own, so nothing leaks between cases.

Two things here are worth knowing before writing a test against them.

**The clock is fake everywhere, including inside the app.** :func:`create_app` builds its
own container, so the only way to inject a clock into a live application is to build the
container and hand it over -- which :func:`app` does. Nothing in this suite sleeps.

**Keyring is a real RSA key and a real JWKS document over a hand-written transport.** See
:mod:`tests.fakes.keyring`. There is no ``unittest.mock`` in this suite: a fake that
satisfies the real shape fails to type-check when the shape changes, and a patched
attribute does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from tests.fakes.clock import EPOCH, FakeClock
from tests.fakes.keyring import ISSUER, JWKS_URL, FakeKeyring, mint
from user_api.api.app import create_app
from user_api.core.config import LogFormat, Settings
from user_api.core.container import Container
from user_api.entries.sql_store import SqlEntryStore
from user_api.events.sql_log import SqlEventLog
from user_api.storage.database import Database
from user_api.storage.migrator import migrate

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

ACCOUNT = "account-a"
OTHER_ACCOUNT = "account-b"
SCOPES = ("home", "work", "health")


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Test settings, built through validation.

    Overrides go through the constructor rather than ``model_copy(update=...)``, which
    skips validators -- so a scope list that the validator would reject would be accepted
    here and fail somewhere far away instead.
    """
    defaults: dict[str, Any] = {
        "_env_file": None,
        "database_path": tmp_path / "user.db",
        "log_format": LogFormat.CONSOLE,
        "keyring_issuer": ISSUER,
        "keyring_jwks_url": JWKS_URL,
        "allowed_scopes": SCOPES,
        # High enough that ordinary cases never trip a limit by accident. Tests that are
        # *about* a limit build their own settings with a low one.
        "max_entries_per_account": 1_000,
        "max_fields_per_account": 200,
        "max_pinned": 10,
        "max_events": 500,
    }
    return Settings(**{**defaults, **overrides})


@pytest.fixture
def clock() -> FakeClock:
    """One clock, shared by the app and by the test that moves it."""
    return FakeClock()


@pytest.fixture
def keyring() -> FakeKeyring:
    """A keyring serving one signing key, counting how often it is asked."""
    return FakeKeyring()


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """A migrated database on a real file.

    A real file rather than ``:memory:`` on purpose. Durability is one of the properties
    this storage exists for, and the erasure tests read the file's **bytes** -- which an
    in-memory database does not have.
    """
    db = Database(tmp_path / "user.db")
    migrate(db, now=EPOCH)
    try:
        yield db
    finally:
        await db.aclose()


@pytest.fixture
def events(database: Database) -> SqlEventLog:
    return SqlEventLog(database=database)


@pytest.fixture
def entries(database: Database, events: SqlEventLog) -> SqlEntryStore:
    return SqlEntryStore(database=database, events=events)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a scratch directory, with limits tuned for tests."""
    return build_settings(tmp_path)


@pytest.fixture
def app(settings: Settings, clock: FakeClock, keyring: FakeKeyring) -> FastAPI:
    """An app wired to the fake clock and the fake keyring.

    The container is built here and parked on the app before the lifespan runs, and
    :func:`user_api.api.app.start` then finds it already there. That is the one seam this
    suite needs: ``create_app`` deliberately builds its own container, and a test that
    could not substitute the clock could not test a grace period without waiting a month.
    """
    built = create_app(settings)
    container = Container.build(settings, clock=clock)
    container.jwks._client = AsyncClient(transport=keyring.transport())
    built.state.container = container
    built.state.prebuilt = container
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired straight to the ASGI app, with lifespan run for real."""
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://user.test") as http,
    ):
        yield http


def container_of(app: FastAPI) -> Container:
    """Reach the wired container, for tests that inspect or substitute an adapter."""
    container: Container = app.state.container
    return container


def token(account_id: str = ACCOUNT, *, scope: str | None = None, **overrides: Any) -> str:
    """A token for one account, optionally granting one scope.

    The audience is assembled here rather than passed, because the audience *is* the
    grant: ``user`` alone reads unscoped entries and ``user.health`` reads health ones.
    Writing it out at every call site would make that relationship easy to get wrong in
    exactly the tests that are about it.
    """
    audience = "user" if scope is None else f"user.{scope}"
    return mint(account_id=account_id, audience=audience, **overrides)


def auth(value: str) -> dict[str, str]:
    """The Authorization header for a token."""
    return {"Authorization": f"Bearer {value}"}


async def set_field(
    client: AsyncClient, tok: str, key: str = "preferred_name", **body: Any
) -> dict[str, Any]:
    """Store a field and return it. Raises on anything but success, so a test that meant
    to set up state cannot silently continue with none.
    """
    payload = {"value": "Sam", "description": "What to call them", **body}
    response = await client.put(f"/v1/user/fields/{key}", json=payload, headers=auth(tok))
    assert response.status_code == 200, response.text
    stored: dict[str, Any] = response.json()
    return stored


async def write_note(client: AsyncClient, tok: str, **body: Any) -> dict[str, Any]:
    """Append a note and return it."""
    payload = {
        "body": "They mentioned preferring tea to coffee.",
        "note_kind": "observation",
        "description": "A preference worth remembering",
        **body,
    }
    response = await client.post("/v1/user/notes", json=payload, headers=auth(tok))
    assert response.status_code == 201, response.text
    written: dict[str, Any] = response.json()
    return written
