"""Fields and notes as rows, with the search index maintained by hand beside them.

Three things in here are worth reading before changing anything.

## The search index is a second copy, and nothing keeps it honest but this file

``entry_search`` is a **plain** FTS5 table, not an external-content one. External content
would avoid the duplication, at the price of having to issue ``'delete'`` commands
carrying the *old* values on every update -- a well-known footgun, because a single missed
one corrupts the index silently and the corruption only shows up as a search that stops
returning something it used to.

The trade taken here is the opposite: the content is duplicated, and every write path goes
through :func:`_index` or :func:`_unindex`. That is a rule a person has to follow rather
than one the database enforces, which is why ``index_agrees`` exists on the port and why
there is a test class asserting the table and the index still match after create, revise,
forget, purge and cascade. The explicit helper is what creates this bug class; the test
class is the price of the trade.

The index is keyed by ``entries.seq``, an integer surrogate that exists for no other
reason. FTS5 rowids are integers and ``entry_id`` is a hex string, so without the
surrogate every index delete would be a full scan of the index.

## A forgotten entry stays in the index

Deliberately. ``?include_forgotten=true`` is how somebody reviews what they asked to be
forgotten and undoes it if they were wrong, and that has to work with a search query in
it. Forgotten rows are excluded by the ``forgotten_at IS NULL`` predicate in the WHERE,
not by being absent from the index. The index row goes at *purge*, which is where
"forgotten" becomes "gone".

## Visibility is one predicate, and it is applied on writes as well as reads

:data:`_VISIBLE` is the SQL half of
:meth:`~user_api.domain.entries.Entry.visible_to`, and a test asserts the two agree. An
entry with no scope rows is visible to everybody; an entry with scope rows is visible only
to a token granting one of them. When the caller holds no scope at all the parameter binds
``NULL``, and ``scope = NULL`` is never true -- so the predicate collapses to
"unscoped only" without a second query shape.

It is applied to writes too, addressed by id, so a ``user.home`` token cannot revise,
confirm or forget a health-scoped entry it could not have read. An invisible entry raises
:class:`~user_api.domain.errors.EntryNotFoundError` -- identical to one that never
existed, because the caller must not be able to tell those apart.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from user_api.domain.cursors import Cursor, Ordering
from user_api.domain.entries import (
    Action,
    Entry,
    EntryType,
    NoteKind,
    Sensitivity,
    Source,
    ValueType,
    new_entry_id,
)
from user_api.domain.errors import EntryNotFoundError, LimitExceededError, ScopeConflictError
from user_api.domain.search import to_match_query
from user_api.domain.values import derive_value_type, searchable_text
from user_api.entries.store import UNSET, Counts, FieldSummary, Journal, Page, _Unset
from user_api.storage.times import from_column, from_column_optional, to_column, to_column_optional

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from user_api.entries.store import Filters
    from user_api.events.log import EventLog
    from user_api.storage.database import Database

ENTRY_COLUMNS = (
    "e.seq, e.entry_id, e.account_id, e.entry_type, e.key, e.value_json, e.value_type, "
    "e.body, e.note_kind, e.description, e.sensitivity, e.source, e.source_detail, "
    "e.asserted_by, e.pinned, e.revision, e.created_at, e.updated_at, e.confirmed_at, "
    "e.forgotten_at"
)
"""Every column a reader needs, and deliberately not ``search_text``.

``search_text`` is a denormalised copy of the content, so selecting it would double the
bytes every read moves for a column nothing above this file looks at.
"""

_INSERT_COLUMNS = (
    "entry_id, account_id, entry_type, key, value_json, value_type, body, note_kind, "
    "description, sensitivity, source, source_detail, asserted_by, pinned, revision, "
    "created_at, updated_at, confirmed_at, forgotten_at, search_text"
)

_VISIBLE = (
    "(NOT EXISTS (SELECT 1 FROM entry_scopes sc WHERE sc.entry_id = e.entry_id)"
    " OR EXISTS (SELECT 1 FROM entry_scopes sc WHERE sc.entry_id = e.entry_id AND sc.scope = ?))"
)
"""The scope boundary, in SQL. Binds exactly one parameter: the caller's granted scope."""

_LIVE = "e.forgotten_at IS NULL"

_ORDER_BY = {
    Ordering.RECENT: "e.updated_at DESC, e.entry_id DESC",
    Ordering.OLDEST: "e.created_at ASC, e.entry_id ASC",
}
_KEYSET = {
    # Row-value comparison, which SQLite has supported since 3.15. Written as one tuple
    # comparison rather than the equivalent OR-of-ANDs because the tuple form is what the
    # partial index can actually seek on -- the expanded form makes it a scan.
    Ordering.RECENT: "(e.updated_at, e.entry_id) < (?, ?)",
    Ordering.OLDEST: "(e.created_at, e.entry_id) > (?, ?)",
}


