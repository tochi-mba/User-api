# ADR-0011: one entries table for fields and notes

**Status:** accepted.

## Context

This service stores two things that look like two things. A **field** is a fact with a
name -- `preferred_name`, `timezone`, `blood_type` -- and there is one live one per key
per account. A **note** is something that happened, was noticed, or was learned, and there
are as many as the assistant writes. Different shapes, different write paths, different
uniqueness rules, and the obvious schema is two tables.

`domain/entries.py` says why there is one, and the count is the argument:

> They share scopes, provenance, pinning, forgetting, confirmation, revision and search.
> Twelve of their fifteen columns are the same, which is the argument for one table
> (ADR-0011) and for one dataclass here.

Twelve is literal. `description`, `sensitivity`, `source`, `source_detail`, `asserted_by`,
`pinned`, `revision`, `created_at`, `updated_at`, `confirmed_at`, `forgotten_at` and
`search_text` are shared, on top of the four that carry identity (`seq`, `entry_id`,
`account_id`, `entry_type`). What differs is three columns for a field (`key`,
`value_json`, `value_type`) and two for a note (`body`, `note_kind`).

## Decision

One `entries` table with an `entry_type` discriminator, one `Entry` dataclass, one FTS5
index, and one set of read paths. Every behaviour in the list above is written once.

## Why not two tables

Two tables duplicate all twelve columns and every rule that runs on them: the scope
predicate, the forgotten filter, the confirmation semantics, the revision bump, the
pinning cap, the event log's shape. Each of those is a place where the field version and
the note version could drift, and drift in a visibility predicate is the kind of bug that
is invisible until it is a disclosure.

The part that does not merely duplicate is search, and it is what settles the question.

**Two tables need two FTS indexes.** `entry_search` is a plain FTS5 table maintained by
hand from every write path (`entries/sql_store.py` explains why plain rather than
external-content), and there would be two of them, each with its own maintenance and its
own way of silently falling out of step with its content.

**Two indexes cannot be ranked together.** `bm25()` is a score computed from the
statistics of the index it is asked about: how often a term occurs in this corpus, how
long the average document in it is. A score from a corpus of short field values and a
score from a corpus of paragraph-length notes are two numbers on two scales. Merging them
into one ranked page means either interleaving numbers that do not mean the same thing, or
picking an arbitrary quota per table and calling it relevance. Neither is a thing bm25
lets you do meaningfully. With one corpus, "search my record" is one `MATCH`, one `ORDER
BY rank`, and one cursor -- which is what `_search_ranked` is.

## The CHECK that keeps the discriminator honest

The pairing is enforced by the database, not documented in a comment:

    CHECK (
        (entry_type = 'field'
            AND key IS NOT NULL AND value_json IS NOT NULL AND value_type IS NOT NULL
            AND body IS NULL AND note_kind IS NULL)
        OR
        (entry_type = 'note'
            AND body IS NOT NULL AND note_kind IS NOT NULL
            AND key IS NULL AND value_json IS NULL AND value_type IS NULL)
    )

Both halves matter, and the second half is the one that would get left out. Requiring a
field to have a key is obvious; requiring it to have *no body* is what stops a row being
both. Without the constraint the table would happily hold a "field" with a note body and
no key, and then every reader -- the row mapper, the response schema, the export, the
search result -- would have to defend against a shape that should not exist. The schema
comment puts it in one line: "enforced rather than documented".

It is also what lets `Entry` in `domain/entries.py` carry both sets of attributes with
`None` defaults and let the type checker keep quiet. The dataclass comment leans on the
constraint explicitly: "Non-null exactly when entry_type is FIELD; the database CHECK
enforces the pairing, so a reader does not have to defend against half a field."

The discriminator earns its keep elsewhere too. `idx_entries_field_key` is a partial
unique index on `(account_id, key)` where `entry_type = 'field' AND forgotten_at IS NULL`,
which is how one table enforces "one live field per key" without saying anything about
notes, and how a forgotten field stops blocking its own key.

## The surrogate `seq` column

`seq INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT` exists for one reason, and it is not
identity. FTS5 rowids are integers; `entry_id` is a hex string. Without an integer to key
the index by, every index delete would be a scan of the index. `entry_id` remains the
identity everything outside the schema file uses -- it is what appears in URLs, and it is
random rather than sequential so that an id in an assistant's transcript does not say how
much this person has told us and roughly when.

`AUTOINCREMENT` rather than a bare `INTEGER PRIMARY KEY` is the part worth writing down.
Plain SQLite rowids are **reused** once the highest row is deleted, and rows are deleted
here constantly, because purging is routine (ADR-0005). A reused id could collide with a
search-index row that a bug had failed to remove, and the result would be an entry
answering searches for text that belonged to something somebody had forgotten. Monotonic
ids turn that silent corruption into an impossible state. The cost is a `sqlite_sequence`
table and an id space that never comes back, which at this volume is nothing.

## What it costs

**Five nullable columns that should not be nullable.** `key`, `value_json`, `value_type`,
`body` and `note_kind` are all `NULL`-able in the DDL because the other kind of row needs
them empty, and on any given row either two or three of them are meaningless. `NOT NULL`,
the thing that would say what is actually true of each, is unavailable to all five.

**A CHECK constraint a reader has to parse.** Twelve lines of boolean that have to be read
in full before you know what the table holds. That is a real tax on the next person, and
it is why it is commented rather than left to speak for itself.

**Every query says which kind it means.** `entry_type = 'field'` turns up in predicates
and in partial indexes that two tables would not have needed, and a query that forgets it
gets notes back from a field lookup.

**The domain type carries both halves.** `Entry` has `key`, `value`, `value_type`, `body`
and `note_kind` all defaulting to `None`, so Python code reads `Optional` on five
attributes the database has already proved are not optional for the row in hand. The
constraint is what makes that safe, and the safety is not visible in the type.

## What would change our minds

A third kind of entry that shares fewer than half of the twelve. Something with its own
lifecycle, or no provenance, or no place in search, would be paying for eleven columns it
does not use in order to join a table it never queries with, and at that point it is its
own table with its own index and the merge problem above becomes real rather than
hypothetical.

Not a third kind that shares most of them. That is one more branch of the CHECK and one
more value in the discriminator, which is the cheap direction and is what this shape was
chosen to make cheap.
