# Architecture

user-api is one service: one process, one port, one SQLite file, one OpenAPI document.
Inside it, hexagonal -- dependencies point inward, and the direction is enforced by
import-linter contracts in `pyproject.toml` rather than by convention.

```
                    ┌──────────────────────────────────┐
   an assistant ───▶│  api/      routers, wire schemas,│
                    │            problem+json, the     │
                    │            identity dependency   │
                    └───────────────┬──────────────────┘
                    ┌───────────────▼──────────────────┐
                    │  users/    UserService, the       │
                    │            record row, settings,  │
                    │            the erasure path       │
                    └───────────────┬──────────────────┘
                    ┌───────────────▼──────────────────┐
                    │  entries/  EntryStore + SQL       │
                    │            adapter, the FTS index │
                    └───────────────┬──────────────────┘
                    ┌───────────────▼──────────────────┐
                    │  events/   EventLog + SQL adapter │
                    └───────────────┬──────────────────┘
                    ┌───────────────▼──────────────────┐
                    │  auth/     JwksClient,            │
                    │            TokenVerifier          │
                    │            (the only jwt/httpx)   │
                    └───────────────┬──────────────────┘
                    ┌───────────────▼──────────────────┐
                    │  storage/  one connection, one    │
                    │            thread, migrations     │
                    └───────────────┬──────────────────┘
                    ┌───────────────▼──────────────────┐
                    │  domain/   pure types & rules     │
                    │            (imports nothing)      │
                    └──────────────────────────────────┘

  core/  config · clock · logging · request context · version · preferences · composition root
         (a shared kernel every layer may use, except domain)
```

`domain/` is the bottom because it is where the rules are written in a form a person can
read. `Entry.visible_to` is the scope boundary in Python, sitting beside the SQL predicate
that actually enforces it, and a test asserts the two agree -- a boundary that exists only
as a `WHERE` clause is a boundary nobody can read. The same goes for `to_match_query`,
`normalize_key`, `validate_value` and `looks_like_a_credential`: each is a rule the service
layer calls, none of them knows what a row or a status code is.

`auth/` sits above `storage/` and below the three store packages because it needs nothing
from the database. A token is checked against a cached public key and nothing else, which
is what makes it possible to keep `jwt` and `httpx` inside one package.

## The five contracts, and what each prevents

`make imports` runs all five. They are not documentation of the diagram above; they are the
reason the diagram is still true.

| Contract | What it forbids | What it prevents |
| --- | --- | --- |
| **Domain is independent** | `user_api.domain` importing any other package. | A rule that quietly needs a store. The moment `domain/scopes.py` can see a row, "what does this token grant" stops being answerable by reading one function. |
| **Layers point inward** | An import from a lower layer to a higher one. | `entries/` learning what an erasure mode is. It reports *who* has forgotten something; the sweeper, which holds the settings, decides what that means. |
| **SQL stays behind the stores** | `api`, `auth` and `domain` importing `user_api.storage` or `sqlite3`, indirectly included. | A router that *could* write a query, which is a router that will eventually contain one -- and a query written outside a store is a query that forgot `account_id`. |
| **Keyring is spoken to from one package only** | Every package except `auth` importing `jwt` or `httpx`, indirectly included. `core.config` is left off so it can validate the settings-api token with `keyring_client.check_service_token`. | "How do we decide who this is" having more than one place to look. It is also why `JwksClient.key_for` returns `Any` rather than `jwt.PyJWK`: a signature naming a library's type is how the library leaks out of the package meant to hold it. |
| **Talking to settings-api stays behind the preferences module** | Every package except `core.preferences` importing `settings_client`. | A call site re-implementing caching, revalidation, single-flight and outage behaviour, slightly wrong, and presenting a user token without the one module that knows how to degrade. |

The layers contract is declared `exhaustive = false`, so `core/` sits outside it
deliberately: config, the clock, logging, preferences and the request context are a shared
kernel, and the composition root in `core/container.py` is the one module that is allowed to
know every adapter by name.

## Ports and adapters

Four ports, four SQL adapters, one substitutable clock. Every consumer imports the port
under `TYPE_CHECKING` and receives the adapter from the composition root.

| Port | Adapter | Why it is a port |
| --- | --- | --- |
| `entries.store.EntryStore` | `entries.sql_store.SqlEntryStore` | The biggest surface: fields, notes, scopes, search, the caps and the index. Every method takes `account_id`, and every read takes `granted`. |
| `events.log.EventLog` | `events.sql_log.SqlEventLog` | Its write methods are **synchronous and take a live connection**, because an event has to be written in the transaction it describes. |
| `users.store.UserStore` | `users.sql_store.SqlUserStore` | Existence and erasure. Holds no content -- a preferred name is a field. |
| `users.settings.SettingsStore` | `users.sql_settings.SqlSettingsStore` | `erasure_mode`, `grace_days` and `log_values`. They stay here because the erasure sweeper has no user token to present to settings-api, and `log_values` is written by the public PUT and read by the event log on the same row. Request-path caps (`max_pinned`, `search_default_limit`) are read from settings-api in `core.preferences` instead. |
| `core.clock.Clock` | `core.clock.SystemClock` | Three rules here are arithmetic on a date, and one is measured in days. |