class SqlEntryStore:
    """Entries in one table, their scopes in another, their text in a third."""

    def __init__(self, *, database: Database, events: EventLog) -> None:
        self._db = database
        self._events = events

    # -- writes ------------------------------------------------------------------------

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
        value_json = json.dumps(value, ensure_ascii=False)
        text = _field_text(key=key, description=description, value=value)

        def write(connection: sqlite3.Connection) -> Entry:
            existing = connection.execute(
                "SELECT e.seq, e.entry_id, e.created_at, e.value_json, e.pinned,"  # noqa: S608
                f" {_VISIBLE} AS visible FROM entries e"
                " WHERE e.account_id = ? AND e.key = ? AND e.entry_type = 'field'"
                f" AND {_LIVE}",
                (granted, account_id, key),
            ).fetchone()

            if existing is None:
                _check_entry_cap(connection, account_id, cap=entry_cap)
                _check_field_cap(connection, account_id, cap=field_cap)
                if pinned:
                    _check_pin_cap(connection, account_id, cap=pin_cap, adding=1)
                created = _insert(
                    connection,
                    entry=Entry(
                        entry_id=new_entry_id(),
                        account_id=account_id,
                        entry_type=EntryType.FIELD,
                        key=key,
                        value=value,
                        value_type=derive_value_type(value),
                        description=description,
                        sensitivity=sensitivity,
                        source=source,
                        source_detail=source_detail,
                        asserted_by=asserted_by,
                        scopes=scopes,
                        pinned=pinned,
                        created_at=now,
                        updated_at=now,
                    ),
                    value_json=value_json,
                    text=text,
                )
                self._log(
                    connection,
                    account_id=account_id,
                    at=now,
                    action=Action.FIELD_SET,
                    asserted_by=asserted_by,
                    journal=journal,
                    entry=created,
                    detail={"value": value},
                )
                return created

            if not existing["visible"]:
                # The awkward one. This token cannot read the value it would be
                # overwriting, so overwriting it silently is out, and reporting success
                # for a write that did not happen is worse. What leaks is that a key is
                # taken -- not its value, not its scope. ADR-0004 argues the trade.
                msg = (
                    f"a field named {key!r} already exists outside what this token "
                    "can see; it was not changed"
                )
                raise ScopeConflictError(msg)

            if pinned and not existing["pinned"]:
                _check_pin_cap(connection, account_id, cap=pin_cap, adding=1)

            # A value that did not change is somebody saying it is still true, which is
            # what a confirmation is. A value that did change has never been vouched for,
            # so the old confirmation must not carry across -- otherwise a fact corrected
            # this morning reports as confirmed last March.
            confirmed_at = to_column(now) if existing["value_json"] == value_json else None

            connection.execute(
                "UPDATE entries SET value_json = ?, value_type = ?, description = ?,"
                " sensitivity = ?, source = ?, source_detail = ?, asserted_by = ?,"
                " pinned = ?, revision = revision + 1, updated_at = ?, confirmed_at = ?,"
                " search_text = ? WHERE entry_id = ?",
                (
                    value_json,
                    derive_value_type(value).value,
                    description,
                    sensitivity.value,
                    source.value,
                    source_detail,
                    asserted_by,
                    int(pinned),
                    to_column(now),
                    confirmed_at,
                    text,
                    existing["entry_id"],
                ),
            )
            _replace_scopes(connection, existing["entry_id"], scopes)
            _index(connection, seq=existing["seq"], text=text)
            replaced = _require(_read_one_in(connection, existing["entry_id"], granted=granted))
            self._log(
                connection,
                account_id=account_id,
                at=now,
                action=Action.FIELD_SET,
                asserted_by=asserted_by,
                journal=journal,
                entry=replaced,
                detail={
                    "value": value,
                    "old_value": json.loads(existing["value_json"]),
                },
            )
            return replaced

        return await self._db.transact(write)

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
        text = f"{description} {body}"

        def write(connection: sqlite3.Connection) -> Entry:
            _check_entry_cap(connection, account_id, cap=entry_cap)
            if pinned:
                _check_pin_cap(connection, account_id, cap=pin_cap, adding=1)
            written = _insert(
                connection,
                entry=Entry(
                    entry_id=new_entry_id(),
                    account_id=account_id,
                    entry_type=EntryType.NOTE,
                    body=body,
                    note_kind=note_kind,
                    description=description,
                    sensitivity=sensitivity,
                    source=source,
                    source_detail=source_detail,
                    asserted_by=asserted_by,
                    scopes=scopes,
                    pinned=pinned,
                    created_at=now,
                    updated_at=now,
                ),
                value_json=None,
                text=text,
            )
            self._log(
                connection,
                account_id=account_id,
                at=now,
                action=Action.NOTE_WRITTEN,
                asserted_by=asserted_by,
                journal=journal,
                entry=written,
                detail={"body": body},
            )
            return written

        return await self._db.transact(write)

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
        def write(connection: sqlite3.Connection) -> Entry:
            current = _require(
                _read_one_in(connection, entry_id, granted=granted, account=account_id)
            )

            if pinned and not current.pinned:
                _check_pin_cap(connection, account_id, cap=pin_cap, adding=1)

            merged = _merge(
                current,
                value=value,
                body=body,
                description=description,
                sensitivity=sensitivity,
                pinned=pinned,
                source=source,
                source_detail=source_detail,
            )
            text = (
                _field_text(
                    key=merged.key or "",
                    description=merged.description,
                    value=merged.value,
                )
                if merged.entry_type is EntryType.FIELD
                else f"{merged.description} {merged.body}"
            )
            value_json = (
                json.dumps(merged.value, ensure_ascii=False)
                if merged.entry_type is EntryType.FIELD
                else None
            )

            connection.execute(
                "UPDATE entries SET value_json = ?, value_type = ?, body = ?, description = ?,"
                " sensitivity = ?, source = ?, source_detail = ?, asserted_by = ?, pinned = ?,"
                " revision = revision + 1, updated_at = ?, search_text = ?"
                " WHERE entry_id = ?",
                (
                    value_json,
                    merged.value_type.value if merged.value_type is not None else None,
                    merged.body,
                    merged.description,
                    merged.sensitivity.value,
                    merged.source.value,
                    merged.source_detail,
                    # Moves to the reviser: asserted_by means "the token that vouches for
                    # what this says NOW", and leaving it on the original writer would
                    # attribute a value somebody else changed to whoever wrote the old one.
                    asserted_by,
                    int(merged.pinned),
                    to_column(now),
                    text,
                    entry_id,
                ),
            )
            if scopes is not None:
                _replace_scopes(connection, entry_id, scopes)
            _index(connection, seq=_seq_of(connection, entry_id), text=text)

            # Re-read with the scope the write was permitted under rather than the one the
            # read used. They are the same everywhere today; passing it explicitly is what
            # keeps a future "widen this entry's scopes" from returning None and looking
            # like the entry vanished mid-request.
            revised = _require(_read_one_in(connection, entry_id, granted=scope_cap_granted))
            self._log(
                connection,
                account_id=account_id,
                at=now,
                action=Action.ENTRY_REVISED,
                asserted_by=asserted_by,
                journal=journal,
                entry=revised,
                detail={"value": revised.value, "old_value": current.value}
                if revised.entry_type is EntryType.FIELD
                else {"body": revised.body, "old_body": current.body},
            )
            return revised

        return await self._db.transact(write)

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
        def write(connection: sqlite3.Connection) -> Entry:
            _require(_read_one_in(connection, entry_id, granted=granted, account=account_id))
            # Only confirmed_at. Not updated_at, and not revision: nothing changed, and an
            # entry whose updated_at moved every time somebody said "yes, still true"
            # would sort to the top of a recency listing for not changing.
            connection.execute(
                "UPDATE entries SET confirmed_at = ? WHERE entry_id = ?",
                (to_column(now), entry_id),
            )
            confirmed = _require(_read_one_in(connection, entry_id, granted=granted))
            self._log(
                connection,
                account_id=account_id,
                at=now,
                action=Action.ENTRY_CONFIRMED,
                asserted_by=asserted_by,
                journal=journal,
                entry=confirmed,
                detail=None,
            )
            return confirmed

        return await self._db.transact(write)

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
        def write(connection: sqlite3.Connection) -> Entry:
            _require(_read_one_in(connection, entry_id, granted=granted, account=account_id))
            connection.execute(
                "UPDATE entries SET forgotten_at = ? WHERE entry_id = ?",
                (to_column(now), entry_id),
            )
            # The search row stays. ?include_forgotten=true is how somebody reviews what
            # they asked to be forgotten, and that has to keep working with a query in it.
            # The index row goes at purge, which is where forgotten becomes gone.
            forgotten = _require(
                _read_one_in(connection, entry_id, granted=granted, include_forgotten=True)
            )
            # Logged without values whatever the account's setting says. The record that
            # something was forgotten is the one event that must survive the purge of what
            # it describes, and an event carrying the forgotten value would be the thing
            # the purge then has to come back for.
            self._log(
                connection,
                account_id=account_id,
                at=now,
                action=Action.ENTRY_FORGOTTEN,
                asserted_by=asserted_by,
                journal=journal,
                entry=forgotten,
                detail=None,
            )
            return forgotten

        return await self._db.transact(write)

    def purge_in(self, connection: sqlite3.Connection, *, account_id: str, entry_id: str) -> bool:
        row = connection.execute(
            "SELECT seq FROM entries WHERE entry_id = ? AND account_id = ?",
            (entry_id, account_id),
        ).fetchone()
        if row is None:
            return False

        _unindex(connection, seq=row["seq"])
        # entry_scopes goes by ON DELETE CASCADE. That is a foreign key, and the connection
        # reads `PRAGMA foreign_keys` back and refuses to open if it did not take -- because
        # a silently-decorative cascade here is an erasure that reports success and leaves
        # the scope rows behind.
        connection.execute("DELETE FROM entries WHERE entry_id = ?", (entry_id,))
        return True

    # -- reads -------------------------------------------------------------------------

    async def get(self, account_id: str, entry_id: str, *, granted: str | None) -> Entry | None:
        return await self._db.run(
            lambda connection: _read_one_in(
                connection, entry_id, granted=granted, account=account_id
            )
        )

    async def get_field(self, account_id: str, key: str, *, granted: str | None) -> Entry | None:
        def read(connection: sqlite3.Connection) -> Entry | None:
            found = _read_many_in(
                connection,
                where="e.account_id = ? AND e.key = ? AND e.entry_type = 'field'",
                parameters=(account_id, key),
                granted=granted,
            )
            return found[0] if found else None

        return await self._db.run(read)

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
        if filters.query is not None:
            return await self._search_ranked(
                account_id, granted=granted, filters=filters, limit=limit, cursor=cursor
            )
        return await self._search_ordered(
            account_id,
            granted=granted,
            filters=filters,
            ordering=ordering,
            limit=limit,
            cursor=cursor,
        )

    async def pinned(
        self, account_id: str, *, granted: str | None, limit: int
    ) -> tuple[Entry, ...]:
        def read(connection: sqlite3.Connection) -> tuple[Entry, ...]:
            return tuple(
                _read_many_in(
                    connection,
                    where=f"e.account_id = ? AND e.pinned = 1 AND {_LIVE}",
                    parameters=(account_id,),
                    granted=granted,
                    order_by=_ORDER_BY[Ordering.RECENT],
                    limit=limit,
                )
            )

        return await self._db.run(read)

    async def describe(self, account_id: str, *, granted: str | None) -> tuple[FieldSummary, ...]:
        def read(connection: sqlite3.Connection) -> tuple[FieldSummary, ...]:
            rows = connection.execute(
                "SELECT e.entry_id, e.key, e.description, e.value_type,"  # noqa: S608
                " e.updated_at, e.confirmed_at, e.pinned FROM entries e"
                f" WHERE e.account_id = ? AND e.entry_type = 'field' AND {_LIVE}"
                f" AND {_VISIBLE} ORDER BY e.key",
                (account_id, granted),
            ).fetchall()
            scopes = _scopes_for(connection, [row["entry_id"] for row in rows])
            return tuple(
                FieldSummary(
                    key=row["key"],
                    description=row["description"],
                    value_type=row["value_type"],
                    updated_at=from_column(row["updated_at"]),
                    confirmed_at=from_column_optional(row["confirmed_at"]),
                    scopes=scopes.get(row["entry_id"], ()),
                    pinned=bool(row["pinned"]),
                )
                for row in rows
            )

        return await self._db.run(read)

    async def counts(self, account_id: str, *, granted: str | None) -> Counts:
        def read(connection: sqlite3.Connection) -> Counts:
            row = connection.execute(
                "SELECT"  # noqa: S608
                "  sum(e.entry_type = 'field' AND e.forgotten_at IS NULL) AS fields,"
                "  sum(e.entry_type = 'note' AND e.forgotten_at IS NULL) AS notes,"
                "  sum(e.pinned = 1 AND e.forgotten_at IS NULL) AS pinned,"
                "  sum(e.forgotten_at IS NOT NULL) AS forgotten"
                f" FROM entries e WHERE e.account_id = ? AND {_VISIBLE}",
                (account_id, granted),
            ).fetchone()
            # sum() over no rows is NULL rather than 0, and a fresh account has no rows.
            return Counts(
                fields=row["fields"] or 0,
                notes=row["notes"] or 0,
                pinned=row["pinned"] or 0,
                forgotten=row["forgotten"] or 0,
            )

        return await self._db.run(read)

    # -- erasure support ---------------------------------------------------------------

    async def accounts_with_forgotten(self) -> tuple[str, ...]:
        rows = await self._db.fetch_all(
            "SELECT DISTINCT account_id FROM entries WHERE forgotten_at IS NOT NULL"
        )
        return tuple(row["account_id"] for row in rows)

    async def due_for_purge(
        self, account_id: str, *, before: datetime, limit: int
    ) -> tuple[str, ...]:
        rows = await self._db.fetch_all(
            "SELECT entry_id FROM entries"
            " WHERE account_id = ? AND forgotten_at IS NOT NULL AND forgotten_at < ?"
            " ORDER BY forgotten_at LIMIT ?",
            (account_id, to_column(before), limit),
        )
        return tuple(row["entry_id"] for row in rows)

    async def index_agrees(self, account_id: str) -> bool:
        def check(connection: sqlite3.Connection) -> bool:
            missing = connection.execute(
                "SELECT count(*) AS total FROM entries e"
                " LEFT JOIN entry_search f ON f.rowid = e.seq"
                " WHERE e.account_id = ? AND f.rowid IS NULL",
                (account_id,),
            ).fetchone()["total"]
            orphaned = connection.execute(
                "SELECT count(*) AS total FROM entry_search f"
                " LEFT JOIN entries e ON e.seq = f.rowid WHERE e.seq IS NULL"
            ).fetchone()["total"]
            stale = connection.execute(
                "SELECT count(*) AS total FROM entries e"
                " JOIN entry_search f ON f.rowid = e.seq"
                " WHERE e.account_id = ? AND f.search_text IS NOT e.search_text",
                (account_id,),
            ).fetchone()["total"]
            return not (missing or orphaned or stale)

        return await self._db.run(check)

    # -- the two search shapes ---------------------------------------------------------

    async def _search_ordered(  # noqa: PLR0913
        self,
        account_id: str,
        *,
        granted: str | None,
        filters: Filters,
        ordering: Ordering,
        limit: int,
        cursor: Cursor | None,
    ) -> Page:
        clauses, parameters = _filter_clauses(account_id, filters)
        if cursor is not None:
            clauses.append(_KEYSET[ordering])
            parameters.extend([cursor.sort_key, cursor.entry_id])

        def read(connection: sqlite3.Connection) -> Page:
            found = _read_many_in(
                connection,
                where=" AND ".join(clauses),
                parameters=tuple(parameters),
                granted=granted,
                order_by=_ORDER_BY[ordering],
                # One more than asked for, so "is there a next page" is an observation
                # rather than a guess from whether this page happened to come out full.
                limit=limit + 1,
            )
            return _paginate(found, limit=limit, ordering=ordering)

        return await self._db.run(read)

    async def _search_ranked(
        self,
        account_id: str,
        *,
        granted: str | None,
        filters: Filters,
        limit: int,
        cursor: Cursor | None,
    ) -> Page:
        # Raised before any SQL runs. FTS5's MATCH is a query language and most
        # punctuation is a syntax error in it, so this is what keeps a hostile search
        # string a 422 instead of a 500 from inside SQLite.
        match = to_match_query(filters.query or "")
        clauses, parameters = _filter_clauses(account_id, filters)

        def read(connection: sqlite3.Connection) -> Page:
            # Every fragment interpolated below is a constant defined in this module or
            # assembled by _filter_clauses from constants; every caller-supplied value is
            # a bound parameter. Nothing a caller sent becomes SQL text.
            inner = (
                f"SELECT {ENTRY_COLUMNS}, bm25(entry_search) AS rank FROM entries e"  # noqa: S608
                " JOIN entry_search ON entry_search.rowid = e.seq"
                " WHERE entry_search MATCH ?"
                # Account isolation lives HERE, in the outer WHERE, because the FTS index
                # is shared across every account -- the MATCH alone finds other people's
                # rows. There is a test named after exactly that.
                f" AND {' AND '.join(clauses)} AND {_VISIBLE}"
            )
            keyset = "WHERE (rank, entry_id) > (?, ?) " if cursor is not None else ""
            tail = [cursor.sort_key, cursor.entry_id] if cursor is not None else []
            rows = connection.execute(
                f"SELECT * FROM ({inner}) {keyset}"  # noqa: S608
                "ORDER BY rank ASC, entry_id ASC LIMIT ?",
                (match, *parameters, granted, *tail, limit + 1),
            ).fetchall()
            scopes = _scopes_for(connection, [row["entry_id"] for row in rows])
            found = [_entry_of(row, scopes=scopes.get(row["entry_id"], ())) for row in rows]
            return _paginate(found, limit=limit, ordering=Ordering.RELEVANCE)

        return await self._db.run(read)

    def _log(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        at: datetime,
        action: Action,
        asserted_by: str,
        journal: Journal,
        entry: Entry,
        detail: dict[str, object] | None,
    ) -> None:
        """Record what just happened, in the transaction that did it.

        ``detail`` is dropped unless the account asked for values to be kept. The caller
        assembles it either way, which looks wasteful and is not: building it at the call
        site is what keeps the decision to *withhold* it in one place instead of in six.
        """
        self._events.append_in(
            connection,
            account_id=account_id,
            at=at,
            action=action,
            asserted_by=asserted_by,
            cap=journal.cap,
            entry_id=entry.entry_id,
            entry_type=entry.entry_type,
            key=entry.key,
            source=entry.source.value,
            detail=detail if journal.log_values else None,
        )


