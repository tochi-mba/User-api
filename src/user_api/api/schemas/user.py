"""Wire models for the record itself: the always-load block, the schema, and erasure."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from user_api.api.schemas.entries import EntryResponse
from user_api.domain.entries import ValueType
from user_api.users.service import SchemaKey, UserView


class CountsResponse(BaseModel):
    """How much is here, within this token's scope."""

    fields: int = Field(description="Live fields this token can see.")
    notes: int = Field(description="Live notes this token can see.")
    pinned: int = Field(description="Entries in the always-load block.")
    forgotten: int = Field(
        description="Forgotten but not yet destroyed. Reach them with include_forgotten."
    )
    events: int = Field(description="Recorded changes.")


class UserResponse(BaseModel):
    """What ``GET /v1/user`` returns: the always-load block.

    One call at the start of a conversation. Counts, and every pinned entry this token may
    see -- deliberately not everything, because everything does not fit in a prompt.

    Never a 404. An account that has written nothing gets an empty record with null
    timestamps, because that is the truth rather than an error.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "account_id": "3f8a1c2b4d6e",
                    "granted_scope": "home",
                    "counts": {
                        "fields": 12,
                        "notes": 40,
                        "pinned": 5,
                        "forgotten": 1,
                        "events": 210,
                    },
                    "pinned": [],
                    "created_at": "2026-01-04T09:12:00.000000+00:00",
                    "updated_at": "2026-03-02T18:40:00.000000+00:00",
                }
            ]
        }
    )

    account_id: str = Field(
        description="Taken from your token's subject. There is no endpoint that accepts one."
    )
    granted_scope: str | None = Field(
        description=(
            "The compartment your token grants, from its audience. null means you see "
            "only entries that carry no scopes at all. You cannot change this from here: "
            "it is set when the person mints the token against their keyring session."
        )
    )
    counts: CountsResponse
    pinned: list[EntryResponse] = Field(
        description="The always-load set. Read these once; do not re-fetch per turn."
    )
    created_at: datetime | None = Field(default=None, description="null until the first write.")
    updated_at: datetime | None = Field(default=None)

    @classmethod
    def of(cls, view: UserView) -> UserResponse:
        """Render the service's view for the wire."""
        return cls(
            account_id=view.account_id,
            granted_scope=view.granted_scope,
            counts=CountsResponse(
                fields=view.counts.fields,
                notes=view.counts.notes,
                pinned=view.counts.pinned,
                forgotten=view.counts.forgotten,
                events=view.events,
            ),
            pinned=[EntryResponse.of(entry) for entry in view.pinned],
            created_at=view.record.created_at if view.record else None,
            updated_at=view.record.updated_at if view.record else None,
        )


class SchemaKeyResponse(BaseModel):
    """One key, described but not valued."""

    key: str
    set: bool = Field(
        description="Whether this key has a value. Well-known keys are listed either way."
    )
    well_known: bool = Field(
        description=(
            "A documented convention rather than a schema. Nothing enforces these; they "
            "exist so you can find the usual key instead of inventing a synonym."
        )
    )
    description: str | None = Field(default=None)
    value_type: ValueType | None = Field(default=None)
    scopes: list[str] = Field(default_factory=list)
    pinned: bool = Field(default=False)
    updated_at: datetime | None = Field(default=None)
    confirmed_at: datetime | None = Field(default=None)

    @classmethod
    def of(cls, key: SchemaKey) -> SchemaKeyResponse:
        return cls(
            key=key.key,
            set=key.set,
            well_known=key.well_known,
            description=key.description,
            value_type=ValueType(key.value_type) if key.value_type else None,
            scopes=list(key.scopes),
            pinned=key.pinned,
            updated_at=key.updated_at,
            confirmed_at=key.confirmed_at,
        )


class SchemaResponse(BaseModel):
    """What ``GET /v1/user/schema`` returns: keys and meanings, no values.

    Cheap enough to call before inventing a key, which is the entire point of it. Call it
    first, reuse a key that already means what you mean, and the record stays a record
    rather than fifty near-synonyms nobody can query.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "keys": [
                        {
                            "key": "preferred_name",
                            "set": True,
                            "well_known": True,
                            "description": "What to call them in conversation",
                            "value_type": "string",
                        }
                    ],
                    "count": 1,
                }
            ]
        }
    )

    keys: list[SchemaKeyResponse]
    count: int


class ErasedResponse(BaseModel):
    """What ``DELETE /v1/user`` destroyed.

    Counts only, and never what was in them -- rendering the contents of what was just
    erased into a response would undo the point of the call.

    This is always a hard destruction regardless of the account's erasure mode: "delete
    everything you know about me" has one honest meaning. What it cannot reach is backups
    taken before the call; see the operations guide.
    """

    model_config = ConfigDict(json_schema_extra={"examples": [{"entries": 52, "events": 210}]})

    entries: int = Field(description="Entries destroyed.")
    events: int = Field(description="Log records destroyed.")
