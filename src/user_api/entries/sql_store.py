"""Fields and notes as rows, with the search index maintained by hand beside them.

Three things in here are worth reading before changing anything.

## The search index is a second copy, and nothing keeps it honest but these write paths

``entry_search`` is a **plain** FTS5 table, not an external-content one. External content
would avoid the duplication, at the price of having to issue ``'delete'`` commands
carrying the *old* values on every update -- a well-known footgun, because a single missed
one corrupts the index silently and the corruption only shows up as a search that stops
returning something it used to.

The trade taken here is the opposite: the content is duplicated, and every write path goes
through :func:`~user_api.entries.sql_rows._index` or
:func:`~user_api.entries.sql_rows._unindex`. That is a rule a person has to follow rather
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

:data:`~user_api.entries.sql_rows._VISIBLE` is the SQL half of
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
    new_entry_id,
)
from user_api.domain.errors import ScopeConflictError
from user_api.domain.search import to_match_query
from user_api.domain.values import derive_value_type
from user_api.entries.sql_rows import (
    _LIVE,
    _VISIBLE,
    ENTRY_COLUMNS,
    _check_entry_cap,
    _check_field_cap,
    _check_pin_cap,
    _entry_of,
    _field_text,
    _filter_clauses,
    _index,
    _insert,
    _merge,
    _paginate,
    _read_many_in,
    _read_one_in,
    _read_written,
    _replace_scopes,
    _require,
    _scopes_for,
    _seq_of,
    _unindex,
)
from user_api.entries.store import UNSET, Counts, FieldSummary, Journal, Page, _Unset
from user_api.storage.times import from_column, from_column_optional, to_column

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from user_api.entries.store import Filters
    from user_api.events.log import EventLog
    from user_api.storage.database import Database

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
            replaced = _require(_read_written(connection, existing["entry_id"]))
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

            revised = _require(_read_written(connection, entry_id))
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
            confirmed = _require(_read_written(connection, entry_id))
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
            forgotten = _require(_read_written(connection, entry_id))
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
                # _LIVE is not optional here and its absence was a real defect. Without it
                # a forgotten field came back from its own key for the whole grace period,
                # so "forget my blood type" left get_field returning it for thirty days --
                # and, because the unique index is partial, several forgotten rows can
                # share a key, making which one came back arbitrary as well.
                where=f"e.account_id = ? AND e.key = ? AND e.entry_type = 'field' AND {_LIVE}",
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