# -- module helpers --------------------------------------------------------------------


def _index(connection: sqlite3.Connection, *, seq: int, text: str) -> None:
    """Write one entry's text into the search index, replacing whatever was there.

    The single place the index is written. ``INSERT OR REPLACE`` on the rowid rather than
    delete-then-insert, so a revision cannot briefly leave the entry unsearchable and
    cannot leave two rows behind if the delete is ever moved.
    """
    connection.execute(
        "INSERT OR REPLACE INTO entry_search (rowid, search_text) VALUES (?, ?)", (seq, text)
    )


def _unindex(connection: sqlite3.Connection, *, seq: int) -> None:
    """Remove one entry's text from the search index. The single place it is deleted."""
    connection.execute("DELETE FROM entry_search WHERE rowid = ?", (seq,))


def _insert(
    connection: sqlite3.Connection, *, entry: Entry, value_json: str | None, text: str
) -> Entry:
    """Write a new entry, its scopes and its index row as one unit."""
    cursor = connection.execute(
        f"INSERT INTO entries ({_INSERT_COLUMNS})"  # noqa: S608
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry.entry_id,
            entry.account_id,
            entry.entry_type.value,
            entry.key,
            value_json,
            entry.value_type.value if entry.value_type is not None else None,
            entry.body,
            entry.note_kind.value if entry.note_kind is not None else None,
            entry.description,
            entry.sensitivity.value,
            entry.source.value,
            entry.source_detail,
            entry.asserted_by,
            int(entry.pinned),
            entry.revision,
            to_column(entry.created_at),
            to_column(entry.updated_at),
            to_column_optional(entry.confirmed_at),
            to_column_optional(entry.forgotten_at),
            text,
        ),
    )
    _replace_scopes(connection, entry.entry_id, entry.scopes)
    # lastrowid is the `seq` surrogate the index is keyed by. Read from the cursor rather
    # than re-selected: a second query would be a second chance for them to disagree.
    _index(connection, seq=_require_rowid(cursor.lastrowid), text=text)
    return entry


