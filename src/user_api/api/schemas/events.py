"""Wire models for the change log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from user_api.domain.entries import Action, EntryType
from user_api.events.log import Event


class EventResponse(BaseModel):
    """One recorded change.

    An event outlives the entry it describes: the record that something was forgotten
    survives the thing that was forgotten, which is the point of keeping a log at all. So
    an ``entry_id`` here may name an entry that no longer exists.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "sequence": 412,
                    "at": "2026-03-02T18:40:00.000000+00:00",
                    "action": "field.set",
                    "entry_id": "9f2c1e4a7b8d4f10a3c5e7b9d1f3a5c7",
                    "entry_type": "field",
                    "key": "preferred_name",
                    "asserted_by": "user",
                    "source": "stated",
                    "detail": None,
                }
            ]
        }
    )

    sequence: int = Field(
        description="Total order. Page with ?before=, not by timestamp -- two changes in "
        "the same tick share a timestamp."
    )
    at: datetime
    action: Action
    entry_id: str | None = Field(default=None, description="May name a destroyed entry.")
    entry_type: EntryType | None = Field(default=None)
    key: str | None = Field(default=None)
    asserted_by: str = Field(description="The token audience that made the change.")
    source: str | None = Field(default=None, description="What that writer claimed.")
    detail: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Old and new values, present only if the account turned log_values on. null "
            "is the default and does not mean nothing changed."
        ),
    )

    @classmethod
    def of(cls, event: Event) -> EventResponse:
        return cls(
            sequence=event.sequence,
            at=event.at,
            action=event.action,
            entry_id=event.entry_id,
            entry_type=event.entry_type,
            key=event.key,
            asserted_by=event.asserted_by,
            source=event.source,
            detail=event.detail,
        )


class EventPage(BaseModel):
    """One page of the change log, newest first."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"events": [], "count": 0}]})

    events: list[EventResponse]
    count: int
    next_before: int | None = Field(
        default=None, description="Pass back as ?before= for the next page."
    )