`tests/unit/test_ports.py` is the file that makes the Protocols mean something. A structural
Protocol checks nothing unless something asks, and every consumer imports its port under
`TYPE_CHECKING`, so the port module's class body never executes at runtime. That file
imports them for real, annotates each adapter with its port so mypy compares the two, and
`isinstance`s it so the runtime agrees at the shape level. Either check alone catches half a
drift.

Three things are deliberately **not** ports. `Database` is a concrete class because there is
exactly one SQLite file and swapping it means swapping the adapters above it.
`TokenVerifier` and `JwksClient` are concrete because substituting them in a test would mean
testing against a fake of the one component whose job is to be suspicious; the suite
substitutes the *transport* underneath instead, and mints real RS256 tokens against a real
JWKS document. `UserService` is concrete because it is the thing being tested. Preferences
are a `PreferenceSource` protocol in `core.preferences`, constructed in the composition
root and never fetched at startup: an empty `USER_API_SETTINGS_API_BASE_URL` keeps
today's behaviour exactly.

**Why erasure did not move.** settings-api's catalogue lists `user.erasure_mode`,
`user.grace_days` and `user.log_values` as well as the two request-path caps. The sweeper
(`Erasure.sweep_once`) reads the first two for every account with forgotten entries from a
background task, with no request and so no user token. A local copy refreshed on each
request would miss a person who switched to `tombstone` directly in settings-api until
they next called user-api, and in the meantime the sweeper would destroy entries they had
just asked to keep. `log_values` is written by `PUT /v1/user/settings` and read by the
event log on the same row. So those three stay on `SqlSettingsStore`; only `max_pinned`
and `search_default_limit` are read from settings-api, clamped to the deployment ceilings
-- a person may lower them and never raise them.

There is no `save(entry)` anywhere. Writing a whole entry back means writing back everything
a caller read some time ago, so two requests revising two different parts of one entry each
write back a record missing the other's change and the loser never finds out. Each write
says what it changes, and `revision` is bumped so a reader can tell that something did.

## A request, from token to row

```
GET /v1/user/entries?q=lisbon&scope=home      Authorization: Bearer <token>

  RequestContextMiddleware
    → bind a request id (yours, capped at 64 chars, or a fresh one)
    → every log record from here on carries it

  get_identity (api/dependencies.py)
    → read `kid` from the token's UNVERIFIED header -- it chooses the key
    → JwksClient.key_for(kid): cached? fresh? else one fetch, rate-limited per kid
        · fetch failed        → KeyringUnreachableError → 503 + Retry-After
        · fetched, no such kid → AuthenticationError    → 401
    → read `aud` from the UNVERIFIED claims, because PyJWT checks only an audience
      it has been told; nothing is decided from this reading
    → jwt.decode(..., algorithms=["RS256"], audience=<that>, issuer=<pinned>,
                 require=exp/iat/iss/sub/aud, verify_exp=False, verify_iat=False)
    → expiry re-checked against the INJECTED clock
    → granted_scope(aud) → Identity(account_id=sub, audience=aud, granted_scope=...)
    → set_account_id(sub): every later log record says whose request this was

  search_user (api/routers/entries.py)
    → parse the query parameters into Filters; nothing here touches a store

  UserService.search (users/service.py)
    → normalise any key or key_prefix, so ?keys=Preferred%20Name means what it says
    → check_filterable(scope, granted=...)          ← 403 here, before any SQL
    → ordering = RELEVANCE when q is present, else what was asked for
    → decode the cursor, refusing one issued for a different ordering

  SqlEntryStore.search → _search_ranked (entries/sql_store.py)
    → to_match_query("lisbon") -- every token quoted into a literal   ← 422 here
    → _filter_clauses(account_id, filters): every fragment a constant,
      every caller value a bound parameter
    → SELECT ... JOIN entry_search ON rowid = e.seq WHERE entry_search MATCH ?
        AND e.account_id = ? AND <filters> AND <_VISIBLE>
      ← account isolation lives in that outer WHERE, because the FTS index is
        shared across accounts and MATCH alone finds other people's rows
    → ORDER BY rank ASC, entry_id ASC LIMIT limit + 1   ← one extra row, so
      "is there a next page" is an observation rather than a guess

  back up
    → _paginate trims the extra row into a cursor
    → EntryResponse.of(entry) for each: value/body, and all four provenance fields
    → X-Request-ID and X-Response-Time-Ms on the way out
```