def _replace_scopes(connection: sqlite3.Connection, entry_id: str, scopes: tuple[str, ...]) -> None:
    """Set an entry's scopes to exactly these, in the caller's transaction."""
    connection.execute("DELETE FROM entry_scopes WHERE entry_id = ?", (entry_id,))
    connection.executemany(
        "INSERT INTO entry_scopes (entry_id, scope) VALUES (?, ?)",
        [(entry_id, scope) for scope in sorted(set(scopes))],
    )


def _require_rowid(rowid: int | None) -> int:
    """Narrow the driver's optional rowid.

    ``lastrowid`` is typed optional because it is meaningless after a statement that is
    not an INSERT. This is called immediately after one, so the ``None`` is a type-level
    possibility rather than a runtime one -- but asserting it would be a branch no test
    can reach, so it raises a storage-level error a test *can* provoke by calling it
    directly.
    """
    if rowid is None:
        msg = "the driver reported no rowid for an insert that succeeded"
        raise RuntimeError(msg)
    return rowid


def _require(entry: Entry | None) -> Entry:
    """Turn an invisible or absent entry into the one error both must produce."""
    if entry is None:
        msg = "no such entry"
        raise EntryNotFoundError(msg)
    return entry


def _seq_of(connection: sqlite3.Connection, entry_id: str) -> int:
    """The surrogate the search index is keyed by, for an entry already known to exist."""
    return int(
        connection.execute("SELECT seq FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()[
            "seq"
        ]
    )


