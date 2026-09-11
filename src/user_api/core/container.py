"""The composition root.

Every adapter is chosen and wired here, once, and handed to the app. Nothing else
constructs its own dependencies -- which is what makes the whole service testable by
substitution, and what keeps "which settings store" a configuration decision rather than
a code one. When the settings-api lands, one line in this file changes and nothing above
the port notices.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from user_api.auth.jwks import JwksClient
from user_api.auth.tokens import TokenVerifier
from user_api.core.clock import SystemClock
from user_api.core.logging import get_logger
from user_api.entries.sql_store import SqlEntryStore
from user_api.events.sql_log import SqlEventLog
from user_api.storage.database import Database
from user_api.storage.migrator import migrate
from user_api.users.erasure import Erasure
from user_api.users.service import UserService
from user_api.users.sql_settings import SqlSettingsStore
from user_api.users.sql_store import SqlUserStore

if TYPE_CHECKING:
    from user_api.core.clock import Clock
    from user_api.core.config import Settings
    from user_api.entries.store import EntryStore
    from user_api.events.log import EventLog
    from user_api.users.settings import SettingsStore
    from user_api.users.store import UserStore

logger = get_logger(__name__)


@dataclass(slots=True)
class Container:
    """Everything the API needs, already wired together."""

    settings: Settings
    clock: Clock
    database: Database
    # The ports, not the adapters. Annotating these with the concrete classes would make
    # every consumer depend on which adapter was wired, which is the one thing a
    # composition root exists to prevent.
    users: UserStore
    entries: EntryStore
    events: EventLog
    user_settings: SettingsStore
    erasure: Erasure
    service: UserService
    jwks: JwksClient
    verifier: TokenVerifier
    started_monotonic: float
    _sweeper: asyncio.Task[None] | None = None

    @classmethod
    def build(cls, settings: Settings, *, clock: Clock | None = None) -> Container:
        """Construct every adapter named by ``settings``.

        Nothing here reaches keyring. The JWKS client is constructed and does not fetch:
        a service that refused to start unless keyring were reachable would turn one
        outage into two, at the moment these two services are being restarted together.
        """
        clock = clock or SystemClock()
        database = Database(settings.database_path)
        migrate(database, now=clock.now())

        events = SqlEventLog(database=database)
        entries = SqlEntryStore(database=database, events=events)
        users = SqlUserStore(database=database)
        user_settings = SqlSettingsStore(database=database)
        erasure = Erasure(
            database=database,
            entries=entries,
            events=events,
            settings=user_settings,
            clock=clock,
            default_grace_days=settings.default_grace_days,
        )
        jwks = JwksClient(
            url=settings.keyring_jwks_url,
            clock=clock,
            cache_seconds=settings.jwks_cache_seconds,
            min_refetch_seconds=settings.jwks_min_refetch_seconds,
            timeout_seconds=settings.keyring_http_timeout_seconds,
        )

        return cls(
            settings=settings,
            clock=clock,
            database=database,
            users=users,
            entries=entries,
            events=events,
            user_settings=user_settings,
            erasure=erasure,
            service=UserService(
                users=users,
                entries=entries,
                events=events,
                settings=user_settings,
                erasure=erasure,
                database=database,
                clock=clock,
                config=settings,
            ),
            jwks=jwks,
            verifier=TokenVerifier(
                jwks=jwks,
                issuer=settings.keyring_issuer,
                audience_prefix=settings.audience_prefix,
                allowed_scopes=settings.allowed_scopes,
                clock=clock,
            ),
            started_monotonic=clock.monotonic(),
        )

    @property
    def uptime_seconds(self) -> float:
        return self.clock.monotonic() - self.started_monotonic

    def start_sweeper(self) -> None:
        """Begin destroying entries whose grace period has run out."""
        self._sweeper = asyncio.create_task(self._sweep_forever(), name="erasure-sweeper")

    async def aclose(self) -> None:
        """Shut everything down in dependency order."""
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None

        await self.jwks.aclose()
        # Last: everything above may still want to write on its way out.
        await self.database.aclose()

    async def _sweep_forever(self) -> None:
        """Sweep, then wait, rather than wait, then sweep.

        The other order has a gap nobody would guess at from the outside: entries whose
        grace period expired while the service was stopped would sit there for a further
        whole interval after it came back, because the first thing the loop did was sleep
        for an hour. A person who deleted something yesterday and restarted the service
        this morning is entitled to have it gone this morning.
        """
        while True:
            await self._sweep_guarded()
            await asyncio.sleep(self.settings.purge_interval_seconds)

    async def _sweep_guarded(self) -> None:
        """Run one sweep, surviving any failure.

        A sweep failure must not kill the sweeper: the next tick tries again. Without this
        the first transient error would silently stop all erasure, and entries somebody
        asked to have destroyed would sit there indefinitely with nothing saying so -- a
        broken promise that looks exactly like a working service.
        """
        try:
            await self.erasure.sweep_once()
        except Exception:
            logger.exception("sweep_failed")
