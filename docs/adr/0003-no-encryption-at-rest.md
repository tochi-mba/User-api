# ADR-0003: No encryption at rest

**Status:** accepted

## Context

This is the honest one, so it starts with what is in the file.

`var/user.db` holds somebody's health notes, the names of the people in their household,
what they are allergic to, who their therapist is and what they said about them last
month. All of it in plaintext, in a `TEXT` column, in a format any SQLite build can open.
Twice over, in fact: `search_text` is a denormalised copy of the same content, and it is
copied again into the `entry_search` FTS5 table so that search works. A `grep` over the
`-wal` file will find a note before the database has even checkpointed.

keyring, next door, is encrypted at rest, and the temptation is to say this service is
too. It is not, and the reason is in the next two sections rather than in an oversight.

## Decision

The database is not encrypted. The file is 0600, along with its `-wal` and `-shm`
sidecars, which hold the same data the file does. The mode is applied after opening rather
than before, because there is nothing to chmod until SQLite has created the file, which
leaves one `open()` worth of window where a brand new and empty database exists at 0644. A
directory the service creates for the database is created 0700; an existing one is left
exactly as it is, because the configured path names a file and its parent may not be ours.

`database.py` puts the consequence in one line: "The mode is therefore not defence in
depth, it is the defence."

Two related settings are on, and neither is encryption. `PRAGMA secure_delete = ON`
overwrites freed pages rather than marking them free, and `PRAGMA
wal_checkpoint(TRUNCATE)` after a purge is what actually removes deleted bytes from the
write-ahead log. Both are about erasure being real (ADR-0005), not about a reader who has
the file.

## What 0600 protects

Every other process and every other user on the box, which is by far the likelier reader.

That is not a small set, and it is the one that gets exercised in practice: a backup agent
running as a different user, a log shipper with a generous glob, a second service on the
same host, a cron job somebody wrote in a hurry, another person's shell on a machine that
was never meant to be shared. Every one of those reads a 0644 SQLite file without trying
and without leaving much trace. None of them reads a 0600 one.

## What it does not protect

Plainly, and in full:

- **A stolen disk**, a lost laptop, or a machine sold or recycled without wiping.
- **A backup copied somewhere else.** The `VACUUM INTO` copy the backup procedure asks for
  is another plaintext SQLite database, and it is usually the one that ends up somewhere
  with laxer permissions than the original.
- **A filesystem or volume snapshot**, and anything replicating blocks off the host.
- **Anybody who can become the service user.** The process reads the file, so anything
  running as that user reads the file, including a bug in this service that can be talked
  into reading a path.
- **Root**, entirely and without qualification.
- **Anybody who can read the process's memory** -- a core dump, a crash reporter, a
  debugger, a container platform that snapshots memory. Rows are ordinary Python strings
  on their way out.
- **Anything holding a valid token.** `export_user` returns every entry that token's scope
  permits, in plaintext, by design. Encryption at rest would not have changed that by a
  single byte, and it is the widest hole in practice.

Logs are deliberately not on that list. Entry content is never passed to a log call, the
names that carry it are on the redaction list as well, and a test drives a request whose
body contains a sentinel and asserts it appears in no log record on the success path or on
any failure path.

## Why not encrypt

Because the service exists to retrieve, and encryption at the value level takes retrieval
away.

Full-text search is most of what justifies this service over a text file: somebody asks
"what did they say about their sister's wedding", and `search_user` answers it from prose
written months earlier, stemmed by `porter unicode61` so "preferring" finds "prefer". That
works because `entry_search` holds the words. You cannot full-text search ciphertext, and
an FTS5 index over encrypted values is an index over noise.

The obvious compromise -- encrypt `value_json` and `body`, leave `search_text` alone -- is
security theatre, and it is worth being precise about why. `search_text` is not a summary
or a set of hashes. For a field it is the key, the description and the value rendered as
text, which for a string value is the value verbatim. For a note it is the description and
the body, verbatim. An attacker holding the file would simply read the column that was
left in the clear, and we would have spent a key management story to make the data
marginally less convenient to steal.

A whole-file encrypted SQLite build -- SQLCipher and its relatives -- does keep FTS5
working, because the encryption is below the b-tree. It is not the stdlib `sqlite3` driver
this service runs on, so it costs a compiled dependency and a divergence from keyring's
storage layer; and the key has to be readable by the process at startup, which on a single
box usually means a file or an environment variable sitting next to the database. Against
every threat in the list above except the stolen disk, that buys nothing. Against the
stolen disk, full-disk encryption buys the same thing for no dependency at all, which is
why it is an operator requirement below rather than a line of Python.

## What it costs

The cost of this decision is paid by the operator, not by the code, so it is written as
requirements rather than as caveats. A deployment that does not meet them is one where the
data is less protected than this document claims.

- **Full-disk encryption on the host.** This is the mitigation for the stolen-disk case,
  and there is no other.
- **A 0700 parent directory.** The service sets that mode only on a directory it creates
  itself. If the path points into a directory that already exists, its mode is whatever
  the operator made it, and a 0755 parent with a 0600 file still tells everyone on the box
  exactly where the record lives and how big it is.
- **Encrypted backups, treated as databases.** A `VACUUM INTO` copy deserves the same
  handling as the original: encrypted at rest, owner-only, and not in object storage with
  a default policy. `cp` of a live WAL database is not a backup at all -- it can miss
  committed transactions -- so the copy is always a real one.
- **Not on a shared box.** Any account that can become the service user reads everything,
  and 0600 has nothing further to say about it.
- **Nowhere a web server serves from.** The default `var/user.db` is a relative path, and
  a relative path is easy to point at a directory something else is publishing.
- **Backups taken before an erasure still hold what was erased.** Restoring one resurrects
  it. Nothing in this service can reach into a backup, and the operator is the only person
  who can.

## What would change our minds

A SQLite-compatible encrypted-at-rest store with a working FTS index, where the key does
not live on the same disk as the data -- supplied at start-up by an operator, or fetched
from something that can refuse to hand it over. The blocker today is not the encryption,
it is that a key sitting beside the database defends against exactly one of the threats
listed above, and that one is already covered.

A deployment whose threat model is a stolen disk rather than a co-tenant process. This
service is currently a single process on a box we control, serving a household. On a
laptop that leaves the house, or on a host somebody else administers, the ranking of the
list above inverts and so does the answer.