def _field_text(*, key: str, description: str, value: object) -> str:
    """What a field contributes to the search index.

    The key and the description are indexed alongside the value, which is what makes
    ``?q=allergies`` find a field whose value is "penicillin" -- the word somebody
    searches for is at least as often in the name of the thing as in the thing.
    """
    return f"{key.replace('_', ' ')} {description} {searchable_text(value)}".strip()


def _check_entry_cap(connection: sqlite3.Connection, account_id: str, *, cap: int) -> None:
    held = connection.execute(
        "SELECT count(*) AS total FROM entries WHERE account_id = ? AND forgotten_at IS NULL",
        (account_id,),
    ).fetchone()["total"]
    if held >= cap:
        msg = f"at most {cap} entries per account"
        raise LimitExceededError(msg)


def _check_field_cap(connection: sqlite3.Connection, account_id: str, *, cap: int) -> None:
    held = connection.execute(
        "SELECT count(*) AS total FROM entries"
        " WHERE account_id = ? AND entry_type = 'field' AND forgotten_at IS NULL",
        (account_id,),
    ).fetchone()["total"]
    if held >= cap:
        msg = f"at most {cap} fields per account"
        raise LimitExceededError(msg)


def _check_pin_cap(
    connection: sqlite3.Connection, account_id: str, *, cap: int, adding: int
) -> None:
    held = connection.execute(
        "SELECT count(*) AS total FROM entries"
        " WHERE account_id = ? AND pinned = 1 AND forgotten_at IS NULL",
        (account_id,),
    ).fetchone()["total"]
    if held + adding > cap:
        msg = f"at most {cap} pinned entries; unpin something first"
        raise LimitExceededError(msg)


