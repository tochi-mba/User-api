# ADR-0005: Erasure is a setting, and what purge actually does

**Status:** accepted.

## The measurement

This ADR starts with the measurement because the code looks wrong without it. A row was
deleted from a page that still held live neighbours, with the value already checkpointed
into the main database file, and both files were then scanned for the bytes:

| after `DELETE`, then...           | still in `.db` | still in `-wal`     |
| --------------------------------- | -------------- | ------------------- |
| nothing                           | yes            | (already flushed)   |
| `PRAGMA wal_checkpoint(FULL)`     | no             | yes                 |
| `PRAGMA wal_checkpoint(TRUNCATE)` | no             | no                  |
| `VACUUM` + truncate               | no             | no                  |

Read down the table in the order the bytes move. `DELETE` takes the row out of the b-tree
and leaves what it held where it was. The checkpoint copies the rewritten page back over
the main file, which is what clears the `.db` column -- and leaves the page it copied
*from* sitting in the log, which is why `FULL` is not enough. Only `TRUNCATE` empties the
log afterwards.

A purge that stops at the `DELETE`, or at a `FULL` checkpoint, reports success while the
forgotten value is still on disk in a sidecar next to the database, findable with `grep`.

## Context

Everything in this service is about a person, so "delete" is not a storage operation with
a convenient meaning. It is a promise made to somebody about their own data, usually in
the middle of a conversation, and it has to be true at the level of bytes on a disk rather
than rows in a b-tree. The database also holds no encryption at all (ADR-0003), so there
is no second line of defence behind the delete: whatever survives it is readable by
anything that can read the file.

Two separate questions follow. What does a purge have to do to be a purge, which the table
above answers. And *when* does one happen, which is not a question this service should be
answering on the person's behalf.

## Decision

**The recipe is `DELETE`, then `PRAGMA wal_checkpoint(TRUNCATE)`.** In `users/erasure.py`
the first step is one transaction that removes the entry, its scope rows (by cascade), its
search-index row, and the values logged in the events *about* that entry. The checkpoint
is the second step, outside the transaction, because a checkpoint cannot run with one
open. It runs once per sweep rather than once per row: that is what keeps a cheap step
cheap, and the `immediate` path pays for its own checkpoint because that is the price of
the promise that mode makes.

Both halves of the first step are in one transaction on purpose. Between two, there is a
moment where the entry is gone and its old value is still sitting in the event that
recorded the change.

**`PRAGMA secure_delete = ON`** is set on the connection, as defence in depth for the
freelist case.

**Erasure is a per-account setting with three modes,** in `domain/settings.py`:

- `grace` (the default, 30 days) marks the entry forgotten now and destroys it later. It
  is invisible from every read path the moment it is marked, so from an assistant's point
  of view it is already gone; the bytes go on the first sweep after the grace period.
- `immediate` destroys it inside the request, before the response goes out. No recovery.
- `tombstone` marks it forgotten and never purges, for people who would rather keep the
  record of what they changed their mind about.

All three answer `DELETE /v1/user/entries/{id}` identically, so an assistant does not need
to know which one it is talking to. Changing the setting is never retroactive in either
direction.

**`DELETE /v1/user` is always a hard purge,** whatever the mode says. **`log_values`
defaults to off.** **An event survives the entry it describes.** Each of those has its own
section below.

## Why not `VACUUM`

It works. The bottom row of the table says so. It also rewrites the entire database under
a write lock to achieve exactly what a truncating checkpoint achieves by emptying a file,
and it would have to run after every purge on a service where purges are routine. The
checkpoint is the cheap operation that does the whole job; `VACUUM` is the expensive one
that does the same job.

## Why `secure_delete` is on, and what it actually bought

Nothing measurable. That is the honest answer: across every case constructed for it, the
pragma made **no measurable difference** -- the truncating checkpoint was doing the work
in all of them. It is on because the freelist case it covers is real (a page freed rather
than rewritten keeps its contents until something reuses it) and because it costs almost
nothing at this write volume, not because it was ever observed to help.

Stated plainly here because a reader who finds it in `CONNECT_PRAGMAS` and assumes it is
what makes erasure work would then be free to remove the checkpoint, which is the step
that does.

## Why `grace` is the default

The two failure modes are not symmetric.

"It came back after I deleted it" is a broken promise. There is nothing to say afterwards
that repairs it, and it is the failure that would make a person stop telling this service
anything.

