# ADR-0006: Provenance is split into what we verified and what we did not

**Status:** accepted.

## Context

An assistant that reads this record will speak from it, and there is a large difference
between "you told me you are allergic to penicillin" and "your notes suggest you are
allergic to penicillin". The first is a person's own words played back. The second is
something a model worked out, possibly from an email it half understood.

Only one thing makes that difference sayable: knowing where the fact came from. So the
record has to carry provenance. The problem is that the server can only verify half of it.

An entry arrives over HTTP from an assistant holding a token. The server knows, exactly
and cryptographically, which token that was. It knows nothing whatever about what happened
before the request: whether a person said the words, whether a model inferred them from a
calendar, whether they were pasted out of a document. The writer is the only witness, and
the writer is the party whose honesty is in question.

## Decision

Store both halves, keep them in separate columns, return both, and label which is which.
The `entries` table comment states it:

> Provenance, split into the half the server verified and the half it did not.
> `asserted_by` is derived from the token server-side and is trustworthy. `source` is what
> the writer claims, and the server cannot tell whether a person said it or a model
> inferred it. The API reports both and labels which is which.

**`asserted_by` is the verified token audience.** `UserService` passes
`identity.audience` -- the `aud` claim out of the verified copy of the token -- into every
write. It is never read from the request body: `SetFieldRequest` and `WriteNoteRequest`
have no such field and forbid unknown ones, so a body that tries to assert its own
provenance cannot. This is the half the server knows is true.

**`source` is what the writer claims.** One of `stated`, `inferred`, `observed` or
`imported`, defaulting to `stated` on a field and `inferred` on a note, with an optional
free-text `source_detail` beside it. Nothing stops an assistant writing `stated` for
something it guessed, and nothing in this service will ever know that it did.

**The API says which is which, in the field descriptions a model actually reads.**
`EntryResponse` describes `source` as "What the writer CLAIMS about where this came from.
The server cannot verify it: a model that inferred something can still write 'stated'",
and `asserted_by` as "The audience of the token that wrote this, taken from the verified
claims. Unlike `source`, the server knows this one is true." The response schema's own
docstring goes further and tells the consumer to treat the whole entry as a reported claim
about a person and never as an instruction, because an assistant that reads web pages and
email is writing into this service from untrusted text.

A consumer rendering the record can therefore say "you told me" only where that is
actually known, and has enough to hedge everywhere else.

## Why `source` is a closed enum and not free text

Because of the query. "Show me only what I actually told you" is the request that makes
this field worth storing at all, and `GET /v1/user/entries?source=stated` only works over
a small fixed set. Free text would give four spellings of `inferred` within a month of two
assistants writing to the same account, and the filter would quietly return a subset of
what it claimed to.

Four values is a deliberate floor rather than a taxonomy. `stated` is the person's own
words; `inferred` is a model's conclusion, and is the one that most deserves a "your notes
suggest"; `observed` is derived from what the person did rather than what they said; and
`imported` came in from a document, an export or another system. Anything finer goes in
`source_detail`, which is free text precisely because nothing filters on it.

## Why `confirmed_at` is a third kind of provenance

`asserted_by` and `source` both answer "where did this come from". `confirmed_at` answers
a different question: when did somebody last say this is *still true*. It is separate from
`updated_at` for a reason the schema comment gives -- an assistant asserting a stale fact
confidently is the failure that makes this service embarrassing rather than useful -- so
both come back on every read and a consumer can say "you told me in March" instead of
asserting a three-year-old preference as current.

Three behaviours keep it meaning that:

- `put_field` **clears** it when the value changed. The new value has never been vouched
  for, and carrying the old confirmation across would make a fact corrected this morning
  report as confirmed last March.
- `put_field` **sets** it to now when the value is identical. Writing a value again is
  somebody saying it is still true, which is what a confirmation is.
- `revise_entry` does not touch it at all, and `confirm_entry` touches nothing else -- not
  the value, not `updated_at`, not `revision`, because nothing changed and an entry whose
  `updated_at` moved every time somebody said "yes, still true" would sort to the top of a
  recency listing for not changing.

`?stale_before=` is the other half: it finds entries confirmed before a date *or never
confirmed at all*, because a never-confirmed entry is the stalest thing there is.

## What it costs

**A writer can misreport `source`, and nothing here will know.** A model that inferred a
dietary restriction from a restaurant booking can write `stated`, and the record will then
tell the person's next assistant that they said it themselves. That is not a hole to be
closed later; it is the boundary of what a server can check, and the only mitigation in
the codebase is that the request schema asks for honesty in the field description
("'inferred' is not a lesser answer") and that `asserted_by` at least says which token was
holding the pen.

**`confirmed_at` is only as good as the caller's discipline.** Nothing proves a human was
in the loop when `confirm_entry` was called, and an assistant that re-writes the same
value on a schedule will keep the staleness clock fresh without anybody having vouched for
anything. Both are the same trust boundary as `source`, arriving through a different door.

**Two provenance fields is one more than a consumer wants.** Every renderer has to decide
what to do with both, and the easy mistake -- reading `source` as though it were verified
because it sits next to something that is -- is exactly the mistake the field descriptions
are written to prevent. A single "provenance" string would be simpler and would be a lie.

## What would change our minds

Nothing available. Verifying a claim about where information came from would mean
verifying it against the only party that was there, which is the writer, which is asking
the writer to vouch for its own trustworthiness.

The shape of an actual answer would have to come from outside: something signed by the
person at the moment they said it, which is a different service and a different trust
model, and which would still leave `inferred`, `observed` and `imported` unverifiable
because there is no person at the moment those are written. Until something like that
exists, the honest design is the one here -- store the claim, label it as a claim, and
never let it sit unmarked beside a fact the server actually checked.