def _merge(  # noqa: PLR0913
    current: Entry,
    *,
    value: object | _Unset,
    body: str | None,
    description: str | None,
    sensitivity: Sensitivity | None,
    pinned: bool | None,
    source: Source | None,
    source_detail: str | None,
) -> Entry:
    """Apply a partial revision to an entry, leaving everything unmentioned alone.

    ``value`` uses the UNSET sentinel rather than ``None`` because ``null`` is a legal
    field value: "unset it" and "leave it alone" are different requests and both have to
    be expressible.
    """
    new_value = current.value if isinstance(value, _Unset) else value
    return Entry(
        entry_id=current.entry_id,
        account_id=current.account_id,
        entry_type=current.entry_type,
        key=current.key,
        value=new_value,
        value_type=(
            derive_value_type(new_value) if current.entry_type is EntryType.FIELD else None
        ),
        body=current.body if body is None else body,
        note_kind=current.note_kind,
        description=current.description if description is None else description,
        sensitivity=current.sensitivity if sensitivity is None else sensitivity,
        source=current.source if source is None else source,
        source_detail=current.source_detail if source_detail is None else source_detail,
        asserted_by=current.asserted_by,
        scopes=current.scopes,
        pinned=current.pinned if pinned is None else pinned,
        revision=current.revision,
        created_at=current.created_at,
        updated_at=current.updated_at,
        confirmed_at=current.confirmed_at,
        forgotten_at=current.forgotten_at,
    )


