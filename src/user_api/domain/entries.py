"""What an entry is.

One type covers both halves of the service, because fields and notes are one thing wearing
two hats. A *field* is a fact with a name -- ``preferred_name``, ``timezone``,
``blood_type`` -- and there is one live one per key. A *note* is something that happened,
was noticed, or was learned, and there are as many as the assistant writes.

They share scopes, provenance, pinning, forgetting, confirmation, revision and search.
Twelve of their fifteen columns are the same, which is the argument for one table
(ADR-0011) and for one dataclass here.

Every closed vocabulary in this module is a :class:`~enum.StrEnum` rather than a free
string, for the reason keyring's ``Permission`` is: a typo'd free string is a note that can
never be filtered for again, and nothing ever tells you it happened.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class EntryType(StrEnum):
    """Which half of the table a row is."""

    FIELD = "field"
    NOTE = "note"


class NoteKind(StrEnum):
    """What kind of thing a note records.

    Closed, and small on purpose. Three kinds a person would recognise, so a model picking
    one does not have to guess between eight near-synonyms -- and so ``?note_kind=lesson``
    means something a year later.
    """

    EPISODE = "episode"
    """Something happened. "We went to Lisbon in March.\""""

    OBSERVATION = "observation"
    """Something noticed. "Prefers to be asked before being given a summary.\""""

    LESSON = "lesson"
    """Something to do differently. "Do not suggest restaurants without checking dietary
    notes first.\""""


class ValueType(StrEnum):
    """The shape of a field's value.

    Derived from the value, never supplied by the caller, so the two cannot disagree.
    That is not pedantry: ``value_type`` is what a consumer switches on to decide how to
    render, and a caller-supplied one is a caller-supplied rendering bug.
    """

    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"
    LIST = "list"
    OBJECT = "object"


class Sensitivity(StrEnum):
    """How freely an assistant should volunteer this.

    A hint to the consumer about *volunteering*: "do not bring this up unprompted". It is
    explicitly **not** access control -- scopes are, and they are carried by the token. An
    entry marked ``sensitive`` is returned in full to any token whose scope permits it.

    Said plainly here, and again in the field description on the wire, so nobody mistakes
    it for a boundary and builds on it.
    """

    NORMAL = "normal"
    SENSITIVE = "sensitive"


class Source(StrEnum):
    """Where the writer says this came from.

    A *claim*, and the server cannot check it. Nothing stops an assistant writing
    ``stated`` for something it inferred, and nothing here pretends otherwise -- the API
    reports it beside :attr:`Entry.asserted_by`, which the server did verify, and labels
    which is which. See ADR-0006.

    Closed rather than free text for the filtering reason: "show me only what I actually
    told you" is the query that makes this field worth having, and it only works if the
    values are a small fixed set.
    """

    STATED = "stated"
    """The person said it, in their own words."""

    INFERRED = "inferred"
    """A model worked it out. The one that most deserves a "your notes suggest"."""

    OBSERVED = "observed"
    """Derived from what the person did rather than what they said."""

    IMPORTED = "imported"
    """Came in from a document, an export, or another system."""


class Action(StrEnum):
    """What an event records having happened."""

    FIELD_SET = "field.set"
    NOTE_WRITTEN = "note.written"
    ENTRY_REVISED = "entry.revised"
    ENTRY_CONFIRMED = "entry.confirmed"
    ENTRY_FORGOTTEN = "entry.forgotten"
    ENTRY_PURGED = "entry.purged"
    USER_DELETED = "user.deleted"
    SETTINGS_UPDATED = "settings.updated"


def new_entry_id() -> str:
    """Return a fresh entry id.

    Opaque and random rather than sequential. An entry id appears in URLs and in an
    assistant's transcript, and a sequential one would say how much this person has told
    us and roughly when -- which is not much, but it is not nothing and it is free to
    avoid.
    """
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class Entry:
    """One field or one note.

    Frozen: an entry that came out of the store is a snapshot of a row, and code that
    "just tweaks" one before writing it back is code that writes back stale neighbours.
    Revisions go through the store, which changes one thing and bumps
    :attr:`revision`.
    """

    entry_id: str
    account_id: str
    entry_type: EntryType
    description: str
    source: Source
    asserted_by: str
    created_at: datetime
    updated_at: datetime

    # Field-only. Non-null exactly when entry_type is FIELD; the database CHECK enforces
    # the pairing, so a reader does not have to defend against half a field.
    key: str | None = None
    value: object = None
    value_type: ValueType | None = None

    # Note-only, under the same guarantee.
    body: str | None = None
    note_kind: NoteKind | None = None

    scopes: tuple[str, ...] = ()
    """Empty means unrestricted: visible to any valid token.

    Non-empty means visible only to a token whose granted scope is among these. Stored in
    a junction table rather than here-as-JSON, for the read-path reason in ADR-0004.
    """

    sensitivity: Sensitivity = Sensitivity.NORMAL
    source_detail: str | None = None
    pinned: bool = False
    revision: int = 1
    confirmed_at: datetime | None = None
    forgotten_at: datetime | None = None
    rank: float | None = field(default=None, compare=False)
    """Search relevance, present only on rows that came back from a ranked query.

    Excluded from equality: two reads of the same entry are the same entry whether or not
    one of them arrived through a search. Without ``compare=False`` every test comparing a
    searched entry to a fetched one would fail for a reason that has nothing to do with
    what it is testing.
    """

    @property
    def is_forgotten(self) -> bool:
        """Whether this entry has been forgotten and is merely awaiting its purge."""
        return self.forgotten_at is not None

    @property
    def is_restricted(self) -> bool:
        """Whether any token at all is excluded from seeing this."""
        return bool(self.scopes)

    def visible_to(self, granted: str | None) -> bool:
        """Whether a token granting ``granted`` may see this entry.

        The rule in one line, and the same one the SQL implements: an entry with no scopes
        is visible to everybody, and an entry with scopes is visible only when the token's
        single granted scope is among them.

        Present here as well as in SQL on purpose. The SQL is the enforcement; this is the
        statement of the rule, and a test asserts the two agree. A boundary that exists
        only as a WHERE clause is a boundary nobody can read.
        """
        if not self.scopes:
            return True
        return granted is not None and granted in self.scopes
