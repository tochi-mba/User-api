"""The server entry point.

Four lines, and they are the line between "the tests pass" and "the process starts". It is
also the one place where a setting becomes how the server actually binds, and the one
place a misspelled ``USER_API_`` variable gets to stop a deployment before it serves
anything on a default nobody chose.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
import uvicorn

from user_api.__main__ import main
from user_api.core.config import UnknownSettingError

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def environment(**values: str) -> Iterator[None]:
    """Set environment variables for the duration of the block, restoring them after."""
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, was in previous.items():
            if was is None:
                del os.environ[name]
            else:
                os.environ[name] = was


class Recorder:
    """A hand-written stand-in for ``uvicorn.run`` that records how it was called."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, app: Any, **options: Any) -> None:
        self.calls.append({"app": app, **options})


@contextmanager
def serving_recorded() -> Iterator[Recorder]:
    """Put a recorder where ``uvicorn.run`` is, and put ``uvicorn.run`` back afterwards.

    The only attribute swap in this suite. Everything else is injected, but ``main``
    exists precisely in order to call ``uvicorn.run`` and has no seam to inject through --
    and the alternative, actually binding a socket, is not a unit test. The restore is in
    a ``finally`` so a failing assertion cannot leave the swap behind for another test.
    """
    recorder = Recorder()
    saved = uvicorn.run
    uvicorn.run = recorder
    try:
        yield recorder
    finally:
        uvicorn.run = saved


def test_it_serves_the_app_factory_on_the_configured_address() -> None:
    # A documentation-range address, so the assertion is unmistakably about the setting
    # being read rather than about the default happening to match.
    with (
        serving_recorded() as recorder,
        environment(USER_API_HOST="192.0.2.10", USER_API_PORT="9123"),
    ):
        main()

    call = recorder.calls[0]
    assert call["app"] == "user_api.api.app:create_app"
    # By factory rather than by instance, so the app is built inside the worker process
    # that will serve it and every worker gets its own connection to the database.
    assert call["factory"] is True
    assert call["host"] == "192.0.2.10"
    assert call["port"] == 9123


def test_it_leaves_the_logging_configuration_to_us() -> None:
    # uvicorn installs its own handlers unless told not to, and access lines written
    # through them would bypass the redaction processor entirely.
    with serving_recorded() as recorder:
        main()

    assert recorder.calls[0]["log_config"] is None


def test_a_misspelled_variable_stops_the_server_before_it_binds_anything() -> None:
    # Loudly, at startup, rather than serving on a default that nobody chose and that
    # nothing in the logs mentions.
    with serving_recorded() as recorder:
        with environment(USER_API_HSOT="192.0.2.10"), pytest.raises(UnknownSettingError):
            main()

        assert recorder.calls == []
