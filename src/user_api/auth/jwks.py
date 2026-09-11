"""Keyring's public keys, cached, and the rules about when we go and get them again.

A token is verified here with no help from keyring beyond a document of public keys that
anybody may fetch. Two consequences come with that. Both are inherited from keyring's
ADR-0008 rather than chosen here, and both are terms of the arrangement rather than
problems to be designed around:

**A signed token cannot be revoked.** Somebody who logs out of keyring has ended their
session there and has ended nothing here: a token already minted keeps working until it
expires, which is up to fifteen minutes. The alternative is a blocklist, and a blocklist
is a call to keyring on every request, which is the whole thing signed tokens exist to
avoid.

**Nothing tells us an account is gone.** This service never asks keyring about an account,
so a keyring account deleted this morning leaves a record here that nobody has mentioned
it to. The levers are therefore local, and both are deliberate: ``delete_user`` is the
person's own and the database file is the operator's. Noticing an upstream deletion would
mean polling keyring for absences, which is the per-request call again on a timer.

Three things about the fetching itself, each of which somebody has learned the hard way:

**Nothing is fetched while starting up.** Constructing this does no network work at all,
and the first fetch happens when the first token arrives. A service that refuses to start
unless keyring is reachable turns one outage into two, at the worst possible moment --
these two services are restarted together.

**An unknown key id is rate limited.** Without a floor between fetches, a stream of tokens
carrying invented ``kid`` values is one outbound request per inbound request: an amplifier
pointed at keyring, aimed by anybody who can reach this service, holding no token and no
account. A key id is read before anything has been verified, which is what makes it the
one value an unauthenticated caller gets to put in front of the verifier.

**"Keyring is down" and "that token is not ours" are different answers.** A fetch that
fails is :class:`~user_api.domain.errors.KeyringUnreachableError` and becomes a 503 with a
Retry-After. A fetch that succeeds and comes back without the key id is
:class:`~user_api.domain.errors.AuthenticationError` and becomes a 401. Conflating them
tells a person to log in again because keyring was briefly down, and logging in again
would not have helped.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import jwt

from user_api.core.logging import get_logger
from user_api.domain.errors import AuthenticationError, KeyringUnreachableError

if TYPE_CHECKING:
    from user_api.core.clock import Clock

logger = get_logger(__name__)

BAD_TOKEN = "the token was not accepted"  # noqa: S105 -- a message, not a credential
"""The one thing every refusal in this package says, whichever rule did the refusing.

It lives here rather than with the verifier because both modules refuse tokens -- an
unknown key id is refused before a signature is ever checked -- and two spellings of "no"
are two responses somebody can tell apart while working out which forgery to try next.
"""

KEYS_UNAVAILABLE = "keyring's signing keys could not be fetched"
"""Said when we cannot verify anything, as opposed to when a token is wrong.

