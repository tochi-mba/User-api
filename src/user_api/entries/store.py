"""What an entry store promises. The adapter is :mod:`user_api.entries.sql_store`.

Every method takes an ``account_id``, and it is not optional on any of them. That is the
mechanism behind account isolation: there is no call that *could* read across accounts, so
isolation is not a check somebody has to remember to write in a handler. It is the same
trick keyring uses, and it is why this service has no ``/v1/users/{account_id}`` -- the
cross-account read is not forbidden, it is inexpressible.

Every read method also takes ``granted``, the single scope the caller's token carries, and
applies the same rule: an entry with no scopes is visible to everybody, an entry with
scopes is visible only to a token granting one of them. An invisible entry reads back as
absent -- identical to one that was never written -- for the same reason a cross-account
one does.

## Why the caps live here

A caller that counts and then writes has a window between the two. Two concurrent writes
both read "499 fields" and both proceed, and the account ends up with 501. Every cap in
this service is therefore passed *into* the write and enforced inside its transaction,
where there is no window. There is a test for each one under ``asyncio.gather``.

## Why the store writes the event log

Every write here appends to :class:`~user_api.events.log.EventLog` **inside its own
transaction**, which is why that port's append is synchronous and takes a live connection.
A service that wrote the entry and then logged it would have two transactions, and a crash
or a rollback between them leaves either a change nothing recorded or a record of a change
that did not happen. Neither is a log.

It is also why :class:`Journal` exists rather than two more loose parameters: how big the
log may get and whether it keeps values are the *account's* settings, and the service is
the layer that holds them.

## Why there is no ``save(entry)``

Writing a whole entry back means writing back everything a caller read some time ago.
Two requests revising two different parts of one entry each write back a record missing
the other's change, and the loser never finds out. So each write says what it changes:
:meth:`revise` takes the fields to change and leaves the rest, and bumps ``revision`` so a
reader can tell that something did.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from user_api.domain.cursors import Cursor, Ordering
    from user_api.domain.entries import Entry, EntryType, NoteKind, Sensitivity, Source


class _Unset(Enum):
    """The type of :data:`UNSET`.

    A single-member enum rather than ``object()`` because it is the only sentinel form a
    type checker narrows properly: ``value is not UNSET`` tells mypy the remaining type is
    the real one, where a bare object leaves it as ``object | Any`` and every caller needs
    a cast.
    """

    TOKEN = 0


UNSET = _Unset.TOKEN
"""Means "do not change this", as distinct from "change this to null".

``None`` cannot carry that meaning here: ``null`` is a legal field value, so a revision
that set ``value=None`` would be indistinguishable from one that left the value alone.
That ambiguity is exactly the kind that shows up as "it deleted my timezone" six months
later.
"""


@dataclass(frozen=True, slots=True)
class Journal:
    """How a write should be recorded, as the account has asked for it to be.

    ``log_values`` is the one that matters. The event log is a **second copy of the
    personal data** -- "changed diagnosis from X to Y" is the sensitive fact, not the
    metadata around it -- so it is off by default and events say what changed and who
    changed it, never to what. Turned on, the values are kept and are purged along with
    the entry they describe, so the erasure promise still holds; it is just doing more
    work to hold it.
    """

    cap: int
    log_values: bool = False


@dataclass(frozen=True, slots=True)
class Page:
    """One window of a walk, and how to ask for the next.

    ``next_cursor`` is ``None`` exactly when there is no next page, so a caller loops
    until it is rather than comparing counts to a limit -- which gets the last page wrong
    whenever it happens to be full.
    """

    entries: tuple[Entry, ...]
    next_cursor: Cursor | None


@dataclass(frozen=True, slots=True)
class FieldSummary:
    """One key, described but not valued. What ``describe_schema`` is made of.

    Deliberately carries no value. The endpoint exists to be cheap enough to call *before*
    inventing a key, and one that returned values would be a full export wearing a
    different name -- expensive enough that an assistant would stop calling it, which is
    the one thing that would make key sprawl worse.
    """

    key: str
    description: str
    value_type: str
    updated_at: datetime
    confirmed_at: datetime | None
    scopes: tuple[str, ...]
    pinned: bool


@dataclass(frozen=True, slots=True)
class Counts:
    """The always-load block's summary of what is here."""

    fields: int
    notes: int
    pinned: int
    forgotten: int


@dataclass(frozen=True, slots=True)
class Filters:
    """Everything ``GET /v1/user/entries`` can narrow by.

    One object rather than fourteen parameters threaded through three layers. Every field
    is optional and they combine; ``None`` means "do not narrow by this", which is
    distinct from a falsy value that does narrow -- ``pinned=False`` means "only the
    unpinned ones".
    """

    query: str | None = None
    entry_type: EntryType | None = None
    keys: tuple[str, ...] | None = None
    key_prefix: str | None = None
    note_kind: NoteKind | None = None
    source: Source | None = None
    asserted_by: str | None = None
    sensitivity: Sensitivity | None = None
    pinned: bool | None = None
    scope: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    stale_before: datetime | None = None
    include_forgotten: bool = False