A write follows the same path as far as the service, and then the order of the checks
matters:

1. **Shape** -- normalise the key, validate the value, check the description. A caller who
   sent nonsense is told so, and nothing has touched the database.
2. **Credentials** -- refuse anything that looks like a secret, naming keyring. Before the
   scope check, so a caller pasting an API key is told what is actually wrong rather than
   being told it lacks a scope and coming back with the same secret under a different one.
3. **Scopes** -- are these names real (`check_known`), and does this token carry them
   (`check_writable`). Known first, so a caller who misspelled the scope it *does* hold is
   told it misspelled it.
4. **Existence and caps** -- inside the store's transaction, where they cannot go stale.

## Transaction boundaries

Every database call goes through one connection on one dedicated worker thread, submitted as
one whole callable. A transaction is therefore indivisible by construction rather than by
convention: there is no point inside it at which another caller can interleave, because
there is no other thread that could run one. The cost belongs in the open -- **a cancelled
request's write may still commit**, because the queued callable runs to completion
regardless of who is still waiting.

`Database.run` is for reads and holds no transaction: a single SQLite statement is atomic by
itself. `Database.transact` wraps `BEGIN IMMEDIATE` / `COMMIT`, rolling back on anything
raised, including a domain error refusing the write.

| One transaction | Contains | Why together |
| --- | --- | --- |
| `put_field`, `write_note` | the cap counts, the insert or update, the `entry_scopes` rows, the FTS index row, **and the event append** (which trims the log in the same statement) | A service that wrote the entry and then logged it would have two transactions, and a crash or a rollback between them leaves either a change nothing recorded or a record of a change that did not happen. Neither is a log. The caps are inside for the same reason: two concurrent writes that both counted first would both pass. |
| `revise`, `confirm`, `forget` | the visibility-filtered read, the update, the scope rows and index row where they change, the re-read, and the event | The read that proves you may touch this entry and the write that touches it are one unit. `_VISIBLE` is applied to the read, which is how a `user.home` token is stopped from revising a health-scoped entry it could not have seen. |
| `UserStore.ensure` | insert-if-absent, then the read-back | Spelled as two calls, the queue is free to run somebody else's `DELETE /v1/user` in the gap, and the read-back comes home empty for a record this method has already promised to return. |
| `SettingsStore.update` | the upsert, whose `DO UPDATE` arm `COALESCE`s each column against itself, and the read-back | So a caller changing one setting cannot null the other two, and so what it is told is what is stored rather than what was proposed. |
| One purge batch | for each entry: `_unindex`, `DELETE FROM entries` (scopes follow by cascade), **and** `purge_entry_values_in` stripping the logged values from the events *about* that entry | Between two transactions there is a moment where the entry is gone and its old value is still sitting in the event that recorded the change -- in the same file, findable with `grep`. |
| `delete_user` | the event count and delete, the index rows, the entries, the settings, the record | And in that order: `entry_search` rows know their entry only by `entries.seq`, so deleting the entries first strands index rows that no statement can reach by account any more, each still holding the words of something somebody asked to have destroyed. |

Two things are deliberately **outside** a transaction.

**The checkpoint.** `PRAGMA wal_checkpoint(TRUNCATE)` cannot run with a transaction open, so
it follows the purge rather than joining it -- once per sweep, and once after
`delete_user`. It is the step that actually erases: a `DELETE` takes the row out of the
b-tree and leaves what it held in the `-wal` file. Running it per row would turn a cheap
step into a pathological one; `VACUUM` would also work and rewrites the whole database under
a write lock to achieve the same thing.

**The settings read that decides whether to purge now.** `forget_entry` marks the entry in
one transaction and then reads the account's mode; `immediate` mode's destruction is a
second transaction plus the checkpoint. The window between them is a window in which the
entry is already invisible to every read path, which is the property that matters.

## What one process costs, and what it buys

One process is a decision, not a limitation ([ADR-0010](adr/0010-sqlite.md)). It buys
indivisible check-and-write, which every cap in this service depends on, and it costs
concurrent writers, which nothing here wants. The sweeper is one `asyncio` task in that same
process: a failed sweep is caught, logged and retried on the next tick, and nothing at all
covers the process not running.

`PRAGMA foreign_keys` is read back and verified rather than trusted. It defaults to off, is
per-connection rather than stored in the file, and is a **silent no-op while a transaction
is open** -- through a driver that opens implicit transactions it can report success and
leave every foreign key decorative. Here that would mean a forgotten entry keeping its scope
rows and a deleted record keeping its entries: erasure that reports success and erases
nothing. The connection is opened `isolation_level=None` for that reason, and
`require_foreign_keys` refuses to continue if the pragma did not take.

When more than one process needs to write -- another replica, or a worker outside the API --
that is a Postgres adapter, and the four ports above are what make it an adapter rather than
a rewrite.
