"""Which version is running, and what it says when nothing installed knows."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from importlib import metadata
from typing import TYPE_CHECKING

import user_api
from user_api.core.version import DISTRIBUTION_NAME, service_version

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def nothing_installed() -> Iterator[None]:
    """Make every installed distribution genuinely unfindable for the duration.

    The real condition is a checkout that was never installed, so it is reproduced rather
    than simulated: :mod:`importlib.metadata` discovers distributions by walking
    ``sys.path``, and an empty path finds none. Nothing in the module under test is
    replaced, so the fallback is reached the same way a container that copies the source
    in reaches it.
    """
    saved = list(sys.path)
    sys.path.clear()
    try:
        yield
    finally:
        sys.path[:] = saved


def test_it_reports_the_installed_distribution_version() -> None:
    assert service_version() == metadata.version(DISTRIBUTION_NAME)


def test_it_falls_back_to_the_source_constant_when_nothing_is_installed() -> None:
    # Running from a checkout that was never installed is normal in development and in a
    # container that copies the source in. Reporting no version at all would make /healthy
    # less useful exactly where it is read most, so the source constant stands in.
    with nothing_installed():
        assert service_version() == user_api.__version__
