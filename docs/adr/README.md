# Architecture decision records

One file per decision that future-us would otherwise re-litigate. Each says what was
decided, what it cost, and what would make us change our minds. The format, and the habit,
are keyring's.

| ADR | Decision |
| --- | --- |
| [0001](0001-data-not-instructions.md) | A record is data, never instructions |
| [0002](0002-no-secrets-here.md) | No credentials in a record; keyring is next door |
| [0003](0003-no-encryption-at-rest.md) | No encryption at rest, and what the file mode does instead |
| [0004](0004-scope-from-token-audience.md) | Scope comes from the token audience, not a query parameter |
| [0005](0005-erasure-is-a-setting.md) | Erasure is a setting, and what purge actually does |
| [0006](0006-provenance-is-partly-a-claim.md) | Provenance is split into what we verified and what we did not |
| [0007](0007-settings-behind-a-port.md) | Settings behind a port on day one |
| [0008](0008-keyword-search-not-embeddings.md) | Keyword search, not embeddings |
| [0009](0009-local-jwks-verification.md) | Tokens verified locally against keyring's JWKS |
| [0010](0010-sqlite.md) | SQLite, and why `synchronous` differs from keyring's |
| [0011](0011-one-entries-table.md) | One entries table for fields and notes |
