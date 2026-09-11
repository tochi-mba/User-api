CREATE INDEX idx_entries_confirmed
    ON entries (account_id, confirmed_at)
    WHERE forgotten_at IS NULL;
CREATE UNIQUE INDEX idx_entries_field_key
    ON entries (account_id, key)
    WHERE entry_type = 'field' AND forgotten_at IS NULL;
CREATE INDEX idx_entries_forgotten
    ON entries (forgotten_at)
    WHERE forgotten_at IS NOT NULL;
CREATE INDEX idx_entries_oldest
    ON entries (account_id, created_at, entry_id)
    WHERE forgotten_at IS NULL;
CREATE INDEX idx_entries_pinned
    ON entries (account_id, updated_at DESC, entry_id DESC)
    WHERE pinned = 1 AND forgotten_at IS NULL;
CREATE INDEX idx_entries_recent
    ON entries (account_id, updated_at DESC, entry_id DESC)
    WHERE forgotten_at IS NULL;
CREATE INDEX idx_entry_scopes_scope ON entry_scopes (scope, entry_id);
CREATE INDEX idx_events_account ON events (account_id, sequence DESC);
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
CREATE TABLE entry_scopes (
    entry_id TEXT NOT NULL REFERENCES entries (entry_id) ON DELETE CASCADE,
    scope    TEXT NOT NULL,

    PRIMARY KEY (entry_id, scope)
) STRICT, WITHOUT ROWID;
CREATE VIRTUAL TABLE entry_search USING fts5 (
    search_text,
    tokenize = 'porter unicode61'
);
CREATE TABLE 'entry_search_config'(k PRIMARY KEY, v) WITHOUT ROWID;
CREATE TABLE 'entry_search_content'(id INTEGER PRIMARY KEY, c0);
CREATE TABLE 'entry_search_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE 'entry_search_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE 'entry_search_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
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
CREATE TABLE schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT    NOT NULL
) STRICT
;
CREATE TABLE sqlite_sequence(name,seq);
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
CREATE TABLE users (
    account_id TEXT NOT NULL PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
