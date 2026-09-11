# ADR-0004: Scope comes from the token audience, not a query parameter

**Status:** accepted.

## Context

A person's record is not one undifferentiated pile. The assistant that plans the week may
have no business with the health entries, and the one that is helping with a diagnosis has
no need of the work ones. Compartments are the only mechanism this service has for saying
so, and a compartment is worth exactly as much as whatever decides which one a caller is
in.

`user_api.domain.scopes` opens with the argument, and it is the whole of it:

> A caller that declares its own scope in a query parameter is asking politely. That is a
> filter, not a boundary: anything that can send `?scope=home` can send `?scope=health`,
> and a service that trusted it would be compartmentalising nothing.

So the question this ADR settles is not whether there is a `?scope=` parameter. There is
one, and it is useful. The question is where the boundary comes from, given that the
parameter cannot be it.

## Decision

The scope comes from the token's `aud` claim, taken from the verified claims that come
back out of `jwt.decode` and from nowhere else. keyring signs that claim, and keyring will
only mint a token against a session -- which is a thing the person has and an assistant
does not. The person therefore decides, at mint time, what a given assistant may ever see,
and no request the assistant sends afterwards changes the answer.

The audience is a family. The table is `granted_scope()` in `domain/scopes.py`, reproduced
from its docstring:

    aud              granted scope    can read
    ---------------  --------------   ------------------------------------------
    user             (none)           unscoped entries only
    user.home        home             unscoped entries, and entries tagged home
    user.health      health           unscoped entries, and entries tagged health

The bare prefix grants nothing beyond entries that carry no scope at all. `None` is not
"everything", and `Identity.granted_scope` says so where somebody reading the type would
otherwise assume the opposite. Which scopes exist at all is deployment configuration --
`allowed_scopes` ships as `home, work, health, family, finance` -- and a startup validator
refuses a scope name containing a dot, because an audience is `{prefix}.{scope}` and a
dotted scope would make one audience parse as another.

Enforcement is one predicate. `_VISIBLE` in `entries/sql_store.py` binds exactly one
parameter, the caller's granted scope: an entry with no scope rows is visible to
everybody, an entry with scope rows is visible only to a token granting one of them. It is
applied to writes addressed by id as well as to reads, so a `user.home` token cannot
revise, confirm or forget a health-scoped entry it could not have read. When the caller
holds no scope the parameter binds `NULL`, and `scope = NULL` is never true, so the
predicate collapses to "unscoped only" without a second query shape. `Entry.visible_to` is
the same rule in Python, present because a boundary that exists only as a WHERE clause is
a boundary nobody can read.

Writes are bounded by `check_writable`: a token may write an entry carrying no scope, or
one tagged with the single scope it holds, and nothing else. Writing *up* is refused, with
`ScopeNotGrantedError` and a 403 naming the scopes that were refused. A `user.home` token
creating a `health` entry would be creating data it cannot read back, cannot revise and
cannot verify it wrote correctly, on the strength of a token the person minted for the
home assistant. There is no case where that is what somebody meant.

## Why one scope per token and not a set

A token granting two scopes is a token whose holder can correlate across two compartments,
and correlation is most of what compartmentalising was for. Knowing the household roster
and knowing the diagnosis are two facts; holding both is a third thing, and it is the
thing the person was trying not to hand out.

Nothing is lost by refusing it, because the person minting the token can mint two if they
mean two. What they cannot then do is hand one credential to one process that sees both at
once, which is the point.

## Why an unknown scope is refused outright rather than granting nothing

`granted_scope` raises `InvalidScopeError` for an audience naming a scope this deployment
does not recognise, and `TokenVerifier` turns that into the same undifferentiated 401 as
every other refusal. The tempting alternative is to treat an unrecognised scope as
granting nothing, on the grounds that granting nothing is safe.

It is not safe, it is quiet. A typo in a mint request would produce a token that works,
reads only unscoped entries, and looks exactly like a correctly configured assistant that
simply has not been told anything yet. Somebody would spend an afternoon on that, and the
failure would not surface until they compared the audience string by eye.

## Why the `?scope=` filter refuses instead of returning an empty page

The parameter narrows. A `user.health` token asking for `?scope=health` gets the entries
actually tagged `health` and not the unscoped ones alongside them, which is a real and
useful query: "only the health entries, please". `check_filterable` permits exactly that
and refuses anything else with a 403.