@runtime_checkable
class EntryStore(Protocol):
    """Persistence for fields and notes, their scopes, and their search index."""

    # Every column a caller may set, plus the three caps this write is bounded by.
    async def put_field(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        key: str,
        value: object,
        description: str,
        source: Source,
        source_detail: str | None,
        asserted_by: str,
        scopes: tuple[str, ...],
        sensitivity: Sensitivity,
        pinned: bool,
        granted: str | None,
        now: datetime,
        entry_cap: int,
        field_cap: int,
        pin_cap: int,
        journal: Journal,
    ) -> Entry:
        """Create or replace the one live field with this key. Idempotent by key.

        ``PUT`` rather than ``POST`` all the way down: the key is the identity, so a model
        that retries after a timeout cannot produce two fields. Replacing keeps the
        ``created_at`` the field already had -- "known since" should not jump because a
        value was corrected -- and bumps ``revision``.

        ``confirmed_at`` moves according to whether the value actually changed, which is
        the one piece of behaviour here that is not obvious:

        * a **different** value clears it. The new value has never been vouched for, and
          carrying the old confirmation across would make a fact that changed this morning
          report as "confirmed last March".
        * the **same** value sets it to ``now``. Writing a value again *is* somebody
          saying it is still true, which is exactly what a confirmation is -- so an
          assistant that re-states what it was told keeps the staleness clock honest
          without anybody calling ``confirm``.

        Raises:
            ScopeConflictError: a live field with this key exists outside what ``granted``
                can see. Refused rather than overwritten: a ``user.home`` token must not
                be able to clobber a health-scoped value it cannot read. See
                :class:`~user_api.domain.errors.ScopeConflictError` for why this is not a
                404.
            LimitExceededError: the account is at ``entry_cap`` or ``field_cap``, or
                pinning this would take it past ``pin_cap``. Replacing an existing field
                is never refused for the entry or field caps, however full the account is.
        """
        ...

    async def write_note(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        body: str,
        note_kind: NoteKind,
        description: str,
        source: Source,
        source_detail: str | None,
        asserted_by: str,
        scopes: tuple[str, ...],
        sensitivity: Sensitivity,
        pinned: bool,
        now: datetime,
        entry_cap: int,
        pin_cap: int,
        journal: Journal,
    ) -> Entry:
        """Append a note. Always creates; notes have no natural key.

        Raises:
            LimitExceededError: the account is at ``entry_cap``, or pinning this would
                take it past ``pin_cap``.
        """
        ...

    async def get(self, account_id: str, entry_id: str, *, granted: str | None) -> Entry | None:
        """One entry, or ``None``.

        ``None`` covers all four of: never existed, belongs to another account, is
        forgotten, and is scoped away from this token. The caller must not be able to tell
        them apart, so neither can this.
        """
        ...

    async def get_field(self, account_id: str, key: str, *, granted: str | None) -> Entry | None:
        """The one live field with this key, or ``None``. Same indistinguishability."""
        ...

    # account, scope, filters, ordering, limit, cursor -- six, and every one of them is a
    # separate axis the caller chooses independently.
    async def search(  # noqa: PLR0913
        self,
        account_id: str,
        *,
        granted: str | None,
        filters: Filters,
        ordering: Ordering,
        limit: int,
        cursor: Cursor | None = None,
    ) -> Page:
        """The one flexible read. Every filter combines; every page is keyset-paginated.

        One endpoint rather than six narrow ones, because the caller is a model choosing
        tools: it has one thing to learn and cannot pick the wrong one.

        Raises:
            InvalidSearchError: ``filters.query`` contains nothing matchable.
            InvalidCursorError: the cursor was issued for a different ordering.
        """
        ...

    async def pinned(
        self, account_id: str, *, granted: str | None, limit: int
    ) -> tuple[Entry, ...]:
        """The always-load set, most recently touched first.

        Capped rather than unbounded because this is read at the start of every
        conversation and goes straight into a context window. Pinning is a token budget
        before it is a preference.
        """
        ...

    async def describe(self, account_id: str, *, granted: str | None) -> tuple[FieldSummary, ...]:
        """Every live field key with its description and type, and no values."""
        ...

    async def counts(self, account_id: str, *, granted: str | None) -> Counts:
        """How many of each kind this account has, within this token's scope."""
        ...

    async def revise(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        entry_id: str,
        granted: str | None,
        now: datetime,
        asserted_by: str,
        pin_cap: int,
        scope_cap_granted: str | None,
        journal: Journal,
        value: object | _Unset = UNSET,
        body: str | None = None,
        description: str | None = None,
        sensitivity: Sensitivity | None = None,
        pinned: bool | None = None,
        scopes: tuple[str, ...] | None = None,
        source: Source | None = None,
        source_detail: str | None = None,
    ) -> Entry:
        """Change some of one entry and leave the rest. Bumps ``revision``.

        ``confirmed_at`` is deliberately **not** touched. A revision is somebody changing
        what we hold; a confirmation is somebody saying it is still true. Conflating them
        would make every correction reset the staleness clock, and the whole point of
        tracking staleness is to find the facts nobody has vouched for lately.

        ``asserted_by`` moves to the reviser. It means "the token that vouches for what
        this entry says *now*", so leaving it on the original writer after somebody else
        changed the value would attribute the new content to whoever happened to write the
        old. The event records the same audience, which is what makes the log answer "who
        changed this" rather than "who first wrote it".

        Args:
            value: the new value for a field. Sentinel-defaulted rather than
                ``None``-defaulted because ``None`` is a legal field value: "unset it" and
                "leave it alone" are different requests and must be expressible as such.
            scope_cap_granted: what the token may widen scopes to. Passed separately from
                ``granted`` so the check reads as what it is -- a write rule, not a read
                rule -- at the one call site where they could ever differ.

        Raises:
            EntryNotFoundError: no such visible entry.
            LimitExceededError: pinning this would pass ``pin_cap``.
        """
        ...

    # account, entry, scope, clock, who is doing it, and how to record it.
    async def confirm(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        entry_id: str,
        granted: str | None,
        now: datetime,
        asserted_by: str,
        journal: Journal,
    ) -> Entry:
        """Record that a human said this is still true. Touches only ``confirmed_at``.

        The cheap half of the staleness story: ``?stale_before=`` finds what to ask about,
        and this is what the answer costs. Deliberately not a revision -- nothing changed,
        so ``asserted_by`` on the *entry* stays where it was and only the event records who
        did the confirming.

        Raises:
            EntryNotFoundError: no such visible entry.
        """
        ...

    # account, entry, scope, clock, who is doing it, and how to record it.
    async def forget(  # noqa: PLR0913
        self,
        *,
        account_id: str,
        entry_id: str,
        granted: str | None,
        now: datetime,
        asserted_by: str,
        journal: Journal,
    ) -> Entry:
        """Mark an entry forgotten. Invisible from every read path immediately.

        Marking only. Whether and when the bytes go is the account's erasure setting, and
        is :mod:`user_api.users.erasure`'s business.

        Raises:
            EntryNotFoundError: no such visible entry.
        """
        ...

    def purge_in(self, connection: sqlite3.Connection, *, account_id: str, entry_id: str) -> bool:
        """Destroy one entry, its scopes and its search row, inside the caller's transaction.

        Synchronous and connection-taking for the same reason the event log's writes are.
        Purging an entry and stripping the values out of the events *about* that entry are
        two halves of one promise, and between two transactions there is a moment where the
        entry is gone and its old value is still sitting in the log. The erasure path holds
        one transaction across both.

        Not scope-filtered, and not for callers: reached only through erasure, which has
        already established what it is purging. A sweeper filtered by a scope it does not
        hold would leave rows behind forever.

        **The bytes are not gone when this returns.** They are out of the b-tree and still
        in the write-ahead log. See
        :meth:`~user_api.storage.database.Database.checkpoint_truncate`, which the erasure
        path calls once per sweep rather than once per row.
        """
        ...

    async def accounts_with_forgotten(self) -> tuple[str, ...]:
        """Every account holding at least one forgotten entry.

        The sweeper's first step, and the reason the sweep is two calls rather than one
        clever query. How long a forgotten entry survives is a *per-account* setting, and
        whether it is ever destroyed at all is too -- so a single global "everything older
        than N" would purge a tombstone account's entries and would use one account's
        grace period on another's data.

        Splitting it also keeps this layer honest: ``entries`` sits below ``users`` and
        must not know what an erasure mode is. It reports who has forgotten something;
        the sweeper, which does hold the settings, decides what that means.
        """
        ...

    async def due_for_purge(
        self, account_id: str, *, before: datetime, limit: int
    ) -> tuple[str, ...]:
        """Entry ids forgotten before ``before``, for one account.

        Returns ids and no content, so the sweeper never holds anybody's data in memory --
        which also means a sweep that crashes mid-way leaves nothing behind in a traceback.
        """
        ...

    async def index_agrees(self, account_id: str) -> bool:
        """Whether the search index and the table hold the same entries.

        Exists for the test that would otherwise have to reach into the adapter's private
        SQL. The explicit ``_index()`` helper this checks is the thing that creates this
        bug class -- a plain FTS5 table is maintained by hand, so "the index and the table
        disagree" is a state the schema permits and only a test forbids.
        """
        ...
