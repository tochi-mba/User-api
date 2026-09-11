"""Router registry.

Adding an API means writing a router module and adding it here. Everything cross-cutting --
problem+json errors, the request id, the account binding, access logging, the version
prefix -- is inherited from the app factory, so a new endpoint starts consistent with the
existing ones rather than having to remember to be. See the recipe in AGENTS.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from user_api.api.routers import entries, health, settings, user

if TYPE_CHECKING:
    from fastapi import APIRouter

ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    user.router,
    entries.router,
    settings.router,
)
"""Every router the application serves, in the order they are mounted.

``user`` before ``entries`` matters: both are mounted at ``/v1/user`` and Starlette matches
in order, so the literal ``/v1/user/schema`` and ``/v1/user/export`` have to be registered
before anything that could shadow them.
"""

__all__ = ["ROUTERS"]
