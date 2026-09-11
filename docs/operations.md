# Running user-api

This service holds one person's own data about themselves, in plaintext, in a file. That
combination is why this page is a checklist rather than a description.

> **Do not skip the hardening section.** The application refuses to serve a request without
> a verified token -- there is no flag for that -- but nothing in the database is
> encrypted, so everything below the application layer is yours to get right.

## Configuration

Every setting is an environment variable prefixed `USER_API_`, read from the environment
and from `.env`. `.env.example` is the full list with the reasoning next to each one.

A `USER_API_`-prefixed variable that matches **no** setting is a **startup error**, not a
warning. pydantic-settings would otherwise ignore it, and `USER_API_ALOWED_SCOPES` would
leave the scope list on its default with nothing in the logs to say so -- in a deployment
that believed it had compartmentalised an assistant.

Three settings a deployment must actually think about:

| Setting | Why |
| --- | --- |
| `USER_API_KEYRING_JWKS_URL` | Where keyring publishes the public half of its signing key. The default points at `127.0.0.1:8001`, which is right for a local pair and wrong everywhere else. |
| `USER_API_KEYRING_ISSUER` | Pinned against every token's `iss`. A token from anywhere else is not a token. |
| `USER_API_DATABASE_PATH` | The one file everything lives in. The default `var/user.db` is a *relative* path, which is easy to point at a directory something else is publishing. |

Two more are worth a decision rather than a default. `USER_API_ALLOWED_SCOPES` is the set of
compartments this deployment recognises, and keyring has to be willing to mint
`user.<scope>` for each of them; an audience naming a scope that is not in this list is a
401, deliberately, rather than a token that silently grants nothing.
`USER_API_DEFAULT_GRACE_DAYS` is only the window a *new* record starts with -- each person
can change their own.

There is no setting that disables token verification, none that lets one account read
another's record, none that turns off the credential refusal, none that accepts an unsigned
or HS256 token, and none by which a caller-supplied scope can widen what its token grants.
Each would be a one-variable route past the property the service is built around, so they
are not configuration; they are the service.

## First start

```bash
uv sync --all-extras --group dev
cp .env.example .env          # then set the three above
make run                      # or: user-api
```

On startup, in this order: settings are loaded (and a misspelled variable stops the process
here), the database file is opened -- created if absent, along with its parent directory at
0700 if the service is the one creating it -- the connection's pragmas are applied and
`PRAGMA foreign_keys` is read back and verified, the file and its `-wal`/`-shm` sidecars are
chmodded to 0600, the migrations in `storage/migrations/` are applied in order inside their
own transactions, and the erasure sweeper starts.

**Nothing reaches keyring during startup.** The JWKS client is constructed, which is not a
network call, and the first fetch happens when the first token arrives. A service that
refused to start unless keyring were reachable would turn one outage into two at the worst
possible moment, because these two are restarted together. A user-api that starts cleanly
while keyring is down is working as designed: it will serve `/healthy` as **degraded** and
answer every authenticated request with a 503 until keyring comes back.

The service binds `127.0.0.1` by default and should stay there. It belongs behind a
TLS-terminating reverse proxy: every token it accepts is a bearer token, and over plain HTTP
anybody on the path has them.

## The hardening checklist

Nothing in the database is encrypted ([ADR-0003](adr/0003-no-encryption-at-rest.md)). Health
notes, the names of the people in somebody's household, what they are allergic to and what
they said about their therapist last month are in a `TEXT` column that any SQLite build can
open -- and twice over, because `search_text` and the FTS index hold a second copy. A `grep`
over the `-wal` file finds a note before the database has even checkpointed.

The file mode is therefore not defence in depth. It is the defence.

- [ ] **Confirm the database file is 0600 on the real deployment**, not merely in a test.
      `ls -l` it after the first start. The service applies the mode after opening, because
      there is nothing to chmod until SQLite has created the file, which leaves one
      `open()` worth of window where a brand new and empty database exists at 0644. Check
      the `-wal` and `-shm` sidecars too: they hold the same data the file does.
- [ ] **A 0700 parent directory.** The service sets that mode only on a directory it creates
      itself; if the configured path points into a directory that already exists, its mode
      is whatever you made it. A 0755 parent with a 0600 file still tells everyone on the
      box exactly where the record lives and how big it is.
- [ ] **Full-disk encryption on the host.** This is the mitigation for a stolen disk, a lost
      laptop or a machine recycled without wiping, and there is no other. It is also why
      there is no encryption in the application: against every other threat a key sitting
      beside the database buys nothing.
- [ ] **Encrypted backups, treated as databases.** A `VACUUM INTO` copy is another plaintext
      SQLite database, and it is usually the one that ends up somewhere with laxer
      permissions than the original. Owner-only, encrypted at rest, and not in object
      storage with a default policy.
- [ ] **Not on a shared box.** Any account that can become the service user reads
      everything, and 0600 has nothing further to say about it. The likeliest reader is not
      an attacker: it is a backup agent running as a different user, a log shipper with a
      generous glob, a second service on the same host, or somebody else's shell on a
      machine that was never meant to be shared.
