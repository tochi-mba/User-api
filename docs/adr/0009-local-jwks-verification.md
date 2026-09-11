# ADR-0009: tokens verified locally against keyring's JWKS

**Status:** accepted.

## Context

keyring mints the tokens this service accepts, and keyring's ADR-0008 already decided
their shape: people get opaque session tokens looked up in a table, and services get
short-lived RS256 JWTs, about fifteen minutes long, verified against a JWKS document
published at `/.well-known/jwks.json`. The reason given there is the one that matters
here: "An opaque token would mean a network call to keyring on every single request
another service serves. Signed means none."

This service is the other service. What is left to decide is not the token format but what
this side does with it, and what it agrees to live without by verifying offline.

## Decision

This service **never calls keyring at request time.** `JwksClient` in `auth/jwks.py`
fetches the JWKS document, caches it by `kid` (an hour, by `jwks_cache_seconds`), and
hands `TokenVerifier` in `auth/tokens.py` a key to check a signature with. Nothing else
crosses the network for a request, in either direction. The only identity this service
ever learns is the token's verified `sub`, an opaque keyring account id, plus the verified
`aud` that `domain/scopes.py` turns into a granted scope (ADR-0004).

## The two consequences we inherit

Both come from keyring's decision rather than this one, and both are terms of the
arrangement rather than problems awaiting a fix. `auth/jwks.py` states them at the top of
the file so nobody has to rediscover them:

**A signed token cannot be revoked.** Somebody who logs out of keyring has ended their
session there and has ended nothing here. A token already minted keeps working until it
expires, which is up to fifteen minutes. The alternative is a blocklist, and a blocklist
is a call to keyring on every request, which is the whole thing signed tokens exist to
avoid.

**Nothing tells us an account is gone.** This service cannot ask keyring anything about an
account: not a display name, not an email address, not whether it still exists. A keyring
account deleted this morning leaves a record here that nobody has mentioned it to, and
noticing would mean polling keyring for absences, which is the per-request call again on a
timer. The levers are therefore local and deliberately so: `delete_user` is the person's
own, and the database file is the operator's.

## The three fetch rules

Each of these is a rule about *when* a fetch may happen, and each exists because the
obvious alternative fails in a specific way.

**Nothing is fetched while starting up.** Constructing `JwksClient` makes an
`httpx.AsyncClient`, which is not a network call; the first fetch happens when the first
token arrives. A service that refused to start unless keyring were reachable turns one
outage into two, at the worst possible moment, because these two services are restarted
together.

**An unknown `kid` is rate limited to one fetch per window** (`jwks_min_refetch_seconds`,
60 by default). A `kid` is read from the token's unverified header *before* anything has
been verified, because it is what chooses the key that would do the verifying -- which
makes it the one value an unauthenticated caller gets to put in front of the verifier.
Without a floor, a stream of tokens carrying invented `kid` values is one outbound request
to keyring per inbound request: an amplifier anyone who can reach this service can aim,
holding no token and no account. The window is claimed *before* the fetch rather than
after it, so a fetch that fails still costs it -- an outage is exactly when a flood of
invented ids must not become a flood of requests to a service already having a bad day.
Only the fetches an unknown id provokes are counted; a fetch because there is no document
yet, or because the one we held went stale, is already bounded by the cache and counting
it would delay a genuine key rotation by a whole window.

**A fetch that fails is a 503; a fetch that succeeds without the `kid` is a 401.** They are
different facts. The first says nothing whatever about the token -- we could not check it,
and the caller should come back rather than start over, which is what the `Retry-After: 5`
on the 503 means. The second is a fact about the token: no key by that name is keyring's.
Conflating them tells a person to log in again over an outage that is not theirs, and
logging in again would not have helped. `KeyringUnreachableError` is therefore the one
exception `TokenVerifier` deliberately does not catch, and it passes through to its own
handler in `api/errors.py`.

The same split governs what counts as unreachable. A proxy's HTML error page served with a
200, a document with no `keys` array, a key set holding nothing usable: all of those are
keyring being unreachable, because none of them says anything about the token in hand. And
the message on the 503 is fixed text rather than the exception's own, because an HTTP
client's error text carries the URL it was given and a URL can carry credentials in its
userinfo.

## Algorithm pinning