def _filter_clauses(account_id: str, filters: Filters) -> tuple[list[str], list[object]]:
    """Build the WHERE fragments and their bound parameters.

    Every fragment is a literal defined here and every value is a bound parameter, which
    is what makes the f-string that assembles them safe -- nothing a caller sent ever
    becomes SQL text.
    """
    clauses = ["e.account_id = ?"]
    parameters: list[object] = [account_id]

    # The plain column equalities, which are all the same shape and are not worth
    # thirteen near-identical `if` blocks.
    for column, value in (
        ("e.entry_type", filters.entry_type.value if filters.entry_type else None),
        ("e.note_kind", filters.note_kind.value if filters.note_kind else None),
        ("e.source", filters.source.value if filters.source else None),
        ("e.asserted_by", filters.asserted_by),
        ("e.sensitivity", filters.sensitivity.value if filters.sensitivity else None),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            parameters.append(value)

    if not filters.include_forgotten:
        clauses.append(_LIVE)
    if filters.keys is not None:
        placeholders = ", ".join("?" for _ in filters.keys)
        clauses.append(f"e.key IN ({placeholders})")
        parameters.extend(filters.keys)
    if filters.key_prefix is not None:
        # LIKE with an escaped prefix rather than glob: the caller's prefix may contain
        # % or _, and unescaped either one turns "contact_" into a wildcard that matches
        # every key with any character where the underscore was.
        clauses.append("e.key LIKE ? ESCAPE '\\'")
        parameters.append(_like_prefix(filters.key_prefix))
    if filters.pinned is not None:
        clauses.append("e.pinned = ?")
        parameters.append(int(filters.pinned))
    if filters.scope is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM entry_scopes sf WHERE sf.entry_id = e.entry_id AND sf.scope = ?)"
        )
        parameters.append(filters.scope)
    if filters.since is not None:
        clauses.append("e.updated_at >= ?")
        parameters.append(to_column(filters.since))
    if filters.until is not None:
        clauses.append("e.updated_at < ?")
        parameters.append(to_column(filters.until))
    if filters.stale_before is not None:
        # A never-confirmed entry is the stalest thing there is, so NULL counts as stale
        # rather than being silently excluded by the comparison -- which is what a plain
        # `confirmed_at < ?` would do, and would hide exactly the entries this filter
        # exists to surface.
        clauses.append("(e.confirmed_at IS NULL OR e.confirmed_at < ?)")
        parameters.append(to_column(filters.stale_before))

    return clauses, parameters