- [ ] **Nowhere a web server serves from.** Set `USER_API_DATABASE_PATH` to an absolute path
      outside any document root.
- [ ] **TLS at a reverse proxy**, with the app left on loopback.
- [ ] **A restore you have actually tried.** A backup you have never restored is a belief.

What 0600 does not protect, plainly: a stolen disk, a copied backup, a filesystem snapshot,
anybody who can become the service user, root, anybody who can read the process's memory,
and anything holding a valid token -- `export_user` returns every entry that token's scope
permits, in plaintext, by design. Encryption at rest would not have changed that last one by
a single byte, and it is the widest hole in practice.

## The database

One SQLite file in WAL mode, holding everything: the record, entries, their scopes, the
search index, the change log and the settings. The schema is applied at startup from
numbered files and recorded in a `schema_version` table; starting an up-to-date database
applies nothing, so it is safe on every start.

### Backing it up

**`VACUUM INTO`, never `cp`.** The database runs in WAL mode, so a plain copy of the main
file can miss transactions that are committed but still in the write-ahead log -- and
copying the three files separately gives you a set that were current at three different
moments. `VACUUM INTO` takes a consistent snapshot of a live database without stopping the
service:

```bash
sqlite3 /var/lib/user-api/user.db "VACUUM INTO '/backup/user-$(date +%F).db'"
```

The `sqlite3` CLI is a separate package and is not always installed. The same thing through
the Python that is already there:

```bash
python -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); \
           c.execute(f\"VACUUM INTO '{sys.argv[2]}'\"); c.close()" \
  /var/lib/user-api/user.db /backup/user-$(date +%F).db
```

Either way the snapshot arrives 0644, because it is a new file this service did not create.
`chmod 600` it, and put it somewhere encrypted. It is exactly as sensitive as the original
and there is no key that would make it less so.

### Restoring it

Stop the service, put the file at `USER_API_DATABASE_PATH`, delete any stale `-wal` and
`-shm` beside the old file first -- they belong to the database they were written for -- and
start. Then read the next section, because a restore is the one operation here that can undo
somebody's erasure.

### Looking inside it

```bash
sqlite3 /var/lib/user-api/user.db "SELECT account_id, created_at FROM users"
sqlite3 /var/lib/user-api/user.db "SELECT at, action, key FROM events
                                   ORDER BY sequence DESC LIMIT 20"
```

Both of those are metadata. Note what you are able to do here that no HTTP caller can:
`SELECT value_json, body FROM entries` returns a person's record in the clear. There is no
administrative surface over HTTP -- no route accepts an account id, so no operator can read
somebody's record through the API -- and that is not a gap the database closes. If you open
the file, you are reading it as a person rather than as an operator, and the person whose
data it is has no way to know you did.

## Erasure, for operators

Forgetting an entry and destroying it are two steps, and the second is the one with an
operational cost.

**What a purge actually does.** One transaction deletes the entry, its scope rows (by
cascade), its search-index row, and any values logged in the events *about* it; then
`PRAGMA wal_checkpoint(TRUNCATE)` runs outside that transaction. The checkpoint is not
housekeeping -- it is the step that erases. `DELETE` takes the row out of the b-tree and
leaves what it held in the `-wal` file, and even `wal_checkpoint(FULL)` leaves the page it
copied from sitting in the log. Only `TRUNCATE` empties it. The measurement is in
[ADR-0005](adr/0005-erasure-is-a-setting.md), and a test scans the bytes of both files for a
sentinel.

**The sweeper runs hourly** (`USER_API_PURGE_INTERVAL_SECONDS`, 3600 by default) and
**sweeps before it waits**, so a restart does not postpone what was already due. That
ordering matters more than it looks: the other way round, entries whose grace period
expired while the service was stopped would sit there for a further whole hour after it
came back, and somebody who deleted something yesterday and restarted this morning is
entitled to have it gone this morning. Each pass asks which accounts hold a forgotten entry, reads each one's settings, skips the
accounts on `tombstone`, and purges up to 500 entries per account -- bounded so one account
with a large backlog cannot hold the single database thread for an unbounded stretch. The
remainder waits for the next hour. The checkpoint runs once per sweep, not once per entry,
which is what keeps a cheap step cheap.

Consequences worth knowing before somebody asks:

- **A grace of zero days does not mean "now".** It means the cutoff is now, so the entry
  goes on the next sweep, up to an hour later. Somebody who means *now* wants `immediate`
  mode, which destroys inside the request and pays for its own checkpoint.
- **`tombstone` means delete never destroys anything.** That is what the person chose, and
  it is the mode most worth warning about, because an assistant offering to delete something
  on a tombstoned account is offering something narrower than it sounds.
- **The sweeper is one task in one process.** A failed sweep is logged as `sweep_failed` and
  retried on the next tick, which covers a transient error and does not cover the process
  not running. If erasure matters to you, `entries_purged` in the logs is the line that says
  it is happening.

### Old backups still contain what was erased