Also the reason :meth:`JwksClient.healthy` reports, and fixed text rather than an
exception's message on purpose: the text an HTTP client raises carries the URL it was
given, and a URL can carry credentials in its userinfo.
"""


class JwksClient:
    """Keyring's signing keys, fetched when one is wanted and cached by ``kid``."""

    def __init__(
        self,
        *,
        url: str,
        clock: Clock,
        cache_seconds: float,
        min_refetch_seconds: float,
        timeout_seconds: float,
    ) -> None:
        self._url = url
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._min_refetch_seconds = min_refetch_seconds
        # Made once so connections are pooled across requests. Constructing a client is
        # not a network call, which is what keeps startup independent of keyring.
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at = 0.0
        self._provoked_at: float | None = None
        self._lock = asyncio.Lock()

    async def key_for(self, kid: str) -> Any:
        """The key keyring signed with, for the ``kid`` a token names.

        Typed as ``Any`` rather than as :class:`jwt.PyJWK` deliberately. An import-linter
        contract keeps PyJWT inside this package, and a signature naming one of its types
        is exactly how that kind of dependency leaks out of the package meant to contain
        it.

        Raises:
            AuthenticationError: no key by that id, either because the document we hold
                does not name it and we fetched too recently to look again, or because a
                fetch just now came back without it.
            KeyringUnreachableError: the document could not be fetched at all, so whether
                the token is good is not something we know.
        """
        cached = self._cached(kid)
        if cached is not None:
            return cached

        async with self._lock:
            # Asked again inside the lock. Ten requests arriving together on a cold cache
            # all queue here, and the nine that waited want the answer the first one
            # fetched rather than a fetch of their own.
            cached = self._cached(kid)
            if cached is not None:
                return cached
            if self._fresh_keys() is not None:
                # What we hold is current and does not name this kid, so the only reason
                # to look again is that keyring may have rotated its key since. That is
                # also the path a flood of invented ids would ride, hence the window.
                self._claim_refetch_window(kid)
            fetched = await self._fetch()

        key = _key_in(fetched, kid)
        if key is None:
            # A fetch that worked and came back without the id is a fact about the token:
            # no key by that name is keyring's. That is a 401 and not a 503.
            logger.info("jwks_kid_unknown", kid=kid)
            raise AuthenticationError(BAD_TOKEN)
        return key

    async def healthy(self) -> tuple[bool, str | None]:
        """Whether a token could be verified right now, and why not if it could not.

        Fetches when the cache is cold, because a check that only reported on the cache
        would have nothing to say on a fresh process -- which is the moment an operator
        most wants to know whether keyring is reachable.

        Reports rather than raises, and that is why the ``except`` below is as wide as it
        is. A health check that raised would answer 500 while trying to tell somebody what
        is wrong, and the thing polling it cannot read a traceback anyway -- so a load
        balancer would see the same 500 for "keyring is down" as for "this process is
        broken", which are different things needing different people.

        The wide catch is deliberate and is confined to this method. Everywhere else an
        unexpected exception should propagate and become a 500 with a request id; here the
        unexpected exception IS the thing being reported.
        """
        if self._fresh_keys() is not None:
            return True, None

        async with self._lock:
            try:
                await self._fetch()
            except KeyringUnreachableError:
                return False, KEYS_UNAVAILABLE
            except Exception:
                logger.exception("jwks_health_check_failed")
                return False, KEYS_UNAVAILABLE
        return True, None

    async def aclose(self) -> None:
        """Release the connection pool. Nothing may be fetched afterwards."""
        await self._client.aclose()

    def _cached(self, kid: str) -> jwt.PyJWK | None:
        """The key for ``kid`` out of the document we hold, if it is young enough to use."""
        held = self._fresh_keys()
        if held is None:
            return None
        return _key_in(held, kid)

    def _fresh_keys(self) -> jwt.PyJWKSet | None:
        """The document we hold, or ``None`` if there is none or it has gone stale.

        Age is measured on the injected clock rather than on :func:`time.monotonic`, for
        the reason in :mod:`user_api.core.clock`: a cache whose expiry can only be
        exercised by waiting an hour is a cache whose expiry is not tested.
        """
        if self._keys is None:
            return None
        if self._clock.monotonic() - self._fetched_at >= self._cache_seconds:
            return None
        return self._keys

    def _claim_refetch_window(self, kid: str) -> None:
        """Take the one fetch a window allows an unknown key id, or refuse it.

        The window is the whole defence described in the module docstring, so a suppressed
        fetch is skipped entirely rather than merely done less often. The caller is told
        what a fetch would almost certainly have told it anyway: we looked recently and
        keyring had no key by that name.

        Only the fetches an unknown id provokes are counted here. A fetch because there is
        no document yet, or because the one we had went stale, is already bounded by
        ``cache_seconds``, and counting those would mean a key rotation could not be
        picked up until a whole window after the last ordinary fetch.

        Raises:
            AuthenticationError: within ``min_refetch_seconds`` of the last such fetch.
        """
        provoked_at = self._provoked_at
        too_soon = (
            provoked_at is not None
            and self._clock.monotonic() - provoked_at < self._min_refetch_seconds
        )
        if too_soon:
            logger.warning("jwks_refetch_suppressed", kid=kid)
            raise AuthenticationError(BAD_TOKEN)

        # Spent before the fetch rather than after it, so a fetch that fails still costs
        # the window. An outage is precisely when a flood of invented ids must not become
        # a flood of requests to a service that is already having a bad day.
        self._provoked_at = self._clock.monotonic()

    async def _fetch(self) -> jwt.PyJWKSet:
        """Fetch the document, replace whatever we held, and hand back the new keys.

        The age is taken before the request goes out rather than after it comes back, so
        the cache expires by when the document was asked for. A slow fetch is a document
        that was already a little old when it arrived.

        Raises:
            KeyringUnreachableError: the document could not be fetched, was not JSON, or
                was not a usable JWKS.
        """
        at = self._clock.monotonic()
        key_set = _key_set_of(await self._get())
        self._keys = key_set
        self._fetched_at = at
        logger.info("jwks_fetched", key_count=len(key_set.keys))
        return key_set

    async def _get(self) -> object:
        """The document at the JWKS URL, as parsed JSON."""
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            document: object = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Only the type of the failure is recorded. Its text carries the URL, and a
            # URL can carry credentials in its userinfo -- which is how a password ends
            # up in a log line nobody meant to write one to.
            logger.warning("jwks_fetch_failed", error=type(exc).__name__)
            raise KeyringUnreachableError(KEYS_UNAVAILABLE) from exc
        return document


def _key_set_of(document: object) -> jwt.PyJWKSet:
    """Turn a fetched document into a key set, refusing anything that is not one.

    Everything refused here is keyring being unreachable rather than the caller being
    wrong: a proxy's HTML error page served with a 200, a document with no ``keys`` array,
    a set holding nothing we could verify with. None of those say anything at all about
    the token in hand.
    """
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list):
        logger.warning("jwks_document_malformed")
        raise KeyringUnreachableError(KEYS_UNAVAILABLE)

    # PyJWK rather than RSAAlgorithm.from_jwk, which would also work and is one line
    # shorter. Two reasons, and the second is the one that matters: from_jwk assumes every
    # key in the document is RSA, which is true of keyring today and is the assumption
    # that breaks on the morning it publishes a second key of another type; and it hands
    # back a bare public key, where a PyJWK binds the algorithm named in the JWK to the
    # key object, so nothing downstream can be talked into using an RSA public key as an
    # HMAC secret. The set builds those, and drops the individual keys it cannot use
    # rather than refusing a document for one entry it did not recognise.
    try:
        key_set = jwt.PyJWKSet(keys)
    except (jwt.PyJWTError, AttributeError, ValueError) as exc:
        # The two exception types that are not PyJWT's are here because PyJWKSet assumes
        # every entry is a mapping of JWK parameters, and an entry that is not fails
        # further down than its own error handling reaches.
        logger.warning("jwks_document_unusable", error=type(exc).__name__)
        raise KeyringUnreachableError(KEYS_UNAVAILABLE) from exc
    return key_set


def _key_in(key_set: jwt.PyJWKSet, kid: str) -> jwt.PyJWK | None:
    """The key with this id, or ``None``.

    PyJWKSet raises :class:`KeyError` for an id it does not hold. A key that is not there
    is an ordinary answer here -- most of this module is about what to do next -- so it is
    a value rather than an exception.
    """
    try:
        key = key_set[kid]
    except KeyError:
        return None
    return key
