"""The package imports, reports a version, and ships its typing marker.

Trivial on its face. It is the test that fails first when the packaging metadata, the
source layout and the installed distribution disagree with each other, which is a failure
that otherwise surfaces as something unrelated in a deployment.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import user_api
from user_api.core.version import DISTRIBUTION_NAME

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
PACKAGE_ROOT = Path(user_api.__file__).resolve().parent


def declared_version() -> str:
    """The version as ``pyproject.toml`` states it, read rather than assumed."""
    with PYPROJECT.open("rb") as handle:
        project: dict[str, str] = tomllib.load(handle)["project"]
    return project["version"]


def test_the_package_exposes_a_three_part_version() -> None:
    assert user_api.__version__.count(".") == 2


def test_the_source_constant_and_the_packaging_metadata_agree() -> None:
    # They are written in two places, so they are compared in one. A disagreement means
    # /healthy reports one version and the wheel somebody deployed is another.
    assert user_api.__version__ == declared_version()


def test_the_installed_distribution_reports_the_same_version() -> None:
    assert metadata.version(DISTRIBUTION_NAME) == declared_version()


def test_the_typing_marker_is_shipped_beside_the_package() -> None:
    # Without it, every service that imports this one gets `Any` for all of it, and the
    # strict type checking here stops at this package's own boundary.
    assert (PACKAGE_ROOT / "py.typed").is_file()
