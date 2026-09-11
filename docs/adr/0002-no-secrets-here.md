# ADR-0002: no credentials in a record; keyring is next door

**Status:** accepted

## Context

Everything about this service is wrong for a secret, and the module that refuses them says
so in one sentence: this is "plaintext on disk, returned in full to any token whose scope
permits it, and indexed for full-text search".

The third is the one that turns a bad idea into a worse one. Every entry is written with a
`search_text` column and a matching row in the `entry_search` FTS5 table -- a second copy
of the content, kept next to the first. An API key written into a note would be a
credential in a search index, and the index is the part nobody thinks to look in when they
go tidying up.

keyring, next door, is built for the opposite of all three: encrypted at rest, never
returned by a read, audited. A person who has both services running has somewhere correct
to put the thing, and the only question is whether this service quietly accepts it anyway.

## Decision

A field value or a note body that looks like a credential is refused with a 422 naming
keyring. Not stripped, not masked, not stored-and-flagged.

`looks_like_a_credential` in `domain/secrets.py` returns the reason it matched or `None`,
and the service layer turns a reason into `CredentialRefusedError`, which the API maps to
422. The reason names the kind of thing -- "this contains what looks like a GitHub
personal access token" -- and never the matched text, because echoing the value into an
error body and a log line would defeat the whole exercise at the moment it succeeded. The
message ends with "store credentials in keyring, not here", because the caller is a model
that will otherwise try the same value again in a different shape.

The check runs on `set_field`, `write_note` and both halves of `revise_entry`, and the
service layer runs it **after** shape validation and **before** the scope check. That
order is deliberate: a caller pasting an API key is told what is actually wrong, rather
than being told it lacks a scope and coming back with the same secret under a different
one.

## What the detector actually looks for

Three exact rules and one heuristic.

- **Known prefixes**: `sk-`, `ghp_`, `gho_`, `ghs_`, `github_pat_`, `xoxb-`, `xoxa-`,
  `xoxp-`, `xoxs-`, `xoxr-`, `AKIA`, `ASIA`, `AIza`. Each is matched case-sensitively,
  only where it starts a token -- at the start of the string or after a non-alphanumeric
  character -- and only when at least eight more credential-shaped characters follow it.
  Both restrictions were added for a sentence that should have been accepted. Case is
  load-bearing on the last two prefixes: `aizawa` is a surname and `AIza` is a Google API
  key. Position is load-bearing on the first: matched as a bare substring, `sk-` refuses
  "They are risk-averse with money", along with every "task-based" and "disk-encrypted"
  sentence anybody will ever write.
- **A private key header**: `-----BEGIN [A-Z ]*PRIVATE KEY-----`, which covers the RSA,
  EC, OPENSSH and unlabelled variants in one pattern.
- **A JWT**: `\beyJ` followed by two more base64url segments. Anchored on `eyJ` -- which
  is `{"` in base64 -- rather than on the bare three-segment shape, because
  `1.2.3-alpha.4` and `en.wikipedia.org` are three dot-separated base64url-legal segments
  and neither is a token.
- **The entropy heuristic**, applied to every run of 32 or more non-space characters.

The heuristic is four conditions and every one must hold. The run has to be drawn entirely
from `A-Za-z0-9+/=_-`; it has to use at least 16 distinct characters; it has to contain
lowercase, uppercase and digits, all three; and its Shannon entropy has to be at least 3.5
bits per character, measured over the run itself rather than assumed from its alphabet.
That last detail is what separates a token from `aB1aB1aB1aB1...`, which is mixed case,
alphabet-legal and entirely predictable.

Three exemptions are checked before any of that, and each was added for a shape that is
long, alphabet-legal and diverse without being a secret: anything URL-shaped (a
`scheme://` prefix, a leading `www.`, or an `@` anywhere in it), a hex colour, and a git
SHA of 7 to 40 lowercase hex characters.

A value that is not a string is rendered through `searchable_text` first, so a credential
buried in a list or an object is caught as readily as one written plainly.

## Why the heuristic is deliberately timid

**There is no override.** That single fact sets every constant above.

A false positive blocks a legitimate memory, and the person on the other end has no way to
insist. There is no force flag, no confirmation round trip, and no "yes, I meant it": the
sentence they wanted written down simply cannot be written down, and the only remedy is
somebody editing the constants in `domain/secrets.py`. A missed credential is the other
kind of mistake -- it is in the wrong place, and the person can still take it out and put
it in keyring. The two errors are not symmetrical, so the tuning is not either.

That is why the thresholds sit where they do. Thirty-two characters, because a
24-character high-entropy run is also sometimes a Lisbon street address with the spaces
taken out, an order reference or a filename. Sixteen distinct characters, because real
tokens use most of their alphabet and padded identifiers do not. All three character
classes, because a long lowercase run is a hyphenated compound or a URL slug and a long
uppercase one is a reference number. And 3.5 bits per character, which is above English
prose with the spaces removed (near 3) and well below base64 (6): the rule lives in that
gap on purpose.

## The specification is a pair of corpora

`tests/unit/domain/test_secrets.py` holds two lists, and they are the specification -- the
constants above are one implementation of it. `MUST_ACCEPT` is sentences somebody would
really write about a person, several chosen because they contain a long, alphabet-legal,
space-free run that a naive entropy rule refuses: a git SHA, a URL, a file path, a very
long word. `MUST_REFUSE` is the credential shapes that must never reach a plaintext,
searchable, fully-readable store.

The asymmetry between them is the tuning argument, written down where it can be run.
`MUST_ACCEPT` is the corpus allowed to grow, and a change to the heuristic that shrinks it
is a regression even if it catches more secrets. A test asserts `MUST_ACCEPT` holds at
least twenty sentences, because a detector can be made to pass three.

## What it costs

**False negatives are certain.** A credential that matches no known prefix and is not
high-entropy goes straight in. A 12-character password, a PIN, a four-word passphrase, a
short API key from a provider not in the list above: none of them trip anything, and all
of them end up in the table and the search index. This is a guard rail, not a boundary,
and nothing downstream should be designed as though a value that got in has been vetted.

**The prefix list ages.** It was written against the providers we had in mind; every
provider that invents a new prefix is a gap until somebody notices and adds it, and
nothing in the service will report the gap.

**Coverage is per field, and two text fields are not covered.** The detector runs on the
field value and the note body. `description` and `source_detail` are not passed through it
-- and `description` is indexed: a field's search text is its key, its description and its
value, and a note's is its description and its body. A credential pasted into a
description is stored and indexed with nothing said about it. That is a real hole rather
than a subtlety, and it is cheap to close.

**A refusal is final and occasionally wrong.** A legitimate 32-character mixed-case
identifier -- a licence key somebody wants remembered, a long opaque reference from
another system -- is refused with no way through. Nobody has measured how often that
happens in practice, which is itself part of the cost: the tuning is argued from asymmetry
rather than from data.

## What would change our minds

Evidence that false positives are common. If the accepted-sentence corpus starts growing
by one-off exemption rather than by pattern -- a new regex per complaint -- that is the
heuristic being the wrong shape, and the answer is probably an override that records the
refusal and the insistence in the event log, rather than a fourth exemption.

Evidence that false negatives matter more than assumed. If somebody finds a credential
sitting in a record, the cheap next step is not a cleverer detector but wider coverage:
`description` and `source_detail` through the same function, which is two lines and closes
the hole named above.

A third service needing the same check. Two copies of this file drifting apart is worse
than a shared one, and the corpora are what would make the move safe.