Quietly returning nothing was the tempting implementation, and it is worse. A caller that
asked for something it may not have and got an empty page cannot tell that from "there is
nothing there", so it caches the emptiness and stops asking. A 403 is a fact about the
caller's own token rather than about what exists, which is why it is safe to be specific
here when a missing entry is deliberately a 404 (`api/errors.py` holds both mappings and
the reasoning for each).

## The awkward case: a 409 that admits a key exists

A `user.home` token does `PUT /v1/user/fields/blood_type`. A live field with that key
already exists, tagged `health`, and this token cannot see it. `put_field` raises
`ScopeConflictError`, which is a 409 saying the key is taken and the entry was not
changed.

That leaks something. Not the value, not the scope it sits in, not who wrote it -- but the
existence of a key, to a caller that was not supposed to know the compartment exists. It
is the one place in this service where concealment is traded for a caller that can act on
the answer, and it is worth being plain about why the other two options are worse.

**Overwrite it silently.** A token gets to clobber a value it cannot read, cannot compare
against and cannot restore. The health assistant's blood type is replaced by the home
assistant's guess, nobody is told, and the compartment boundary has been crossed in the
one direction that destroys data rather than merely revealing it.

**Report success and write nothing.** This is a lie with a long tail: the caller believes
a fact about the person is now stored, will read it back later and find its own value
absent, and cannot distinguish that from a bug in this service. A write path that reports
success for a write that did not happen makes every other promise here unfalsifiable.

**Return 404.** Superficially the most consistent answer, since 404 is what an
out-of-scope entry gets on a read. But this is a `PUT` on a key, not a read of an entry,
and a `PUT` that returns 404 for a key that would have been created a moment ago is a
caller that retries forever: it has been told the thing it is trying to create does not
exist, which is precisely the condition under which creating it is the right move.

So the 409 stands, and the API says what it means: the key is taken, out of your scope,
and nothing was changed. A caller that receives it can pick a different key, or tell the
person that this one is already spoken for by an assistant they gave a different token to.

## Why scopes live in a junction table and not a JSON column

`entry_scopes` is `(entry_id, scope)`, `WITHOUT ROWID`, with a secondary index on
`(scope, entry_id)`. The schema comment gives the reason: a JSON column means a
virtual-table scan and a `json_each()` per row on the hottest read in the service, where a
junction table with a covering index is an index seek.

"Hottest read" is not rhetorical. `_VISIBLE` is on every list, every search, every counts
query, the always-load block, and every write addressed by id -- there is no read path in
this service that does not carry it. The primary key *is* the table under `WITHOUT ROWID`,
so both halves of the predicate seek straight into it, and the secondary index serves the
other direction for `?scope=`. A JSON column would have made the boundary check the most
expensive part of every request in the service, to save one table.

What was actually measured here was the *other* half of the schema: the migration records
that the partial indexes serving ORDER BY as well as WHERE, with no temp B-tree, was
confirmed with `EXPLAIN QUERY PLAN` over 4,000 rows. The junction-versus-JSON choice was
made on the plan shape rather than on a stopwatch, and was made before there was a corpus
to put a stopwatch to.

## What it costs

**The person mints a token per compartment.** An assistant that should see home and health
needs two tokens, and something on the assistant's side has to hold both and know which to
send. That is friction, and it falls on the person, who has to go through keyring for each
one.

**A signed token cannot be narrowed after the fact.** Nothing in this service consults
keyring at request time -- it fetches public keys and nothing else -- so there is no
revocation list and no downgrade path. A `user.health` token that turns out to have been
given to the wrong assistant stays a `user.health` token until it expires. Narrowing means
minting a different one and waiting the old one out.

**The 409 above.** One bit of information, to one caller, about one key.

**Scope changes on an entry are a write.** An entry's compartment is rows in
`entry_scopes`, replaced wholesale when a write supplies scopes, and a token can only
widen as far as the scope it holds. Re-compartmentalising an existing record is therefore
something the person does through an assistant that already holds the target scope, not
something an administrator does in bulk. There is no administrative surface here at all.

## What would change our minds

An assistant that legitimately spans two compartments -- meal planning that genuinely has
to see the allergies recorded under health, say. Even then the first answer is two tokens
and a caller that holds both, because that keeps the correlation in the caller rather than
in something this service signed. A two-scope token would only become the answer if
holding two turned out to be unworkable in practice, and it would be a change to what
keyring mints before it was a change here.
