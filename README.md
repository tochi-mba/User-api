# user-api

Somewhere to keep structured data about the **person** an assistant is talking to, and to
get it back: what they are called and how they want to be addressed, their timezone, who is
in their household, what they are allergic to, what they are working on, and what they said
last month.

An assistant loads the record once at the start of a conversation to know who it is talking
to, and writes to it as it learns. The subject is a person rather than a system, and that
single fact drives most of the design: everything comes back with provenance attached so it
can be rendered as a claim rather than obeyed as an instruction, anything that looks like a
credential is refused rather than stored, "forget that" reaches the bytes on disk, and no
request can name a person other than the one its token was minted for.

```
GET    /v1/user                      -> counts, and every pinned entry your token may see
GET    /v1/user/schema               -> which field keys exist, and what each one means
PUT    /v1/user/fields/{key}         -> one named fact: preferred_name, timezone, ...
POST   /v1/user/notes                -> something that happened, was noticed, or was learned
GET    /v1/user/entries?q=lisbon     -> full-text and filtered search across both
POST   /v1/user/entries/{id}/confirm -> a human says this is still true
DELETE /v1/user/entries/{id}         -> forget one thing
DELETE /v1/user                      -> forget all of it, for good
```

There is no account id in any of those paths, and there is no parameter that names a
subject. Which record you are reading comes out of your token, and so does which
compartment of it you can see.

## Its relationship to keyring

[keyring](../keyring-api) is the first service in this family and holds accounts and
credentials. This is the second, and it has no accounts of its own: no registration, no
login, no password, and no way to ask keyring anything about a person. The only identity it
ever learns is the `sub` of a token keyring signed.

Tokens are verified **locally**, against the JWKS document keyring publishes. This service
fetches that document, caches it for an hour, and makes no other call to keyring in either
direction ([ADR-0009](docs/adr/0009-local-jwks-verification.md)). Two things follow, and
both are terms of the arrangement rather than gaps:

- A signed token cannot be revoked. Logging out of keyring ends a session there and ends
  nothing here; an already-minted token works until it expires, which is about fifteen
  minutes.
- Nothing tells this service that an account is gone. A keyring account deleted this
  morning leaves a record here that nobody has mentioned it to.

The other half of the relationship is what this service refuses. A field value or a note
body that looks like an API key, a private key or a JWT is refused with a 422 naming
keyring as the right home ([ADR-0002](docs/adr/0002-no-secrets-here.md)). keyring is
encrypted at rest, never returns a stored secret, and is audited. This service is the
opposite of all three.

## Quick start

```bash
uv sync --all-extras --group dev          # or: make install
cp .env.example .env                      # every setting has a working default
make run                                  # http://127.0.0.1:8002/docs
```

Every route except `GET /healthy` needs a bearer token from keyring's
`POST /v1/auth/service-token`, minted with audience `user` (unscoped entries only) or
`user.<scope>` (those, plus entries tagged with that scope). A `USER_API_`-prefixed
environment variable that matches no setting is a **startup error**, not a warning.

`make check` is the gate: format, lint, strict types, the four layering contracts, and the
suite at 100% branch coverage.

## The five properties it is built around

1. **No request can name another person.** Every path is `/v1/user`; the account id comes
   from the verified `sub` and appears in no URL. Every store method takes an `account_id`
   and it is not optional on any of them, so a cross-account read is not forbidden, it is
   inexpressible. Another account's entry reads back exactly like one that never existed.
2. **Scope comes from the token's audience, never from a query parameter.** `?scope=` can
   narrow within what a token already grants and is refused, loudly, if it would widen
   ([ADR-0004](docs/adr/0004-scope-from-token-audience.md)). One scope per token, because a
   token granting two is a token whose holder can correlate across two compartments.
3. **A record is data, never instructions.** An assistant that reads web pages and email
   writes here from untrusted text, so every entry carries provenance -- the token that
   wrote it, what that writer claimed about where it came from, and when a human last said
   it was still true -- and the documentation tells the consumer to render it as "your
   notes say" ([ADR-0001](docs/adr/0001-data-not-instructions.md)).
4. **Credentials are refused, not stored.** This store is plaintext on disk, returned in
   full to any token whose scope permits it, and indexed for full-text search. A secret in
   it would be a secret in a search index.
5. **Erasure reaches the bytes.** A purge is `DELETE` plus a truncating WAL checkpoint,
   because a delete alone leaves the value in the write-ahead log where `grep` will find it
   ([ADR-0005](docs/adr/0005-erasure-is-a-setting.md)). What `DELETE` on one entry *means*
   is the person's own setting: grace (the default), immediate, or tombstone.

## Where to read next

| Document | What it covers |
| --- | --- |
| [AGENTS.md](AGENTS.md) | How work is done here: the map, the invariants, the recipes. |
| [docs/api.md](docs/api.md) | The HTTP contract, the error shape, and a worked example. |
| [docs/architecture.md](docs/architecture.md) | Ports, adapters, contracts, transactions. |
| [docs/operations.md](docs/operations.md) | Running it, backing it up, and the hardening it needs. |
| [docs/mcp.md](docs/mcp.md) | Fronting it with MCP tools, and how a bridge must render a record. |
| [docs/testing.md](docs/testing.md) | How the suite is organised and what the coverage gate means. |
| [docs/adr/](docs/adr/) | The decisions, and what each one traded away. |

## Things worth knowing before you run this for somebody

**Nothing in the database is encrypted.** Health notes, household names, a therapist's
name: plaintext, in a `TEXT` column, twice over, because the search index is a second copy.
The file is 0600 and that is the defence, not a layer of it
([ADR-0003](docs/adr/0003-no-encryption-at-rest.md)). Full-disk encryption, a 0700 parent
directory, encrypted backups and not sharing the box are operator requirements rather than
suggestions. The checklist is in [docs/operations.md](docs/operations.md).

**Backups taken before an erasure still contain what was erased.** Restoring one resurrects
it, including entries destroyed under `immediate` mode and entries destroyed by
`DELETE /v1/user`. Nothing in this codebase can reach into a backup, and the operator is the
only person who can do anything about it.

**Search is keyword, not semantic.** FTS5 with porter stemming, so "preferring" finds
"prefer" and "what does she like to drink" does not find "always orders an oat flat white"
([ADR-0008](docs/adr/0008-keyword-search-not-embeddings.md)).

**An assistant that pastes entry bodies into a system prompt has built a prompt-injection
persistence layer.** The service attaches provenance to everything it returns and cannot do
anything further; the rest is the consumer's, and [docs/mcp.md](docs/mcp.md) says what it
has to look like.
