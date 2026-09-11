"""Fields, notes, and the one flexible read over both.

``GET /v1/user/entries`` is a single endpoint with a dozen optional parameters rather than
six narrow ones, and that is a decision about the caller. When the caller is a model
choosing a tool, one endpoint it can combine is easier than six it has to choose between --
and a model that picks the wrong narrow endpoint gets a confidently empty answer, which is
the worst failure available.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from user_api.api.dependencies import ContainerDep, IdentityDep
from user_api.api.schemas.common import Problem
from user_api.api.schemas.entries import (
    EntryPage,
    EntryResponse,
    ReviseEntryRequest,
    SetFieldRequest,
    WriteNoteRequest,
)
from user_api.domain.cursors import Ordering
from user_api.domain.entries import EntryType, NoteKind, Sensitivity, Source
from user_api.entries.store import UNSET, Filters

router = APIRouter(prefix="/v1/user", tags=["entries"])

_AUTH: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": Problem},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Problem},
}
_READ: dict[int | str, dict[str, Any]] = {
    **_AUTH,
    status.HTTP_404_NOT_FOUND: {
        "model": Problem,
        "description": (
            "No such entry. Identical whether it never existed, was forgotten, belongs to "
            "another account, or is outside your token's scope -- you cannot tell those "
            "apart, by design."
        ),
    },
}
_WRITE: dict[int | str, dict[str, Any]] = {
    **_AUTH,
    status.HTTP_403_FORBIDDEN: {
        "model": Problem,
        "description": "Your token does not grant a scope you asked to write.",
    },
    status.HTTP_409_CONFLICT: {
        "model": Problem,
        "description": "A per-account limit is full, or the key is taken out of scope.",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": Problem,
        "description": (
            "The value is malformed, too large, too deeply nested, or looks like a "
            "credential -- credentials belong in keyring, not here."
        ),
    },
}


@router.get(
    "/entries",
    operation_id="search_user",
    summary="Find entries, by text or by any combination of filters",
    description=(
        "The one flexible read. Every parameter is optional and they combine.\n\n"
        "`q` is full-text over keys, descriptions, values and note bodies, stemmed, so "
        "'preferring' finds 'prefer'. With `q` the results are ranked by relevance; "
        "without it they are ordered by `order`.\n\n"
        "Useful combinations: `?pinned=true` for the always-load set; `?keys=a,b,c` to "
        "fetch several named fields in one call; `?key_prefix=contact_` for a family of "
        "them; `?stale_before=<a year ago>` for what nobody has confirmed lately, which is "
        "how you find what to ask about; `?include_forgotten=true` to review what was "
        "deleted and undo it.\n\n"
        "`scope` narrows within what your token already grants. It cannot widen it -- "
        "asking for a scope you do not hold is refused rather than silently empty."
    ),
    response_model=EntryPage,
    responses=_READ,
)
# One parameter per filter, which is what a single flexible read endpoint IS. FastAPI
# reads these positionally-declared parameters to build the OpenAPI document, so they
# cannot be collected into an object without losing the documentation a model reads.
async def search_user(  # noqa: PLR0913, PLR0917
    container: ContainerDep,
    identity: IdentityDep,
    q: Annotated[str | None, Query(max_length=200, description="Full-text query.")] = None,
    type: Annotated[  # noqa: A002 -- the query parameter's public name
        EntryType | None, Query(description="'field' or 'note'.")
    ] = None,
    keys: Annotated[str | None, Query(description="Comma-separated field keys.")] = None,
    key_prefix: Annotated[str | None, Query(max_length=64)] = None,
    note_kind: Annotated[NoteKind | None, Query()] = None,
    source: Annotated[Source | None, Query()] = None,
    asserted_by: Annotated[str | None, Query(max_length=64)] = None,
    sensitivity: Annotated[Sensitivity | None, Query()] = None,
    pinned: Annotated[bool | None, Query()] = None,
    scope: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[datetime | None, Query(description="updated_at at or after.")] = None,
    until: Annotated[datetime | None, Query(description="updated_at before.")] = None,
    stale_before: Annotated[
        datetime | None,
        Query(description="confirmed_at before this, or never confirmed at all."),
    ] = None,
    include_forgotten: Annotated[bool, Query()] = False,
    order: Annotated[
        Ordering,
        Query(
            description=(
                "'recent' (default) or 'oldest'. Ignored when q is given, which always "
                "ranks by relevance. Prefer 'oldest' when walking everything: created_at "
                "never moves, so a concurrent write cannot make the walk skip a row."
            )
        ),
    ] = Ordering.RECENT,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> EntryPage:
    """Search and filter."""
    page = await container.service.search(
        identity,
        filters=Filters(
            query=q,
            entry_type=type,
            keys=tuple(_split(keys)) if keys else None,
            key_prefix=key_prefix,
            note_kind=note_kind,
            source=source,
            asserted_by=asserted_by,
            sensitivity=sensitivity,
            pinned=pinned,
            scope=scope,
            since=since,
            until=until,
            stale_before=stale_before,
            include_forgotten=include_forgotten,
        ),
        ordering=order,
        limit=limit,
        cursor=cursor,
    )
    entries = [EntryResponse.of(entry) for entry in page.entries]
    return EntryPage(
        entries=entries,
        count=len(entries),
        next_cursor=page.next_cursor.encode() if page.next_cursor else None,
    )


@router.get(
    "/entries/{entry_id}",
    operation_id="get_entry",
    summary="Read one entry",
    description=(
        "Returns both `updated_at` and `confirmed_at`. They are not the same: the second "
        "is the last time a human said it was still true. Say 'you told me in March' "
        "rather than asserting a stale fact as current."
    ),
    response_model=EntryResponse,
    responses=_READ,
)
async def get_entry(entry_id: str, container: ContainerDep, identity: IdentityDep) -> EntryResponse:
    """One entry by id."""
    return EntryResponse.of(await container.service.get_entry(identity, entry_id))


@router.patch(
    "/entries/{entry_id}",
    operation_id="revise_entry",
    summary="Change part of an entry",
    description=(
        "Omitted fields are left alone; `revision` is bumped. `confirmed_at` is "
        "deliberately NOT touched -- a revision is you changing what is held, a "
        "confirmation is the person saying it is still true. Use confirm_entry for that."
    ),
    response_model=EntryResponse,
    responses={**_READ, **_WRITE},
)
async def revise_entry(
    entry_id: str,
    request: ReviseEntryRequest,
    container: ContainerDep,
    identity: IdentityDep,
) -> EntryResponse:
    """Partially revise an entry."""
    # `value` is the one field where "omitted" and "sent as null" must differ, because
    # null is a legal field value. model_fields_set is the only thing that knows which
    # happened -- a plain `is None` check would silently turn "unset it" into "leave it".
    sent = request.model_fields_set
    entry = await container.service.revise_entry(
        identity,
        entry_id,
        value=request.value if "value" in sent else UNSET,
        body=request.body,
        description=request.description,
        sensitivity=request.sensitivity,
        pinned=request.pinned,
        scopes=tuple(request.scopes) if request.scopes is not None else None,
        source=request.source,
        source_detail=request.source_detail,
    )
    return EntryResponse.of(entry)


@router.delete(
    "/entries/{entry_id}",
    operation_id="forget_entry",
    summary="Forget one entry",
    description=(
        "Invisible from every read immediately. What happens to the bytes is the "
        "account's `erasure_mode` setting, not yours: 'grace' (default) destroys it after "
        "the grace period and leaves it recoverable until then, 'immediate' destroys it "
        "before this response returns, 'tombstone' never destroys it.\n\n"
        "All three answer identically, so you do not need to know which one you are "
        "talking to. Returns the entry with forgotten_at set."
    ),
    response_model=EntryResponse,
    responses=_READ,
)
async def forget_entry(
    entry_id: str, container: ContainerDep, identity: IdentityDep
) -> EntryResponse:
    """Forget an entry."""
    return EntryResponse.of(await container.service.forget_entry(identity, entry_id))


@router.post(
    "/entries/{entry_id}/confirm",
    operation_id="confirm_entry",
    summary="Record that this is still true",
    description=(
        "Sets `confirmed_at` to now and changes nothing else -- not the value, not "
        "`updated_at`, not `revision`. The cheap half of keeping a record honest: find "
        "what is stale with `?stale_before=`, ask about it, and call this when the person "
        "says it still holds."
    ),
    response_model=EntryResponse,
    responses=_READ,
)
async def confirm_entry(
    entry_id: str, container: ContainerDep, identity: IdentityDep
) -> EntryResponse:
    """Re-confirm an entry."""
    return EntryResponse.of(await container.service.confirm_entry(identity, entry_id))


@router.put(
    "/fields/{key}",
    operation_id="set_field",
    summary="Set a named fact about this person",
    description=(
        "Creates or replaces the one field with this key. PUT rather than POST because "
        "the key is the identity: retrying after a timeout cannot produce two fields.\n\n"
        "Keys are normalised -- 'Preferred Name', 'preferred-name' and 'PREFERRED_NAME' "
        "are one key. **Call describe_schema first** and reuse a key that already means "
        "what you mean.\n\n"
        "Writing the SAME value again counts as a confirmation and moves `confirmed_at` "
        "forward. Writing a DIFFERENT value clears it, because the new value has not been "
        "vouched for by anyone yet.\n\n"
        "`asserted_by` is taken from your token; sending one in the body does nothing."
    ),
    response_model=EntryResponse,
    responses=_WRITE,
)
async def set_field(
    key: str, request: SetFieldRequest, container: ContainerDep, identity: IdentityDep
) -> EntryResponse:
    """Create or replace a field."""
    entry = await container.service.set_field(
        identity,
        key=key,
        value=request.value,
        description=request.description,
        source=request.source,
        source_detail=request.source_detail,
        scopes=tuple(request.scopes),
        sensitivity=request.sensitivity,
        pinned=request.pinned,
    )
    return EntryResponse.of(entry)


@router.get(
    "/fields/{key}",
    operation_id="get_field",
    summary="Read one named fact",
    description=(
        "The key is normalised first, so any spelling of it finds the same field. Use "
        "search_user with ?keys=a,b,c to fetch several in one call."
    ),
    response_model=EntryResponse,
    responses=_READ,
)
async def get_field(key: str, container: ContainerDep, identity: IdentityDep) -> EntryResponse:
    """One field by key."""
    return EntryResponse.of(await container.service.get_field(identity, key))


@router.post(
    "/notes",
    operation_id="write_note",
    summary="Write down something that happened, was noticed, or was learned",
    description=(
        "Notes have no key and are never replaced -- each call creates one. Pick the kind "
        "honestly: 'episode' (something happened), 'observation' (something noticed), "
        "'lesson' (something to do differently). The kind is what makes '?note_kind=lesson' "
        "worth asking a year from now.\n\n"
        "Be honest about `source` too. 'inferred' is not a lesser answer, and a person "
        "reading their own record deserves to know which of it they actually said."
    ),
    status_code=status.HTTP_201_CREATED,
    response_model=EntryResponse,
    responses=_WRITE,
)
async def write_note(
    request: WriteNoteRequest, container: ContainerDep, identity: IdentityDep
) -> EntryResponse:
    """Append a note."""
    entry = await container.service.write_note(
        identity,
        body=request.body,
        note_kind=request.note_kind,
        description=request.description,
        source=request.source,
        source_detail=request.source_detail,
        scopes=tuple(request.scopes),
        sensitivity=request.sensitivity,
        pinned=request.pinned,
    )
    return EntryResponse.of(entry)


def _split(raw: str) -> list[str]:
    """Split a comma-separated parameter, dropping blanks.

    Blanks dropped rather than passed through: ``?keys=a,,b`` is a caller assembling a
    list with a loop that had an empty element, and an empty key matches nothing but would
    silently make the whole request return less than it should.
    """
    return [part.strip() for part in raw.split(",") if part.strip()]
