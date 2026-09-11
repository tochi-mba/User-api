"""Starting up, shutting down, and the sweeper that must survive its own failures.

Two properties here are about what happens when something else is broken. Startup must not
require keyring, because these two services are restarted together and a startup dependency
turns one outage into two. And a sweep that raises must not kill the sweeper, because the
first transient error would otherwise stop all erasure silently -- entries somebody asked
to have destroyed sitting there with nothing saying so.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from tests.conftest import auth, container_of, token
from user_api.api.app import create_app, start, stop
from user_api.core.container import Container
from user_api.entries.store import EntryStore
from user_api.events.log import EventLog
from user_api.users.settings import SettingsStore
from user_api.users.store import UserStore

if TYPE_CHECKING:
    from fastapi import FastAPI

    from tests.fakes.clock import FakeClock
    from tests.fakes.keyring import FakeKeyring
    from user_api.core.config import Settings


class TestStartupDoesNotNeedKeyring:
    async def test_nothing_is_fetched_while_starting_up(
        self, app: FastAPI, keyring: FakeKeyring
    ) -> None:
        # A service that refused to start unless keyring were reachable would turn one
        # outage into two, at the moment both are being restarted together.
        async with LifespanManager(app):
            assert keyring.fetches == 0

    async def test_it_serves_health_with_keyring_unreachable(
        self, app: FastAPI, keyring: FakeKeyring
    ) -> None:
        import httpx

        keyring.error = httpx.ConnectError("keyring is not there")

        async with (
            LifespanManager(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://app.test") as http,
        ):
            response = await http.get("/healthy")

        assert response.status_code == 503
        assert response.json()["checks"]["keyring"]["status"] == "degraded"

    async def test_health_survives_a_failure_nobody_anticipated(
        self, app: FastAPI, keyring: FakeKeyring
    ) -> None:
        # /healthy must never 500. A load balancer would see the same status for "keyring
        # is down" as for "this process is broken", and those need different people.
        keyring.error = RuntimeError("something nobody wrote a handler for")

        async with (
            LifespanManager(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://app.test") as http,
        ):
            response = await http.get("/healthy")

        assert response.status_code == 503
        assert response.json()["checks"]["keyring"]["status"] == "degraded"
        assert "Traceback" not in response.text

    async def test_the_first_token_is_what_provokes_the_first_fetch(
        self, app: FastAPI, keyring: FakeKeyring
    ) -> None:
        async with (
            LifespanManager(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://app.test") as http,
        ):
            assert keyring.fetches == 0
            await http.get("/v1/user", headers=auth(token()))
            assert keyring.fetches == 1


class TestTheContainer:
    def test_it_wires_every_port(self, settings: Settings, clock: FakeClock) -> None:
        # Annotated with the PORTS rather than the adapters, which is what stops every
        # consumer of the container from depending on which adapter was wired.
        container = Container.build(settings, clock=clock)

        checked_entries: EntryStore = container.entries
        checked_events: EventLog = container.events
        checked_users: UserStore = container.users
        checked_settings: SettingsStore = container.user_settings

        assert isinstance(checked_entries, EntryStore)
        assert isinstance(checked_events, EventLog)
        assert isinstance(checked_users, UserStore)
        assert isinstance(checked_settings, SettingsStore)

    def test_uptime_is_measured_on_the_injected_clock(
        self, settings: Settings, clock: FakeClock
    ) -> None:
        container = Container.build(settings, clock=clock)

        clock.advance(90)

        assert container.uptime_seconds == 90

    async def test_closing_without_a_sweeper_started_is_not_an_error(
        self, settings: Settings, clock: FakeClock
    ) -> None:
        container = Container.build(settings, clock=clock)

        await container.aclose()

    async def test_start_honours_a_container_a_test_already_built(
        self, app: FastAPI, settings: Settings
    ) -> None:
        # create_app building its own is what keeps production wiring in one place; a
        # suite that could not substitute the clock could not test a grace period without
        # waiting a month.
        prebuilt = container_of(app)

        assert start(app) is prebuilt
        await stop(prebuilt)

    def test_create_app_builds_its_own_when_nothing_is_parked(self, settings: Settings) -> None:
        built = create_app(settings)

        assert not hasattr(built.state, "prebuilt")
        assert built.state.settings is settings


class TestTheSweeper:
    async def test_a_sweep_that_raises_does_not_kill_the_sweeper(
        self, settings: Settings, clock: FakeClock
    ) -> None:
        # Without this the first transient error silently stops ALL erasure, and entries
        # somebody asked to have destroyed sit there with nothing saying so -- a broken
        # promise that looks exactly like a working service.
        container = Container.build(settings, clock=clock)
        container.erasure = _ErasureThatFails()  # type: ignore[assignment]

        await container._sweep_guarded()
        await container._sweep_guarded()

        assert container.erasure.attempts == 2  # type: ignore[attr-defined]
        await container.aclose()

    async def test_it_sweeps_before_it_waits(self, settings: Settings, clock: FakeClock) -> None:
        # The other order has a gap: entries whose grace expired while the service was
        # stopped would sit for a further whole interval after it came back, because the
        # first thing the loop did was sleep for an hour.
        container = Container.build(settings, clock=clock)
        counting = _ErasureThatCounts()
        container.erasure = counting  # type: ignore[assignment]

        container.start_sweeper()
        for _ in range(20):
            await asyncio.sleep(0)
            if counting.attempts:
                break

        assert counting.attempts >= 1
        await container.aclose()

    async def test_closing_stops_it(self, settings: Settings, clock: FakeClock) -> None:
        container = Container.build(settings, clock=clock)
        container.start_sweeper()

        await container.aclose()

        assert container._sweeper is None


class _ErasureThatFails:
    """An erasure path that always raises. Hand-written; the real one is not substitutable
    by configuration, and this asserts on how many times it was asked rather than on how."""

    def __init__(self) -> None:
        self.attempts = 0

    async def sweep_once(self) -> int:
        self.attempts += 1
        msg = "the sweep failed"
        raise RuntimeError(msg)


class _ErasureThatCounts:
    """An erasure path that succeeds and counts."""

    def __init__(self) -> None:
        self.attempts = 0

    async def sweep_once(self) -> int:
        self.attempts += 1
        return 0
