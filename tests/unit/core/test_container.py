"""The composition root: what it wires, and what it gives back when wiring fails."""

from __future__ import annotations

import gc
import threading
import warnings
from typing import TYPE_CHECKING

import pytest

from tests.conftest import build_settings
from tests.fakes.clock import FakeClock
from user_api.core.container import Container

if TYPE_CHECKING:
    from pathlib import Path


class TestARefusedBuildLeaksNothing:
    """A build that fails after opening the database must close it.

    Migration runs after the open and is *meant* to raise when the schema is not what this
    build expects. That refusal used to drop the open database -- file handle, WAL sidecars
    and worker thread -- which Python 3.13 reports as a ResourceWarning at collection and
    this suite treats as a failure.
    """

    def test_a_migration_that_fails_closes_the_database_it_was_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_: object, **__: object) -> None:
            msg = "the schema is not what this build expects"
            raise RuntimeError(msg)

        monkeypatch.setattr("user_api.core.container.migrate", refuse)
        threads_before = threading.active_count()

        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            with pytest.raises(RuntimeError, match="schema"):
                Container.build(build_settings(tmp_path), clock=FakeClock())
            gc.collect()
        assert threading.active_count() == threads_before, "the worker thread was given back"
