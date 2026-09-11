"""Settings and the change log: what a person has decided, and what has been done.

Both are the account's own, both are addressed without an account id, and both go through
the same token. There is no administrative surface in this service at all -- no operator
can read somebody's record over HTTP, because no route exists that would let them.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from user_api.api.dependencies import ContainerDep, IdentityDep
from user_api.api.schemas.common import Problem
from user_api.api.schemas.events import EventPage, EventResponse
from user_api.api.schemas.settings import SettingsResponse, UpdateSettingsRequest

router = APIRouter(prefix="/v1/user", tags=["settings"])

_PROBLEM: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": Problem},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Problem},
}


@router.get(
    "/settings",
    operation_id="get_settings",
    summary="Read this person's choices about their own data",
    description=(
        "Never 404s -- an account that has expressed no preference has the default "
        "preference. Worth reading before telling somebody what 'delete' will do for "
        "them, because the answer genuinely differs."
    ),
    response_model=SettingsResponse,
    responses=_PROBLEM,
)
async def get_settings(container: ContainerDep, identity: IdentityDep) -> SettingsResponse:
    """Read the settings."""
    return SettingsResponse.of(await container.service.get_settings(identity))


@router.put(
    "/settings",
    operation_id="update_settings",
    summary="Change what deletion and logging mean for this person",
    description=(
        "Omitted settings are left alone. Changes are **never retroactive**: switching to "
        "'immediate' does not destroy what is already waiting out a grace period, and "
        "switching away from 'tombstone' does not schedule what is already tombstoned. A "
        "settings change that silently destroyed data would be the worst surprise this "
        "service could produce.\n\n"
        "Do not change these on your own initiative. They are the person's decision about "
        "their own data; ask, then set what they asked for."
    ),
    response_model=SettingsResponse,
    responses=_PROBLEM,
)
async def update_settings(
    request: UpdateSettingsRequest, container: ContainerDep, identity: IdentityDep
) -> SettingsResponse:
    """Change some settings."""
    settings = await container.service.update_settings(
        identity,
        erasure_mode=request.erasure_mode,
        grace_days=request.grace_days,
        log_values=request.log_values,
    )
    return SettingsResponse.of(settings)


@router.get(
    "/events",
    operation_id="read_events",
    summary="Read the log of what has changed",
    description=(
        "Newest first. Page with `?before=<sequence>`, never by timestamp -- two changes "
        "in the same tick share one.\n\n"
        "`detail` carries old and new values only if the account turned `log_values` on; "
        "null is the default and does not mean nothing changed. An event outlives the "
        "entry it describes, so an entry_id here may name something that no longer exists "
        "-- which is the point: the record that something was forgotten survives the thing "
        "that was forgotten."
    ),
    response_model=EventPage,
    responses=_PROBLEM,
)
async def read_events(
    container: ContainerDep,
    identity: IdentityDep,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    before: Annotated[int | None, Query(ge=1, description="Sequence, exclusive.")] = None,
) -> EventPage:
    """One page of the change log."""
    events = await container.service.read_events(identity, limit=limit, before=before)
    rendered = [EventResponse.of(event) for event in events]
    return EventPage(
        events=rendered,
        count=len(rendered),
        next_before=rendered[-1].sequence if rendered else None,
    )