def _like_prefix(prefix: str) -> str:
    """Escape a caller's prefix for LIKE, then anchor it."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _paginate(found: list[Entry], *, limit: int, ordering: Ordering) -> Page:
    """Trim the extra row and turn it into a cursor, or report the end of the walk."""
    if len(found) <= limit:
        return Page(entries=tuple(found), next_cursor=None)

    window = found[:limit]
    last = window[-1]
    return Page(
        entries=tuple(window),
        next_cursor=Cursor(
            ordering=ordering,
            sort_key=_sort_key(last, ordering),
            entry_id=last.entry_id,
        ),
    )


def _sort_key(entry: Entry, ordering: Ordering) -> str | float:
    """The value the next page resumes strictly after."""
    if ordering is Ordering.RELEVANCE:
        # Present on every row that came out of a ranked query, which is the only place
        # this ordering is reachable from.
        return float(entry.rank or 0.0)
    if ordering is Ordering.OLDEST:
        return to_column(entry.created_at)
    return to_column(entry.updated_at)


def _read_one_in(
    connection: sqlite3.Connection,
    entry_id: str,
    *,
    granted: str | None,
    account: str | None = None,
    include_forgotten: bool = False,
) -> Entry | None:
    """One entry by id, or ``None`` if it is absent, elsewhere, forgotten or out of scope."""
    clauses = ["e.entry_id = ?"]
    parameters: list[object] = [entry_id]
    if account is not None:
        clauses.append("e.account_id = ?")
        parameters.append(account)
    if not include_forgotten:
        clauses.append(_LIVE)

    found = _read_many_in(
        connection, where=" AND ".join(clauses), parameters=tuple(parameters), granted=granted
    )
    return found[0] if found else None


# connection, where, parameters, scope, order and limit -- six, and the last three are
# each optional and independent.
def _read_many_in(  # noqa: PLR0913
    connection: sqlite3.Connection,
    *,
    where: str,
    parameters: tuple[object, ...],
    granted: str | None,
    order_by: str | None = None,
    limit: int | None = None,
) -> list[Entry]:
    """Run a read with the visibility predicate appended, and hydrate the rows."""
    sql = f"SELECT {ENTRY_COLUMNS} FROM entries e WHERE {where} AND {_VISIBLE}"  # noqa: S608
    bound: list[object] = [*parameters, granted]
    if order_by is not None:
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += " LIMIT ?"
        bound.append(limit)

    rows = connection.execute(sql, bound).fetchall()
    scopes = _scopes_for(connection, [row["entry_id"] for row in rows])
    return [_entry_of(row, scopes=scopes.get(row["entry_id"], ())) for row in rows]


def _scopes_for(connection: sqlite3.Connection, entry_ids: list[str]) -> dict[str, tuple[str, ...]]:
    """Every scope for a page of entries, in one query rather than one per row."""
    if not entry_ids:
        return {}

    placeholders = ", ".join("?" for _ in entry_ids)
    rows = connection.execute(
        f"SELECT entry_id, scope FROM entry_scopes WHERE entry_id IN ({placeholders})"  # noqa: S608
        " ORDER BY entry_id, scope",
        entry_ids,
    ).fetchall()

    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["entry_id"], []).append(row["scope"])
    return {entry_id: tuple(scopes) for entry_id, scopes in grouped.items()}


def _rank_of(row: sqlite3.Row) -> float | None:
    """The bm25 score, on the rows that have one.

    Only a ranked query selects it, so this asks the row rather than the caller -- which
    keeps one hydration function serving both search shapes instead of two that drift.
    """
    columns = row.keys()
    return float(row["rank"]) if "rank" in columns else None


def _entry_of(row: sqlite3.Row, *, scopes: tuple[str, ...]) -> Entry:
    """Rebuild an entry from a row. The one place column names become attributes."""
    entry_type = EntryType(row["entry_type"])
    return Entry(
        entry_id=row["entry_id"],
        account_id=row["account_id"],
        entry_type=entry_type,
        key=row["key"],
        value=json.loads(row["value_json"]) if row["value_json"] is not None else None,
        value_type=ValueType(row["value_type"]) if row["value_type"] is not None else None,
        body=row["body"],
        note_kind=NoteKind(row["note_kind"]) if row["note_kind"] is not None else None,
        description=row["description"],
        sensitivity=Sensitivity(row["sensitivity"]),
        source=Source(row["source"]),
        source_detail=row["source_detail"],
        asserted_by=row["asserted_by"],
        scopes=scopes,
        pinned=bool(row["pinned"]),
        revision=row["revision"],
        created_at=from_column(row["created_at"]),
        updated_at=from_column(row["updated_at"]),
        confirmed_at=from_column_optional(row["confirmed_at"]),
        forgotten_at=from_column_optional(row["forgotten_at"]),
        rank=_rank_of(row),
    )
