# ADR-0007: settings behind a port on day one

**Status:** accepted.

## Context

There are three settings. What `DELETE` means (`erasure_mode`), how long a forgotten entry
stays recoverable (`grace_days`), and whether the event log keeps the old value
(`log_values`). They are one row of `user_settings` keyed by account: an enum, an integer
and a flag. At that size, an interface in front of them looks like ceremony.

The reason it is not ceremony has nothing to do with today, and `users/settings.py` opens
with it:

> This port exists on day one for a reason that has nothing to do with today: **a separate
> settings-api is the next service in this family, and it becomes a second adapter.** When
> it does, nothing above this line changes -- not the service, not the routers, not a
> single test that is written against the port rather than against the table.

## Decision

`SettingsStore` is a `Protocol` with two methods, `get` and `update`, and both traffic in
`UserSettings` -- the domain dataclass from `domain/settings.py` -- rather than in rows.
`get` never returns `None`: an account that has expressed no preference has the default
preference, because a caller forced to handle `None` is a caller with a branch in which
the erasure mode is undefined, and that is the one branch where forgetting to decide means
quietly keeping the data.

`SqlSettingsStore` in `users/sql_settings.py` is the adapter, and it is deliberately the
dull one. A `SELECT`; an upsert whose `DO UPDATE` arm `COALESCE`s each column against
itself, so a caller changing one setting cannot overwrite the other two with the nulls it
passed for them; and a read-back inside the same transaction, so what the caller is told is
what is stored rather than what was proposed. It does not create the `users` row it points
at -- `user_settings.account_id` is a foreign key, the service calls `UserStore.ensure`
first, and a store that created the thing it points at is a store that can resurrect an
account `DELETE /v1/user` has just erased.

The choice of adapter is made in one place. `core/container.py` constructs
`SqlSettingsStore` and hands it to `UserService`, which holds the port type; the routers
hold the service. The composition root says what that buys: "When the settings-api lands,
one line in this file changes and nothing above the port notices."

## Why now rather than when the second adapter exists

Because the alternative is well understood. Settings that begin as three columns on a user
table are still three columns on a user table when four services need them, and moving them
at that point is a migration, a backfill, a dual-write window and a rollback plan, executed
against data somebody is relying on. A port is the cheap moment to make that decision, and
the cheap moment is before there is anything to move.

The thing being bought is not abstraction for its own sake. It is that the service, the
routers and the tests are written against a contract rather than against a table, so the
second adapter inherits the test suite instead of needing a new one -- the same argument
keyring's ADR-0012 cashed in when its in-memory stores became SQLite ones.

## The defect the port surfaced

Writing the contract down is what found this, so it belongs in the record rather than in a
commit message.

Both methods take `default_grace_days`, and the reason `get` takes it is obvious: there may
be no row, and the window a rowless account is living under is the deployment's, not the
domain's. The reason `update` takes it is the one that was missed. An update may be the
call that *creates* the row, and its `INSERT` arm has to supply all three columns, including
the ones the caller said nothing about. Falling back to `UserSettings`' own defaults there
looks harmless for exactly as long as the two numbers agree.

They do not have to agree. `default_grace_days` is deployment configuration; `UserSettings`
defaults to thirty days. On a deployment configured for a seven-day window, an account whose
first ever settings change was `log_values` would come away with a thirty-day grace period
it never asked for, and nothing anywhere would report it: the row would look deliberate, and
the only way to notice would be to compare the row against the configuration by eye.

So both methods take it, and `sql_settings.py` says why in the place somebody will read it:
"An untouched setting keeps the value the account already had, and for a row that does not
exist yet that value is the deployment's, not the domain's."

The general form is the second half of the port docstring's rule -- the store is handed
configuration rather than reading it, "because a store that read configuration would be a
store that needs configuration injected to be tested".

## What it costs

One file and one indirection, for three values, today. `users/settings.py` is a Protocol
nobody calls, describing two methods with exactly one implementation, and every read of a
setting goes through a type whose only current job is to name what `SqlSettingsStore`
already does.

Two parameters on the port exist only because it is a port. `default_grace_days` on both
methods and `now` on `update` are configuration and the clock, passed down from the service
because the store is not allowed to reach for either. That is three arguments of ceremony
on a call that writes one flag.

If the settings-api never lands, that is the whole cost and there is no return on it.

## What would change our minds

The settings-api being cancelled, or a decision that settings stay here permanently. The
port would then be one file of indirection that never got cashed in, and collapsing it into
the service would be a small, safe change -- which is itself the argument for having taken
the risk in this direction rather than the other.

A setting that turns out to be per-deployment rather than per-account. That is
configuration, it belongs in `core/config.py`, and putting it behind this port would be
giving an operator's decision an account's shape.
