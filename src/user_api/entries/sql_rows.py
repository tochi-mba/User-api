"""Connection-taking row I/O for the entry store.

These are module-level rather than methods on
:class:`~user_api.entries.sql_store.SqlEntryStore` because they take a live
``sqlite3.Connection`` and none of the store's instance state. The store opens the
transaction and decides *what* to change; these functions are the SQL and the hydration.
Keeping them off the class means a write path cannot reach for ``self`` for something that
has to run on the connection the caller already holds, and the same helpers serve both the
write methods and the read methods without dragging the event log or the database handle
along.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from user_api.domain.cursors import Cursor, Ordering
from user_api.domain.entries import (
    Entry,
    EntryType,
    NoteKind,
    Sensitivity,
    Source,
    ValueType,
)
from user_api.domain.errors import EntryNotFoundError, LimitExceededError
from user_api.domain.values import derive_value_type, searchable_text
from user_api.entries.store import Page, _Unset
from user_api.storage.times import from_column, from_column_optional, to_column, to_column_optional

if TYPE_CHECKING:
    import sqlite3

    from user_api.entries.store import Filters

ENTRY_COLUMNS = (
    "e.seq, e.entry_id, e.account_id, e.entry_type, e.key, e.value_json, e.value_type, "
    "e.body, e.note_kind, e.description, e.sensitivity, e.source, e.source_detail, "
    "e.asserted_by, e.pinned, e.revision, e.created_at, e.updated_at, e.confirmed_at, "
    "e.forgotten_at"
)
"""Every column a reader needs, and deliberately not ``search_text``.

``search_text`` is a denormalised copy of the content, so selecting it would double the
bytes every read moves for a column nothing above the store looks at.
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


def _read_written(connection: sqlite3.Connection, entry_id: str) -> Entry | None:
    """Read back an entry the caller has just written, without the visibility filter.

    Every write path has already established that this caller may act on this entry --
    ``put_field`` checked the key, and ``revise``, ``confirm`` and ``forget`` each read it
    through :data:`_VISIBLE` before touching it. Applying the filter a second time on the
    way out is not a second check; it is a different question, asked after the answer has
    changed.

    Two of them changed it. A write that narrows an entry's scopes past what the writer
    holds makes the row invisible *to the writer*, so the read-back returned nothing and
    the store raised "no such entry" for a write that had just succeeded. Forgetting did
    the same thing for a different reason -- the row it returns is by definition no longer
    live.

    You may always read what you just wrote. That is the rule, and it is simpler than the
    two special cases it replaces.
    """
    found = _read_many_in_unscoped(connection, where="e.entry_id = ?", parameters=(entry_id,))
    return found[0] if found else None


def _read_one_in(
    connection: sqlite3.Connection, entry_id: str, *, granted: str | None, account: str
) -> Entry | None:
    """One live entry by id, or ``None`` if it is absent, elsewhere, forgotten or out of scope.

    The account is required rather than optional, and there is no way to ask for a
    forgotten one. Both used to be parameters and both had exactly one value at every call
    site -- the coverage gate is what said so, by reporting the other arm of each branch as
    unreachable.

    Neither absence costs anything. Reading back a write goes through
    :func:`_read_written`, which deliberately applies no filter at all; a caller that wants
    forgotten entries goes through :meth:`SqlEntryStore.search` with
    ``include_forgotten``. What is left here is the one question every caller was actually
    asking: is there a live entry with this id that this token may see.
    """
    found = _read_many_in(
        connection,
        where=f"e.entry_id = ? AND e.account_id = ? AND {_LIVE}",
        parameters=(entry_id, account),
        granted=granted,
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


def _read_many_in_unscoped(
    connection: sqlite3.Connection, *, where: str, parameters: tuple[object, ...]
) -> list[Entry]:
    """The same read as :func:`_read_many_in`, with no visibility predicate.

    Reached only from :func:`_read_written`. Kept as its own function rather than a flag on
    the other, so that every call site which *does* filter says so by calling the one that
    filters -- a boolean argument spelled ``scoped=False`` at a call site is exactly how a
    scope check goes missing.
    """
    rows = connection.execute(
        f"SELECT {ENTRY_COLUMNS} FROM entries e WHERE {where}",  # noqa: S608
        parameters,
    ).fetchall()
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
