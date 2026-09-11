# ADR-0008: Keyword search, not embeddings

**Status:** accepted.

## Context

Recall is half of what this service is for. An assistant asks "what do I know about their
family?" or "did they say anything about Lisbon?", and the answer has to come out of prose
somebody typed months ago, in words they chose rather than the words being searched for.
That is the case semantic search is famously good at, and the case a keyword index is
famously bad at, so the temptation to reach for embeddings is real and should be answered
rather than ignored.

The countervailing fact is what an embedding index costs to *operate*. A model to run or
an API to call on every write and every query, a vector store or an extension to keep
beside the database, an index that has to be rebuilt when the model changes, and a second
copy of the personal data in a system with its own erasure story -- on a service whose
central promise is that a purge reaches the bytes (ADR-0005) and whose data is already in
plaintext twice over (ADR-0003).

## Decision

SQLite FTS5, in the database that is already there. `entry_search` is a plain FTS5 virtual
table tokenised `porter unicode61`, ranked by `bm25()`, and queried through one expression
builder in `domain/search.py`. No new dependency, no model to run, no index to rebuild, no
vector store to operate.

## The measurement

Three things had to be true, and all three were checked rather than assumed. On the stack
in this repository -- CPython 3.11.15 with SQLite 3.45.1 behind the stdlib `sqlite3`
module -- FTS5 is compiled in, so `CREATE VIRTUAL TABLE ... USING fts5` needs no build
flag and no package. Porter stemming works: a search for "preferring" returns a row whose
text says "prefer", which is the thing that makes recall work on prose somebody typed
months ago. And `bm25()` ranks, returning a score that is more negative the better the
match, which is why `_search_ranked` in `entries/sql_store.py` orders by `rank ASC`.

The fourth thing was not true, and it is why `domain/search.py` exists at all. FTS5's
`MATCH` takes a **query language**, not a string: it has `AND`, `OR`, `NOT`, `NEAR`,
phrase quoting, column filters (`col:term`), prefix globs (`term*`) and parentheses. User
input goes straight into it on a read endpoint. Ten plausible search strings were tried
against a real FTS5 table:

    '"'   'a"b'   'x AND OR y'   '*'   'NEAR('   '^'   '()'   'a:b'   ''   '   '

The module records eight of the ten raising `sqlite3.OperationalError`. Re-run today
against a table built the way the migration builds it, all ten raise -- the two the module
does not count are `''` and `'   '`, and neither ever reaches SQLite anyway. Either number
says the same thing: the punctuation people type is a syntax error in a query language,
and a syntax error on a read endpoint is a 500.

## The fix

Stop treating the input as a query. `to_match_query` extracts word tokens with a
unicode-aware regular expression (`[^\W_]+`), double-quotes each one so FTS5 reads it as a
literal phrase rather than as syntax, and joins them with `OR`. A query with no word
tokens at all is refused as `InvalidSearchError`, which the error map turns into a 422,
before any SQL runs.

So `'x AND OR y'` becomes `"x" OR "AND" OR "OR" OR "y"` -- four quoted literals, one of
which happens to spell an operator and none of which operates -- and returns rows. Four of
the ten strings above contain word characters and all four build an expression FTS5
accepts; the other six contain none and are refused as a clean 422. (The module docstring
says seven and three. Counted against the ten strings it lists, the split is four and six;
the behaviour it describes is what the code does.)

Two caps sit alongside: 200 characters, and the first 16 tokens, because more than that is
a paste rather than a query and each token costs an index walk. Over the cap a query is
truncated rather than refused, which keeps the paste working.

Quoting is also what makes this safe rather than merely working. A caller who sends `tea"
OR body:"` is trying to close the quote the builder opened and append an operator of their
own; the tokeniser never emits a quote character, so there is nothing to close, and there
is a test that searches for exactly that string and gets one ordinary row back.

## Why `OR` and not `AND`

`bm25()` does the work. Every token that matches ranks a row above a row where one
matched, so somebody who typed three words gets the entry containing all three at the top
*and* still gets the near-misses underneath it. `AND` would return nothing at all for a
query with one wrong word in it, and a query with one wrong word in it is the normal case
for prose half-remembered from a conversation in March.

## What it costs

**No semantic recall, at all.** "What does she like to drink?" will not find a note that
says "always orders an oat flat white", because no word overlaps. That is the whole class
of query embeddings exist for, and this decision gives it up rather than approximating it.
Stemming closes the gap between "prefer" and "preferring" and nothing closes the gap
between "drink" and "flat white".

**Phrase queries and prefix globs are gone.** `"the boss"` searches for two words rather
than for the phrase, and `tea*` searches for `tea`, because both are quoted into literals.
Nobody can type either, and a caller that needs them is a caller building a query language
on top of this one.

**The corpus is a second copy of the content.** `search_text` on the row and the
`entry_search` row beside it, both plaintext, which is the thing ADR-0002 refuses
credentials over and ADR-0003 is honest about. An embedding index would have been a third.

**The index is shared across accounts.** `MATCH` alone finds other people's rows, so
account isolation lives in the outer `WHERE` of `_search_ranked` rather than in the index,
and there is a test named after exactly that.

## What would change our minds

Enough content per person that keyword recall visibly fails. Nobody has hit that yet --
the cap is 5,000 entries per account -- and the symptom to watch for is a person asking
for something they know they wrote down and not getting it, rather than a benchmark.

A local embedding model cheap enough to run on the same box, with no external call and no
second datastore. At that point it is an **additional** index rather than a replacement,
because `bm25` beats embeddings at the exact recall of a name, a date or a key, and those
are what most searches here actually are. The shape would be two rankings merged at the
service layer, which is a decision with its own costs, and it is not one to take before
the first sentence of this section is true.
