# ADR-0010: SQLite, and why `synchronous` differs from keyring's

**Status:** accepted.

## Context

keyring's ADR-0012 already made this decision for the family: one SQLite database in WAL
mode through the stdlib `sqlite3` driver, one process, no Postgres, on the grounds that
this is a handful of people with no availability requirement and that a second service to
run, back up, patch and be woken up by is the cost being avoided. That argument is not
repeated here; it applies unchanged.

`storage/database.py` was lifted from keyring rather than rewritten, and its docstring
says what that means: "The reasoning that has not changed is reproduced rather than
referenced, because the next person to edit this file should not have to read another
repository to know why it is shaped this way." This ADR exists for the part that *did*
change -- two pragmas -- and for the reason the two services want different answers.

## What is inherited, briefly

One file, WAL, the stdlib driver. Hand-rolled numbered `.sql` migrations against a
`schema_version` table, because with no ORM and one engine what a migration tool would be
left doing is "run these files in order".

A single-worker `ThreadPoolExecutor` rather than a lock. The obvious design -- `to_thread`
under an `asyncio.Lock` -- is broken, because cancelling the task unwinds the `async with`
and releases the lock without cancelling the thread, leaving `work` mid-transaction on the
shared connection while the next task calls into it from another thread. A client
disconnecting cancels its request task, so that is an ordinary Tuesday. One thread removes
the failure rather than patching it, and every database call is submitted as one whole
callable, which is what makes a transaction indivisible by construction rather than by
convention.

`PRAGMA foreign_keys` read back and verified rather than trusted, because it is
per-connection, defaults to off, and is a silent no-op when a transaction is open. Here a
decorative foreign key would mean a forgotten entry keeping its scope rows and a deleted
user keeping their entries: erasure that reports success and erases nothing. The
connection is opened `isolation_level=None` and issues its own `BEGIN IMMEDIATE` for that
reason, and `require_foreign_keys` refuses to continue if the pragma did not take.

0600 on the file and on its `-wal` and `-shm` sidecars, which hold the same data the file
does, applied after opening because there is nothing to chmod until SQLite has created the
file. Here that mode carries more weight than it did in keyring, which has encryption
behind it: this database has none, so "the mode is therefore not defence in depth, it is
the defence" (ADR-0003).

And backups are `VACUUM INTO`, not `cp`, because a plain copy of a live WAL database can
miss committed transactions. Nothing in this codebase takes a backup; that is the
operator's job, inherited as practice rather than as code, and ADR-0003 is blunt about the
copy being another plaintext database.

## Decision: the two things that differ

**`synchronous = NORMAL` rather than `FULL`.** keyring pays one fsync per commit because a
lost transaction there is a credential somebody believes is saved and is not, at a volume
of a few writes a minute. A lost transaction here is a note somebody believes was written
-- worse than nothing, because the person thinks the assistant knows something it does
not, but recoverable by saying it again. And the write volume is an order of magnitude
higher, because an assistant writes as it learns rather than when a person fills in a
form.

Stated plainly, because it is a durability decision and those get quietly forgotten: WAL
with `NORMAL` **cannot corrupt the database.** What it can do is lose the last few commits
on power loss or an operating-system crash. A clean process exit is not affected. That is
the trade: some small number of the most recent things somebody told the assistant, in
exchange for not fsyncing on every write in a service designed to be written to
constantly.

**`PRAGMA secure_delete = ON`, which keyring does not set.** It overwrites freed pages
rather than merely marking them free, which covers the freelist case: a page released
rather than rewritten keeps its contents until something reuses it.

The honest note goes with it. Across every case constructed for it, it made **no
measurable difference** -- ADR-0005 has the table, and the truncating checkpoint was doing
the work in all of them. It is on because the freelist case it covers is real and because
it costs almost nothing at this write volume, not because it was ever observed to help.
Said plainly here because a reader who finds it in `CONNECT_PRAGMAS` and assumes it is
what makes erasure work would then feel free to remove the checkpoint, which is the step
that does.

There is a third difference, and it is an addition rather than a change:
`Database.checkpoint_truncate` runs `PRAGMA wal_checkpoint(TRUNCATE)`, and it is the step
that actually erases. A `DELETE` takes the row out of the b-tree and leaves the bytes in
the `-wal` file until a checkpoint moves them. ADR-0005 is that decision and has the
measurement.

## What it costs

**Some recent commits are lost on power loss.** That is what `NORMAL` buys and it is worth
restating rather than filing away: a person may have to say something twice. The
mitigation is that they *can*, which is exactly the property a credential does not have.

**A cancelled request's write may still commit.** The queued callable runs to completion
regardless of who is waiting, so a client that disconnected mid-write is not a write that
did not happen. That belongs in the open, and it is the price of serialising on one thread
rather than on a lock cancellation can drop.

**No concurrent writers, and one process.** WAL gives readers concurrency with a writer
and the single connection declines to use it, which is what makes a check-and-write pair
indivisible. Every cap in this service -- entries, fields, pins, events -- is counted and
enforced inside one of those transactions, so "count, then write" cannot go stale. The
purge sweeper is a task in the same process and stops if it dies.

**`busy_timeout = 5000` is doing nothing most of the time.** With one connection there is
nobody to be busy against, except another process that has opened the same file, which is
the case this service does not support.

## What would change our minds

More than one process needing to write. Another replica, a worker outside the API, or a
settings-api sharing this file rather than its own (ADR-0007 says it will have its own).
That is a Postgres adapter, and the ports are what make it an adapter rather than a
rewrite -- the same argument keyring's ADR-0012 makes, and the same one that would have to
be cashed in here.

Nothing about `synchronous` would change with it. If durability ever mattered more than
write throughput here -- because the loss of a note turned out to cost more than saying it
again -- that is a one-word change to `CONNECT_PRAGMAS` and a paragraph in this file, not
a migration.