"I deleted it by mistake and had a month to say so" is a recovery. It is an inconvenience
with a remedy, and `?include_forgotten=true` is the remedy: a person can review what they
asked to have forgotten and undo it.

A person telling an assistant to forget something is often mid-conversation and sometimes
wrong. Thirty days is long enough to notice and short enough that "deleted" still means
something. Somebody for whom the window is itself the problem sets `immediate`, and that
mode exists precisely so the default does not have to be defensive.

## Why `DELETE /v1/user` ignores the mode

The erasure mode governs what forgetting *one entry* means. "Delete everything you know
about me" has one honest reading, and a tombstone is not it. So `delete_user` destroys
every entry, scope row, search-index row, event and setting for the account and then
truncates the log, regardless of what the account had chosen, and returns counts of what
went rather than any of the contents.

The mode is a preference about how this service handles an ordinary correction. It is not
a standing instruction that can override the person asking for all of it to be gone.

## Why the event log does not keep values by default

The event log is a **second copy of the personal data.** "Changed diagnosis from X to Y"
is itself the sensitive fact, and it does not stop being sensitive because it is phrased
as a change. So `log_values` defaults to off: events record what changed, when, and which
token did it, and never to what. The `detail` column stays `NULL`.

Turned on, the old and new values are kept and are stripped in the same transaction that
purges the entry they belong to, so the promise still holds -- it is just doing more work,
and there is more of the person's data in more places while it does.

The setting is read per write rather than cached, because somebody turning it off is doing
so for a reason and the next write is the one they mean.

## Why an event outlives the entry it describes

The `events` table has no foreign keys, deliberately. Deleting an entry must not delete
the record that it was deleted: the row keeps its metadata, so "you forgot something on
the 3rd" survives the thing that was forgotten. An `entry_id` in the log may therefore
name something that no longer exists, which is the point rather than a dangling reference.

This is the person's own history of their own record, and the one question it exists to
answer -- "what did you change, and when did I tell you that?" -- is unanswerable if the
log disappears alongside what it describes.

## What this cannot reach

**Backups taken before a purge still contain the data, and this service has no way to
reach into them.** Restoring an old backup resurrects forgotten entries, including
entries destroyed under `immediate` mode and entries destroyed by `DELETE /v1/user`.

Nothing in this codebase can fix that, and nothing in it pretends to. It is a property of
having backups at all, and the operator is the only person who can do anything about it:
by keeping the retention window short enough to be honest about, by knowing that a restore
undoes erasures made since the backup, and by saying so to the people whose data it is.
The API documentation for `DELETE /v1/user` says the same thing in the place a caller will
read it.

## What it costs

**A grace period means the data is still there.** For thirty days by default, a forgotten
entry is invisible and recoverable, which means it is on disk and would be in a backup.
That is the trade `grace` makes, and it is why `immediate` exists.

**"Now" means "on the next sweep".** The sweeper runs hourly by default
(`purge_interval_seconds`), and it sleeps before its first pass. A grace of zero days
therefore does not mean "immediately" -- it means the cutoff is now, so the entry goes on
the next sweep, up to an hour later. Somebody who means *now* wants `immediate` mode,
which does not wait for a sweep at all.

**A sweep is bounded.** 500 entries per account per pass, so one account with a large
backlog cannot hold the single database thread for an unbounded stretch. The remainder
waits for the next hour.

**`tombstone` means delete never destroys anything.** That is what the person asked for,
and it is the mode most worth warning about, because an assistant offering to delete
something on a tombstoned account is offering something narrower than it sounds.

**A settings change does not reach backwards.** Switching to `immediate` does not purge
what is already waiting out a grace period, and switching away from `tombstone` does not
schedule what is already tombstoned. A settings change that silently destroyed data would
be the worst surprise this service could produce, so it does not.

**The sweeper is one task in one process.** If it dies, erasure stops; a failure is caught
and logged and the next tick tries again, which covers a transient error and does not
cover the process not running.

## What would change our minds

A per-entry retention setting, if anybody ever asks for one. "Forget this in a week, and
keep that until I say otherwise" is a coherent thing to want, and the shape of it is
clear: a column beside `forgotten_at` and a cutoff computed per row rather than per
account. It is not built because nobody has asked, and because a per-account setting is
one decision a person makes once rather than a decision an assistant has to get right on
every write.