**This is the part only you can do anything about.** Restoring a backup taken before a purge
resurrects what it destroyed -- including entries destroyed under `immediate` mode and
entries destroyed by `DELETE /v1/user`, which is somebody asking for all of it to be gone.
Nothing in this codebase can reach into a backup, and nothing in it pretends to. It is a
property of having backups at all.

Three things follow, and all three are yours:

1. **Keep the retention window short enough to be honest about.** "Deleted, unless we
   restore from a backup taken in the last N days" is the true sentence; pick an N you are
   willing to say out loud.
2. **Know that a restore undoes every erasure made since the backup**, and that nobody will
   be told. If you restore, consider re-running nothing -- there is no replay -- and
   consider telling the person.
3. **Say so to the people whose data it is**, during onboarding rather than after. The API
   documentation for `DELETE /v1/user` says the same thing in the place a caller reads it.

## When keyring is down

Every authenticated request fails with **503** and `Retry-After: 5`, not 401. That
distinction is deliberate: a 401 would tell a person to log in again because *this* service
could not fetch a public key, and logging in again would not have helped.

What still works: `GET /healthy` answers, and reports `keyring` as degraded. The database is
fine and nothing is lost.

What to expect around the edges:

- **A cached key set lasts an hour** (`USER_API_JWKS_CACHE_SECONDS`). A keyring outage may
  therefore be invisible here for up to an hour for tokens signed by a `kid` already held,
  and `/healthy` will keep reporting `ok` while the cache is fresh.
- **A key rotation during an outage costs the first caller a 401.** An unknown `kid`
  provokes at most one fetch per `USER_API_JWKS_MIN_REFETCH_SECONDS` (60 by default); a real
  token arriving inside that window is refused with the same message a forgery gets. That is
  the rate limit working -- without it, a stream of tokens carrying invented `kid` values is
  one outbound request to keyring per inbound request, which is an amplifier anybody who can
  reach this service can aim at a service already having a bad day.
- **There is nothing to do here.** There is no local fallback, no cached-credential mode and
  no flag that accepts unverified tokens. Fix keyring; this service recovers on its own, at
  the next fetch.

## Watching it

`GET /healthy` needs no authentication, so everything on it is written on the assumption
that a stranger is reading it: the service version, the environment, uptime, a
**process-wide** entry count, and whether keyring's keys are fetchable. There is
deliberately no per-account number anywhere on it -- a count that moves when one person does
something is an oracle, whatever it is counting.

```json
{
  "status": "ok",
  "version": "0.1.0",
  "environment": "production",
  "uptime_seconds": 12.34,
  "checks": {
    "storage": {"status": "ok", "detail": {"entries": 52}},
    "keyring": {"status": "ok", "detail": {"reachable": true, "reason": null}}
  }
}
```

It answers **200** when every check passed and **503** when any did not, with the same body
shape either way. `degraded` today means one thing: keyring's signing keys could not be
fetched, so no token can be verified and every authenticated request is failing. The process
is up and the database is fine, which is exactly why reporting it as healthy would hide the
one outage this service cannot work around. The `reason` is short fixed text and never the
URL -- a URL can carry credentials in its userinfo, and this endpoint is open.

Logs are JSON (`USER_API_LOG_FORMAT=json`) with a `request_id` on every record and an
`account_id` on every authenticated one. The account id is keyring's opaque identifier; this
service never learns an email address, so a log record names a row rather than a human
being. Entry content is never passed to a logger, and a redaction pass replaces anything
whose field name carries content anyway. If you add a field that must never be logged, add
its name to `_CONTENT_FIELDS` in `core/logging.py` rather than remembering not to log it.

Lines worth alerting on: `sweep_failed` (erasure has stopped), `jwks_fetch_failed` and
`jwks_document_malformed` (keyring, or something in front of it, is not serving a key set),
and `jwks_refetch_suppressed` in volume (somebody is aiming invented key ids at you).

## What is deliberately not here

- **No administrative surface.** No route accepts an account id, so there is no endpoint by
  which an operator could read or delete somebody's record. The levers are `DELETE /v1/user`,
  which is the person's own, and the database file, which is yours.
- **No rate limiting.** Nothing here returns a 429. The proxy should cap request volume;
  the app cannot refuse a request it has already parsed. The one internal limit is the JWKS
  refetch window described above.
- **No encryption at rest, and no key management.** See the hardening checklist for what
  that means and [ADR-0003](adr/0003-no-encryption-at-rest.md) for why.
- **No revocation of a token before it expires.** A person who logs out of keyring has ended
  their session there and ended nothing here, for up to fifteen minutes
  ([ADR-0009](adr/0009-local-jwks-verification.md)).
- **No notice that an upstream account is gone.** This service cannot ask keyring anything
  about an account, so a keyring account deleted this morning leaves a record here that
  nobody has mentioned it to. Removing it is a `DELETE /v1/user` with that person's token,
  or your hands on the file.
- **No multi-replica anything.** One process, one connection, one thread
  ([ADR-0010](adr/0010-sqlite.md)).
