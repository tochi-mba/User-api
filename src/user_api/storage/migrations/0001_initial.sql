-- The whole schema, as it stands at the first release.
--
-- Two shapes here are decisions rather than details, and both have an ADR:
--
--   * Fields and notes live in ONE table with a discriminator (ADR-0011). They share
--     twelve of their columns -- scopes, provenance, pinning, forgetting, confirmation,
--     revision, search -- and two tables would duplicate every one of them, need two FTS
--     indexes, and force recall to merge two rankings computed over different corpora.
--     The CHECK constraint below is what keeps the discriminator honest.
--
--   * Scopes are a junction table, not a JSON column (ADR-0004). A JSON column means a
--     virtual-table scan and a json_each() per row on the hottest read in the service;
--     a junction table with a covering index is an index seek.
--
-- Every index that serves a retrieval path is PARTIAL on `forgotten_at IS NULL`, because
-- every retrieval path filters that way. It keeps the index smaller and -- confirmed with
-- EXPLAIN QUERY PLAN over 4,000 rows -- lets the index serve the ORDER BY as well as the
-- WHERE, with no temp B-tree.

CREATE TABLE users (
    account_id TEXT NOT NULL PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

-- Deliberately holds no content. A preferred name is a field; duplicating it here would
-- create two places to change it and one of them would eventually be wrong. The row is
-- created on first write, so there is no "create" step for an assistant to forget and no
-- 404 on an account that has simply not written yet.

CREATE TABLE user_settings (
    account_id   TEXT    NOT NULL PRIMARY KEY REFERENCES users (account_id) ON DELETE CASCADE,
    erasure_mode TEXT    NOT NULL DEFAULT 'grace',
    grace_days   INTEGER NOT NULL DEFAULT 30,
    log_values   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL,

    CHECK (erasure_mode IN ('grace', 'immediate', 'tombstone')),
    CHECK (grace_days >= 0),
    CHECK (log_values IN (0, 1))
) STRICT;

-- Behind a SettingsStore port. A separate settings-api is the next service and becomes a
-- second adapter; nothing above the port changes when it does. That is the entire reason
-- this is a port on day one rather than a table somebody later has to prise out.

CREATE TABLE entries (
    -- An integer surrogate, purely so the FTS5 table has a stable rowid to be keyed by.
    -- AUTOINCREMENT rather than a bare INTEGER PRIMARY KEY: plain rowids are reused after
    -- the highest row is deleted, and a reused id could collide with a search-index row
    -- that a bug had failed to remove. Monotonic ids turn that silent corruption into an
    -- impossible state. entry_id remains the identity everything outside this file uses.
    seq           INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    entry_id      TEXT    NOT NULL UNIQUE,
    account_id    TEXT    NOT NULL REFERENCES users (account_id) ON DELETE CASCADE,
    entry_type    TEXT    NOT NULL,

    -- Field-only. A field is a fact with a name: preferred_name, timezone, blood_type.
    key           TEXT,
    value_json    TEXT,
    value_type    TEXT,

    -- Note-only. A note is something that happened, was noticed, or was learned.
    body          TEXT,
    note_kind     TEXT,

    -- Shared by both, which is the argument for one table.
    description   TEXT    NOT NULL,
    sensitivity   TEXT    NOT NULL DEFAULT 'normal',

    -- Provenance, split into the half the server verified and the half it did not.
    -- asserted_by is derived from the token server-side and is trustworthy. source is
    -- what the writer claims, and the server cannot tell whether a person said it or a
    -- model inferred it. The API reports both and labels which is which.
    source        TEXT    NOT NULL,
    source_detail TEXT,
    asserted_by   TEXT    NOT NULL,

    pinned        INTEGER NOT NULL DEFAULT 0,
    revision      INTEGER NOT NULL DEFAULT 1,

    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    -- Separate from updated_at on purpose: this is the last time a HUMAN said it was
    -- still true. An assistant asserting a stale fact confidently is the failure that
    -- makes this service embarrassing rather than useful, so both come back on every
    -- read and the consumer can say "you told me in March" instead of asserting it.
    confirmed_at  TEXT,
    forgotten_at  TEXT,

    -- What the FTS index is built from. Denormalised so the index and the row are written
    -- from the same value in the same transaction.
    search_text   TEXT    NOT NULL,

    CHECK (entry_type IN ('field', 'note')),
    CHECK (sensitivity IN ('normal', 'sensitive')),
    CHECK (pinned IN (0, 1)),
    CHECK (revision > 0),
    -- The discriminator, enforced rather than documented. Without this the table would
    -- happily hold a "field" with a note body and no key, and every reader would have to
    -- defend against it.
    CHECK (
        (entry_type = 'field'
            AND key IS NOT NULL AND value_json IS NOT NULL AND value_type IS NOT NULL
            AND body IS NULL AND note_kind IS NULL)
        OR
        (entry_type = 'note'
            AND body IS NOT NULL AND note_kind IS NOT NULL
            AND key IS NULL AND value_json IS NULL AND value_type IS NULL)
    )
) STRICT;

-- One live field per key per account. Partial, so a forgotten field does not block the
-- key being used again -- which is what makes "forget it and tell me again" work.
CREATE UNIQUE INDEX idx_entries_field_key
    ON entries (account_id, key)
    WHERE entry_type = 'field' AND forgotten_at IS NULL;

-- The default retrieval order, newest touch first. Covers the ORDER BY as well as the
-- WHERE, so the top page of a 4,000-entry account is an index seek and twenty steps.
CREATE INDEX idx_entries_recent
    ON entries (account_id, updated_at DESC, entry_id DESC)
    WHERE forgotten_at IS NULL;

-- The stable order: created_at never changes, so a cursor walking it cannot skip a row
-- that was updated mid-walk. Export uses it, and so does any caller who needs the
-- guarantee more than they need recency.
CREATE INDEX idx_entries_oldest
    ON entries (account_id, created_at, entry_id)
    WHERE forgotten_at IS NULL;

-- The always-load set. Read once at the start of every conversation, so it gets its own.
CREATE INDEX idx_entries_pinned
    ON entries (account_id, updated_at DESC, entry_id DESC)
    WHERE pinned = 1 AND forgotten_at IS NULL;

-- "What have I not checked in a year?" -- the staleness sweep an assistant uses to decide
-- what to ask about. NULLs sort first in SQLite, which is right: never confirmed is the
-- stalest thing there is.
CREATE INDEX idx_entries_confirmed
    ON entries (account_id, confirmed_at)
    WHERE forgotten_at IS NULL;

-- The sweeper's index, and the only one that wants the forgotten rows. Not account-scoped,
-- because the sweep is over every account at once.
CREATE INDEX idx_entries_forgotten
    ON entries (forgotten_at)
    WHERE forgotten_at IS NOT NULL;

CREATE TABLE entry_scopes (
    entry_id TEXT NOT NULL REFERENCES entries (entry_id) ON DELETE CASCADE,
    scope    TEXT NOT NULL,

    PRIMARY KEY (entry_id, scope)
) STRICT, WITHOUT ROWID;

-- An entry with NO rows here is unrestricted and visible to any valid token. An entry
-- with rows here is visible only when the token's granted scope is among them. The scope
-- comes from the token's audience and never from a query parameter -- see ADR-0004.
CREATE INDEX idx_entry_scopes_scope ON entry_scopes (scope, entry_id);

-- A plain FTS5 table, NOT external-content. External content requires issuing 'delete'
-- commands carrying the OLD values on every update, and a single missed one silently
-- corrupts the index -- the classic footgun. Here the index is a second copy maintained by
-- one private helper called from every write path, and a test class asserts the table and
-- the index agree after create, revise, forget, purge and cascade.
--
-- porter unicode61 so a search for "preferring" matches "prefer", which is what makes
-- recall work on prose somebody typed months ago.
CREATE VIRTUAL TABLE entry_search USING fts5 (
    search_text,
    tokenize = 'porter unicode61'
);

CREATE TABLE events (
    sequence    INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT    NOT NULL,
    at          TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    entry_id    TEXT,
    entry_type  TEXT,
    key         TEXT,
    asserted_by TEXT    NOT NULL,
    source      TEXT,
    -- JSON, and NULL unless the account has turned log_values on. The event log is a
    -- second copy of the personal data: "changed diagnosis from X to Y" IS the sensitive
    -- fact. So by default events record what changed and who changed it, not the values.
    detail      TEXT
) STRICT;

-- Deliberately NO foreign keys. Deleting an entry must not delete the record that it was
-- deleted: an event whose entry is purged keeps its metadata row, and the record that
-- something was forgotten survives the thing itself.
--
-- Ordered by sequence rather than by `at`, because the clock is injectable and two events
-- in one tick share a timestamp -- which would make the order of a page non-deterministic
-- in exactly the tests that care about it.
CREATE INDEX idx_events_account ON events (account_id, sequence DESC);