`ALGORITHM = "RS256"`, and `jwt.decode` is given that one-element list. This is not a
preference about spelling. Leaving the list open is the classic JWT failure in two
flavours: a caller sends `alg: none` and nothing checks the signature at all, or
downgrades RS256 to HS256 and signs with the public key it fetched from the JWKS endpoint
-- an endpoint published for anybody to fetch, so "only keyring has the key" was never
true of that half of it. There are tests for both.

The same instinct is why the document is parsed into `jwt.PyJWK` objects rather than
through `RSAAlgorithm.from_jwk`, which is one line shorter. `from_jwk` assumes every key
in the document is RSA, which is true of keyring today and is the assumption that breaks
on the morning it publishes a key of another type; and it hands back a bare public key,
where a `PyJWK` binds the algorithm named in the JWK to the key object, so nothing
downstream can be talked into using an RSA public key as an HMAC secret.

## Why PyJWT's clock is switched off

`verify_exp` and `verify_iat` are both `False`, and expiry is re-checked against the
injected clock: `self._clock.now().timestamp() >= float(claims["exp"])`.

Both of PyJWT's checks read the wall clock, which breaks the invariant the rest of the
codebase is built on and makes the expiry rule untestable without waiting a quarter of an
hour for a token to go stale. Switching off `exp` alone is not enough, and that is the
part worth knowing before somebody puts it back: PyJWT also refuses a token whose `iat` is
in the future *by the wall clock*, so a test that pins the injected clock to next Tuesday
would watch every perfectly good token be refused by a rule it never asked for. With both
on, a test can only assert that a token minted now is valid now.

`iat` is not re-checked at all, because PyJWT's only rule for it is "not issued in the
future", which decides nothing `exp` has not decided already. It stays in
`REQUIRED_CLAIMS` alongside `exp`, `iss`, `sub` and `aud`, because PyJWT verifies most
claims only when they are present -- a token that simply omits one is a token that passes
the check for it -- and because a token without an `iat` is not a token keyring minted.

## One refusal

Every rejected token gets the same `AuthenticationError` carrying the same message, "the
token was not accepted": a bad signature, the wrong audience, the wrong issuer, an expired
token, a missing claim, a malformed header, a scope this deployment does not have, a `kid`
that is not keyring's. Two refusals differ only in the request id in the problem body.
Which rule did the refusing goes to the logs, where the operator reads it and a forger
does not.

The constant lives in `auth/jwks.py` rather than beside the verifier because both modules
refuse tokens -- an unknown `kid` is refused before a signature is ever checked -- and two
spellings of "no" are two responses somebody can tell apart while working out which
forgery to try next. The one refusal that is deliberately different is having sent no
token at all, which says "a token is required"; it distinguishes nothing about a token,
because there isn't one.

## What it costs

**Up to fifteen minutes of access after a logout.** Bounded by keyring's TTL and by nothing
this service does. There is no revocation path here to add, short of the blocklist the
design exists to avoid.

**A deleted keyring account keeps its record here.** No display name, no email, no existence
check: this service cannot ask, so a deletion upstream is invisible until a person calls
`DELETE /v1/user` or an operator removes the row. The record is not reachable without a
token, because everything is scoped by the `sub` in one, but it is still on disk.

**A key rotation can cost a genuine caller a 401.** The held document is fresh for an hour;
when keyring rotates, the first token signed with the new `kid` provokes a refetch, and
any other unknown `kid` within the same 60-second window is refused rather than provoking
one of its own. A real token can land in that gap and be told the same thing a forgery is
told. That is the rate limit working as designed, and it is indistinguishable from the
outside, by design.

**The unverified audience is read before anything is verified.** PyJWT will not check an
audience it has not been told, and the only place to learn which one a token claims is the
token. So the claims are read once unverified to get that string, it is handed to `decode`
as the audience to verify, and every decision is taken from the verified copy that comes
back. It is a correct pattern and it is one line away from being a serious bug, which is
why `auth/tokens.py` says so twice.

## What would change our minds

keyring publishing a revocation signal this service could consume without a per-request
call: a short-lived deny list fetched on the same cache schedule as the JWKS, or an event
feed. That would shrink the logout window without reintroducing the dependency, and it
would be a change to what keyring publishes before it was a change here.

An account-deleted notification, for the same reason and through the same door. Polling
for absences is the version of this that is available today, and it is the per-request
call wearing a different hat.

Nothing about the split itself. A per-request check would make every read of a person's
own record depend on a second service being up, which is precisely the failure the
arrangement is built to avoid.
