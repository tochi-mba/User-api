"""Keyring's public keys, and the rules about when we go and get them again.

Three of the properties here are bounds on how often this service talks to keyring rather
than statements about what a token means. A warm cache does not fetch again; ten requests
arriving together on a cold one make a single fetch between them; and an unrecognised key
id -- the one value an unauthenticated caller gets to put in front of the verifier -- may
provoke at most one fetch per window. Each of those is counted rather than inferred, which
is what :class:`~tests.fakes.keyring.FakeKeyring` counts fetches for.

The fourth is the difference between "that token is not ours" and "keyring is down", which
is the difference between telling somebody to log in again and telling their assistant to
try again shortly. Every shape of unusable document is the second one, and only a fetch
that *worked* and came back without the key id is the first.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Protocol

import httpx
import pytest

from tests.conftest import ACCOUNT, SCOPES
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import ISSUER, JWKS_URL, ROTATED_KEY, FakeKeyring, mint, thumbprint
from user_api.auth.jwks import BAD_TOKEN, KEYS_UNAVAILABLE, JwksClient
from user_api.auth.tokens import TokenVerifier
from user_api.domain.errors import AuthenticationError, KeyringUnreachableError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

CACHE_SECONDS = 3_600.0
"""The default cache lifetime. Tests that are *about* expiry pass their own, low."""

WINDOW_SECONDS = 60.0
"""The default floor between fetches an unknown key id may provoke, likewise."""

TIMEOUT_SECONDS = 5.0
CONCURRENT_REQUESTS = 10
FLOOD = 50


class MakeJwks(Protocol):
    """Builds a client against the fake keyring, closed when the test ends."""

    def __call__(
        self,
        *,
        cache_seconds: float = ...,
        min_refetch_seconds: float = ...,
        transport: httpx.AsyncBaseTransport | None = ...,
    ) -> JwksClient: ...


def serving(status: int, body: bytes) -> httpx.MockTransport:
    """A keyring answering with exactly these bytes.

    :class:`~tests.fakes.keyring.FakeKeyring` serves documents that are at least JSON
    objects, which is every shape keyring itself could produce. These are the shapes
    something *else* produces: a proxy's error page, a truncated body, an answer from a
    service that is not keyring at all.
    """

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handle)


def verifier_for(jwks: JwksClient, clock: FakeClock) -> TokenVerifier:
    """A verifier over one client, for the tests that are about whole tokens."""
    return TokenVerifier(
        jwks=jwks,
        issuer=ISSUER,
        audience_prefix="user",
        allowed_scopes=SCOPES,
        clock=clock,
    )


@pytest.fixture
async def make_jwks(clock: FakeClock, keyring: FakeKeyring) -> AsyncIterator[MakeJwks]:
    """Build clients whose bytes come from the fake keyring rather than the network.

    The pool the constructor made is replaced rather than patched: everything under test
    keeps its own ``get``, and the only thing substituted is where the document comes
    from. Both pools are closed at the end, because an unclosed one is a warning and this
    suite runs with warnings as errors.
    """
    pools: list[httpx.AsyncClient] = []

    def make(
        *,
        cache_seconds: float = CACHE_SECONDS,
        min_refetch_seconds: float = WINDOW_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> JwksClient:
        client = JwksClient(
            url=JWKS_URL,
            clock=clock,
            cache_seconds=cache_seconds,
            min_refetch_seconds=min_refetch_seconds,
            timeout_seconds=TIMEOUT_SECONDS,
        )
        pools.append(client._client)
        if transport is None:
            transport = keyring.transport()
        client._client = httpx.AsyncClient(transport=transport)
        pools.append(client._client)
        return client

    yield make

    for pool in pools:
        await pool.aclose()


class TestJwksCaching:
    async def test_a_second_request_for_the_same_key_does_not_ask_keyring_again(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        client = make_jwks()

        first = await client.key_for(thumbprint())
        second = await client.key_for(thumbprint())

        assert keyring.fetches == 1
        assert first.key_id == thumbprint()
        assert second.key_id == thumbprint()

    async def test_a_cache_younger_than_its_lifetime_is_still_used(
        self, make_jwks: MakeJwks, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        client = make_jwks(cache_seconds=120.0)
        await client.key_for(thumbprint())

        clock.advance(119.0)
        await client.key_for(thumbprint())

        assert keyring.fetches == 1

    async def test_the_cache_expires_on_the_injected_clock_and_the_keys_are_fetched_again(
        self, make_jwks: MakeJwks, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        """Age is measured on the clock this client was handed, not on the wall.

        A cache whose expiry can only be exercised by waiting an hour is a cache whose
        expiry nobody tests, and an expiry nobody tests is how a rotated key goes
        unnoticed until somebody restarts the process.
        """
        client = make_jwks(cache_seconds=120.0)
        await client.key_for(thumbprint())

        clock.advance(120.0)
        await client.key_for(thumbprint())

        assert keyring.fetches == 2

    async def test_ten_concurrent_cold_requests_make_exactly_one_fetch_between_them(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        """A restart under load is precisely this shape.

        Every request in flight arrives to find no document at all, and without the lock
        each of them fetches its own copy of the same one -- so the moment keyring is
        busiest is the moment this service multiplies its requests to it.
        """
        client = make_jwks()

        keys = await asyncio.gather(
            *(client.key_for(thumbprint()) for _ in range(CONCURRENT_REQUESTS))
        )

        assert keyring.fetches == 1
        assert [key.key_id for key in keys] == [thumbprint()] * CONCURRENT_REQUESTS


class TestUnknownKidRateLimit:
    async def test_a_flood_of_invented_key_ids_provokes_at_most_one_fetch_per_window(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        """The amplifier, and the one thing that closes it.

        A key id is read before anything at all has been verified, which makes it the one
        value an unauthenticated caller puts in front of the verifier. Without a floor
        between the fetches an unrecognised one may provoke, every inbound request
        carrying an invented id becomes an outbound request to keyring: an amplifier
        anybody who can reach this service gets to aim, holding no token and no account.
        """
        client = make_jwks(min_refetch_seconds=WINDOW_SECONDS)
        await client.key_for(thumbprint())

        for _ in range(FLOOD):
            with pytest.raises(AuthenticationError):
                await client.key_for(uuid.uuid4().hex)

        # One ordinary fetch to warm the cache, and one the whole flood was allowed
        # between them.
        assert keyring.fetches == 2

    async def test_one_more_fetch_is_allowed_once_the_window_has_passed(
        self, make_jwks: MakeJwks, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        """The floor is a rate and not a ban.

        A key id unknown at half past may be keyring's at half past one, so refusing to
        look again ever would turn a rotation into an outage.
        """
        client = make_jwks(min_refetch_seconds=WINDOW_SECONDS)
        await client.key_for(thumbprint())
        with pytest.raises(AuthenticationError):
            await client.key_for("invented")
        assert keyring.fetches == 2

        clock.advance(WINDOW_SECONDS)
        with pytest.raises(AuthenticationError):
            await client.key_for("invented-again")

        assert keyring.fetches == 3

    async def test_a_suppressed_refetch_is_the_caller_being_wrong_not_keyring_being_down(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        """Nothing failed, so nothing may be reported as having failed.

        We looked recently and keyring had no key by that name, which is a fact about the
        token in hand. Reporting it as an outage would answer 503 to a request nobody
        made an outbound call for -- and would hand the flood a way to tell a suppressed
        request apart from an answered one.
        """
        client = make_jwks(min_refetch_seconds=WINDOW_SECONDS)
        await client.key_for(thumbprint())
        with pytest.raises(AuthenticationError):
            await client.key_for("invented")

        with pytest.raises(AuthenticationError) as refusal:
            await client.key_for("invented")

        assert type(refusal.value) is AuthenticationError
        assert str(refusal.value) == BAD_TOKEN
        assert keyring.fetches == 2

    async def test_the_window_is_spent_even_when_the_fetch_it_allowed_fails(
        self, make_jwks: MakeJwks, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        """An outage is when a flood must not become a flood of requests.

        The window is claimed before the request goes out rather than after it comes
        back, so a keyring that is already having a bad day is not asked once per
        invented key id while it recovers.
        """
        client = make_jwks(min_refetch_seconds=WINDOW_SECONDS)
        await client.key_for(thumbprint())
        keyring.error = httpx.ConnectError("connection refused")

        with pytest.raises(KeyringUnreachableError):
            await client.key_for("invented")
        with pytest.raises(AuthenticationError):
            await client.key_for("invented-again")

        assert keyring.fetches == 2


class TestKeyRotation:
    async def test_a_token_signed_with_the_replacement_key_verifies(
        self, make_jwks: MakeJwks, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        """A new key id is exactly what a rotation looks like from here.

        It arrives as an id the cached document does not name, which is the same thing an
        invented one looks like -- so the window that bounds the flood is also what a
        rotation has to be picked up through.
        """
        client = make_jwks(min_refetch_seconds=WINDOW_SECONDS)
        verifier = verifier_for(client, clock)
        await verifier.verify(mint())

        keyring.rotate()
        identity = await verifier.verify(mint(key=ROTATED_KEY))

        assert identity.account_id == ACCOUNT
        assert keyring.fetches == 2

    async def test_the_key_the_rotation_replaced_stops_working(
        self, make_jwks: MakeJwks, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        """A rotation nobody can be refused by is not a rotation.

        The refusal is a window late on purpose: within one we do not look again, and a
        token signed by a key keyring no longer publishes is refused either way.
        """
        client = make_jwks(min_refetch_seconds=WINDOW_SECONDS)
        verifier = verifier_for(client, clock)
        await verifier.verify(mint())
        keyring.rotate()
        await verifier.verify(mint(key=ROTATED_KEY))

        clock.advance(WINDOW_SECONDS)
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint())

        assert keyring.fetches == 3


class TestKeyringUnreachable:
    @pytest.mark.parametrize(
        ("status", "body"),
        [
            pytest.param(503, b'{"keys": []}', id="an-error-status"),
            pytest.param(200, b"<html>502 Bad Gateway</html>", id="a-body-that-is-not-json"),
            pytest.param(200, b'"keys"', id="json-that-is-not-an-object"),
            pytest.param(200, b'{"kid": "something"}', id="a-document-with-no-keys-member"),
            pytest.param(200, b'{"keys": {"kid": "x"}}', id="keys-that-are-not-a-list"),
            pytest.param(200, b'{"keys": []}', id="a-keys-list-with-nothing-in-it"),
            pytest.param(200, b'{"keys": [{"kty": "nothing-we-know"}]}', id="no-usable-key"),
            pytest.param(200, b'{"keys": ["not-a-mapping"]}', id="an-entry-that-is-not-a-mapping"),
            pytest.param(
                200,
                b'{"keys": [{"kty": "RSA", "alg": "RS256", "kid": "k", "n": "AQ", "e": "AQAB"}]}',
                id="a-key-whose-numbers-do-not-parse",
            ),
        ],
    )
    async def test_a_document_we_cannot_use_is_keyring_being_unreachable(
        self, make_jwks: MakeJwks, status: int, body: bytes
    ) -> None:
        """Not one of these says anything about the token in hand.

        A proxy's error page served with a 200, a document with no keys in it, a key of a
        type nothing here can verify with: in every case the token may be perfectly good
        and we have no way to tell. Answering 401 would send somebody back through a login
        that would not have helped, because what was broken was not their session.
        """
        client = make_jwks(transport=serving(status, body))

        with pytest.raises(KeyringUnreachableError) as failure:
            await client.key_for(thumbprint())

        assert str(failure.value) == KEYS_UNAVAILABLE

    async def test_a_keyring_that_cannot_be_reached_at_all_is_not_the_callers_fault(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        client = make_jwks()
        keyring.error = httpx.ConnectError("connection refused")

        with pytest.raises(KeyringUnreachableError):
            await client.key_for(thumbprint())

    async def test_a_fetch_that_worked_and_lacks_the_key_id_is_the_token_being_wrong(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        """The other half of the same distinction.

        The document arrived, and nothing in it is the key this token names. That is a
        fact about the token -- keyring did not sign it -- so it is a 401 and telling the
        holder to get another token is advice that will work.
        """
        client = make_jwks()

        with pytest.raises(AuthenticationError) as refusal:
            await client.key_for("a-key-id-nobody-published")

        assert str(refusal.value) == BAD_TOKEN
        assert keyring.fetches == 1

    async def test_the_health_check_reports_the_failure_rather_than_raising_it(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        # A health check that raised would be a health check answering 500 while trying
        # to say what is wrong, and whatever is polling it cannot read a traceback.
        client = make_jwks()
        keyring.error = httpx.ConnectError("connection refused")

        healthy, reason = await client.healthy()

        assert healthy is False
        assert reason == KEYS_UNAVAILABLE

    async def test_the_reason_carries_no_url_and_no_traceback(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        """Fixed text rather than the exception's own message.

        What an HTTP client says carries the URL it was handed, and a URL can carry
        credentials in its userinfo -- which is how a password ends up in the output of a
        health endpoint anybody is allowed to poll.
        """
        client = make_jwks()
        keyring.error = httpx.ConnectError(f"failed to connect to {JWKS_URL}")

        _, reason = await client.healthy()

        assert reason is not None
        assert JWKS_URL not in reason
        assert "keyring.test" not in reason
        assert "ConnectError" not in reason
        assert "Traceback" not in reason

    async def test_the_health_check_fetches_when_the_cache_is_cold(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        # A fresh process holds nothing, and that is the moment an operator most wants to
        # know whether keyring can be reached -- so reporting only on the cache would be
        # reporting nothing at all.
        client = make_jwks()

        assert await client.healthy() == (True, None)
        assert keyring.fetches == 1

    async def test_the_health_check_answers_from_a_warm_cache_without_asking_keyring(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        client = make_jwks()
        await client.key_for(thumbprint())

        assert await client.healthy() == (True, None)
        assert keyring.fetches == 1

    async def test_constructing_the_client_fetches_nothing(
        self, make_jwks: MakeJwks, keyring: FakeKeyring
    ) -> None:
        """Starting up must not require keyring to be up.

        A service that refused to start unless keyring were reachable would turn one
        outage into two, at the worst possible moment: these two are restarted together.
        """
        make_jwks()

        assert keyring.fetches == 0


class TestClosing:
    async def test_closing_releases_the_connection_pool(self, make_jwks: MakeJwks) -> None:
        client = make_jwks()
        await client.key_for(thumbprint())

        await client.aclose()

        assert client._client.is_closed

    async def test_closing_twice_is_not_an_error(self, make_jwks: MakeJwks) -> None:
        # Shutdown runs on paths that may have closed it already -- a lifespan that failed
        # half way through, say -- and a second close must not be what breaks the
        # shutdown that was already going badly.
        client = make_jwks()
        await client.aclose()

        await client.aclose()

        assert client._client.is_closed
