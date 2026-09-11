"""Turning a bearer token into an identity, or into one undifferentiated refusal.

This is the service's front door, so most of what is here is a refusal. They are grouped
by the rule that does the refusing -- the pinned algorithm, the pinned issuer, the clock,
the required claims -- and the last test in the file is the one that ties the group
together: every one of those refusals says the same thing, byte for byte, because each
distinction a caller can tell apart is an oracle that helps somebody forge the next token.

Several of the tokens here are assembled by hand rather than minted, and always for the
same reason: PyJWT declines to *produce* the token an attacker would send. It will not
sign with a public key, and it will not encode a header it disapproves of -- both of which
guard somebody writing a signer and neither of which does anything for a verifier. So
those tokens are put together the way an attacker would put them together. Nothing here
uses a mocking library: the keys are real, the signatures are real, and the only thing
wrong with each token is the thing its test is named after.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from tests.conftest import ACCOUNT, OTHER_ACCOUNT, SCOPES
from tests.fakes.clock import EPOCH, FakeClock
from tests.fakes.keyring import (
    DEFAULT_TTL_SECONDS,
    ISSUER,
    JWKS_URL,
    SIGNING_KEY,
    FakeKeyring,
    forge_hs256,
    forge_unsigned,
    mint,
    private_pem,
    thumbprint,
)
from user_api.auth.jwks import BAD_TOKEN, JwksClient
from user_api.auth.tokens import REQUIRED_CLAIMS, TokenVerifier
from user_api.domain.errors import AuthenticationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PREFIX = "user"
OTHER_ISSUER = "https://keyring.other.test"
CACHE_SECONDS = 3_600.0
WINDOW_SECONDS = 60.0
TIMEOUT_SECONDS = 5.0


def claims(**changes: Any) -> dict[str, Any]:
    """The claim set keyring mints, with any of it bent.

    ``mint`` covers every token keyring would really issue. This is for the ones it would
    not: an audience that is a list, a header with no ``kid`` in it.
    """
    minted: dict[str, Any] = {
        "iss": ISSUER,
        "sub": ACCOUNT,
        "aud": PREFIX,
        "iat": int(EPOCH.timestamp()),
        "exp": int(EPOCH.timestamp()) + DEFAULT_TTL_SECONDS,
    }
    return {**minted, **changes}


def sign(payload: dict[str, Any], **headers: Any) -> str:
    """Sign a claim set with keyring's real key, with exactly these headers."""
    return jwt.encode(payload, private_pem(), algorithm="RS256", headers=headers)


def encode(segment: dict[str, Any]) -> str:
    packed = json.dumps(segment, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(packed).rstrip(b"=").decode()


def hand_signed(payload: dict[str, Any], **headers: Any) -> str:
    """A genuine RS256 token, assembled without PyJWT's help.

    PyJWT refuses to *encode* a header it disapproves of, a ``kid`` that is not a string
    among them. That is the same guard as its refusal to sign with a public key: it
    protects somebody writing a signer and does nothing at all for a verifier, which is
    the side this service is on. So the token is put together the way an attacker would
    put it together -- real key, real signature -- and the only thing wrong with it is the
    thing the test is named after.
    """
    header = encode({"typ": "JWT", "alg": "RS256", **headers})
    body = encode(payload)
    signature = SIGNING_KEY.sign(f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return ".".join([header, body, base64.urlsafe_b64encode(signature).rstrip(b"=").decode()])


def decode(segment: str) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    )
    return decoded


def with_payload(token: str, **changes: Any) -> str:
    """The same token with its payload edited and its signature left as it was."""
    header, payload, signature = token.split(".")
    return ".".join([header, encode({**decode(payload), **changes}), signature])


def with_raw_payload(token: str, raw: bytes) -> str:
    """The same token with something that is not JSON where its payload was."""
    header, _, signature = token.split(".")
    return ".".join([header, base64.urlsafe_b64encode(raw).rstrip(b"=").decode(), signature])


def with_header(token: str, **changes: Any) -> str:
    """The same token with its header edited and its signature left as it was."""
    header, payload, signature = token.split(".")
    return ".".join([encode({**decode(header), **changes}), payload, signature])


def with_flipped_signature(token: str) -> str:
    """The same token with one character of its signature changed.

    One character is all it takes, and that is the property: a signature is not a
    checksum, so there is no such thing as nearly right.
    """
    header, payload, signature = token.split(".")
    flipped = ("B" if signature[0] != "B" else "C") + signature[1:]
    return ".".join([header, payload, flipped])


def with_borrowed_signature(token: str, other: str) -> str:
    """The same token carrying a real signature that was made over another one."""
    return ".".join([*token.split(".")[:2], other.split(".")[2]])


@pytest.fixture
async def jwks_client(clock: FakeClock, keyring: FakeKeyring) -> AsyncIterator[JwksClient]:
    """A keys client whose bytes come from the fake keyring rather than the network."""
    client = JwksClient(
        url=JWKS_URL,
        clock=clock,
        cache_seconds=CACHE_SECONDS,
        min_refetch_seconds=WINDOW_SECONDS,
        timeout_seconds=TIMEOUT_SECONDS,
    )
    pool = client._client
    client._client = httpx.AsyncClient(transport=keyring.transport())

    yield client

    await client.aclose()
    await pool.aclose()


@pytest.fixture
def verifier(jwks_client: JwksClient, clock: FakeClock) -> TokenVerifier:
    return TokenVerifier(
        jwks=jwks_client,
        issuer=ISSUER,
        audience_prefix=PREFIX,
        allowed_scopes=SCOPES,
        clock=clock,
    )


class TestAudience:
    async def test_the_bare_audience_grants_no_scope_at_all(self, verifier: TokenVerifier) -> None:
        """``None`` is not "everything".

        A token for ``user`` reads the entries that carry no scope, and nothing else. The
        bare audience being the *weakest* token rather than the strongest is the whole
        shape of the family, and it is the one that would be most expensive to get wrong.
        """
        identity = await verifier.verify(mint(audience="user"))

        assert identity.granted_scope is None
        assert identity.audience == "user"

    @pytest.mark.parametrize("scope", SCOPES)
    async def test_a_scoped_audience_grants_exactly_the_scope_it_names(
        self, verifier: TokenVerifier, scope: str
    ) -> None:
        identity = await verifier.verify(mint(audience=f"user.{scope}"))

        assert identity.granted_scope == scope

    async def test_a_token_minted_for_another_service_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        """Less of a tautology than it looks, because the audience is read twice.

        PyJWT checks an audience only against one it has been handed, and the only place
        to learn which one a token claims is the token -- so a ``media-tool`` token is
        checked against ``media-tool`` and passes. What refuses it is the scope rule,
        reading the verified claim afterwards.
        """
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(audience="media-tool"))


class TestUnknownScope:
    async def test_an_audience_naming_a_scope_this_deployment_has_not_got_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        """Refused, rather than treated as granting nothing.

        Granting nothing is the tempting reading and the dangerous one: a typo in a mint
        request would produce a token that authenticates, reads the unscoped entries, and
        looks exactly like a correctly configured assistant that nobody has told anything
        yet. The person would be left wondering why their health assistant knows no
        health.
        """
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(audience="user.helth"))

    async def test_a_scope_another_deployment_configures_is_still_refused_here(
        self, jwks_client: JwksClient, clock: FakeClock
    ) -> None:
        # The allowed scopes are the only thing narrowed: the token is otherwise exactly
        # the one the default deployment accepts, so nothing but the configured set can
        # be what refuses it.
        narrow = TokenVerifier(
            jwks=jwks_client,
            issuer=ISSUER,
            audience_prefix=PREFIX,
            allowed_scopes=("home",),
            clock=clock,
        )

        with pytest.raises(AuthenticationError):
            await narrow.verify(mint(audience="user.health"))


class TestIssuer:
    async def test_a_token_from_another_issuer_is_refused(self, verifier: TokenVerifier) -> None:
        # Signed with the right key, for the right audience, well within its lifetime. The
        # only thing wrong with it is who minted it, which is the whole of what pinning
        # the issuer is for on the morning somebody stands up a second keyring.
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(issuer=OTHER_ISSUER))


class TestExpiry:
    """PyJWT's own ``verify_exp`` is off, and this is what checks instead.

    It reads the injected clock, so these tests can assert more than "a token minted now
    is valid now": the interesting moment is the one the token stops working at, and on
    the wall clock that moment is a quarter of an hour away.
    """

    async def test_a_token_is_accepted_while_it_is_still_within_its_lifetime(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        clock.advance(DEFAULT_TTL_SECONDS - 1)

        identity = await verifier.verify(mint())

        assert identity.account_id == ACCOUNT

    async def test_a_token_stops_working_at_the_very_second_it_expires(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        # The boundary is exclusive: at ``exp`` the token is gone, not on its last second.
        clock.advance(DEFAULT_TTL_SECONDS)

        with pytest.raises(AuthenticationError):
            await verifier.verify(mint())

    async def test_a_token_issued_ahead_of_the_wall_clock_is_judged_on_the_injected_one(
        self, jwks_client: JwksClient
    ) -> None:
        """Why ``verify_iat`` is off as well as ``verify_exp``.

        PyJWT refuses a token whose ``iat`` is in the future by the *wall* clock, so a
        test that pinned the injected clock to next Tuesday would watch every perfectly
        good token be refused by a rule it never asked for -- and the person putting
        ``verify_exp`` back would find switching that one off alone was not enough.
        """
        next_decade = EPOCH + timedelta(days=3_650)
        later = FakeClock(start=next_decade)
        verifier = TokenVerifier(
            jwks=jwks_client,
            issuer=ISSUER,
            audience_prefix=PREFIX,
            allowed_scopes=SCOPES,
            clock=later,
        )

        identity = await verifier.verify(mint(issued_at=next_decade))

        assert identity.account_id == ACCOUNT


class TestAlgorithmConfusion:
    """Both halves of the classic JWT failure, assembled by hand.

    PyJWT refuses to *sign* with a public key. That refusal protects somebody writing a
    signer and does nothing whatever for a verifier, so neither of these tokens comes out
    of ``jwt.encode``: they are built exactly as an attacker would build them, out of a
    document anybody may fetch -- because being fetchable by anybody is what a JWKS
    document is for.
    """

    async def test_a_token_signed_hs256_with_the_published_public_key_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        with pytest.raises(AuthenticationError):
            await verifier.verify(forge_hs256())

    async def test_a_token_claiming_no_algorithm_at_all_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        with pytest.raises(AuthenticationError):
            await verifier.verify(forge_unsigned())

    async def test_the_forgeries_are_otherwise_perfect_tokens(
        self, verifier: TokenVerifier
    ) -> None:
        """What the two tests above would be worth without this one.

        Every claim in both forgeries is the claim a good token carries, and each names a
        key id keyring really published. Nothing but the pinned algorithm stands between
        them and an identity, which is why the list is pinned and never merely defaulted.
        """
        for forged in (forge_hs256(), forge_unsigned()):
            unverified = jwt.decode(forged, options={"verify_signature": False})

            assert unverified["aud"] == PREFIX
            assert unverified["iss"] == ISSUER
            assert jwt.get_unverified_header(forged)["kid"] == thumbprint()


class TestTampering:
    async def test_a_payload_edited_after_signing_is_refused(self, verifier: TokenVerifier) -> None:
        # The account id is what every row in this service is scoped by, so a token whose
        # ``sub`` can be edited is a token for every account at once.
        with pytest.raises(AuthenticationError):
            await verifier.verify(with_payload(mint(account_id=ACCOUNT), sub=OTHER_ACCOUNT))

    async def test_a_scope_widened_after_signing_is_refused(self, verifier: TokenVerifier) -> None:
        # The same edit, aimed at the other half of the identity: an assistant told about
        # somebody's home promoting itself to their health record.
        with pytest.raises(AuthenticationError):
            await verifier.verify(with_payload(mint(audience="user"), aud="user.health"))

    async def test_flipping_a_byte_in_the_signature_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        with pytest.raises(AuthenticationError):
            await verifier.verify(with_flipped_signature(mint()))

    async def test_a_signature_borrowed_from_another_token_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        # Both tokens are keyring's and the borrowed signature is a real one. It covers
        # the other token's claims, which is the only thing a signature ever promises.
        borrowed = with_borrowed_signature(mint(account_id=ACCOUNT), mint(account_id=OTHER_ACCOUNT))

        with pytest.raises(AuthenticationError):
            await verifier.verify(borrowed)

    async def test_swapping_the_header_is_refused(self, verifier: TokenVerifier) -> None:
        # The header is signed too -- the signing input is header.payload -- so an
        # algorithm swapped in afterwards invalidates the signature it was hoping to keep.
        with pytest.raises(AuthenticationError):
            await verifier.verify(with_header(mint(), alg="HS256"))


class TestMissingClaims:
    @pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
    async def test_a_token_that_omits_a_required_claim_is_refused(
        self, verifier: TokenVerifier, claim: str
    ) -> None:
        """PyJWT verifies most claims only when they are present.

        So a token that simply leaves one out is a token that passes the check for it: no
        issuer is an issuer nobody pinned, no expiry is an expiry nobody compared against
        the clock. Requiring each of them is what turns every one of those from a pass
        into a refusal.
        """
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(omit=claim))


class TestBadKid:
    async def test_a_token_with_no_key_id_is_refused(self, verifier: TokenVerifier) -> None:
        # There is no default key. Choosing one when the token names none would be
        # choosing which key to verify against on the sender's behalf.
        with pytest.raises(AuthenticationError):
            await verifier.verify(sign(claims()))

    async def test_a_key_id_that_is_not_a_string_is_refused(self, verifier: TokenVerifier) -> None:
        # A key id ends up as a lookup key, so a caller that picks its *type* as well as
        # its contents has to be refused rather than looked up. PyJWT's own header
        # validation happens to get there first; this service's check is what catches the
        # other shape of the same problem, a header with no key id in it at all.
        with pytest.raises(AuthenticationError):
            await verifier.verify(hand_signed(claims(), kid=7))

    async def test_a_key_id_nobody_published_is_refused(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(kid="a-key-id-nobody-published"))

    async def test_a_malformed_key_id_is_refused_before_a_single_key_is_fetched(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        """The id is read first, so a token that has none costs keyring nothing.

        That ordering is what the rate limit in :mod:`user_api.auth.jwks` is built on: a
        flood of nonsense must not be a flood of outbound requests, and the cheapest
        nonsense of all is refused before any of them.
        """
        with pytest.raises(AuthenticationError):
            await verifier.verify(sign(claims()))

        assert keyring.fetches == 0


class TestMalformed:
    @pytest.mark.parametrize(
        "token",
        [
            pytest.param("", id="nothing-at-all"),
            pytest.param("not-a-token", id="not-a-jwt"),
            pytest.param("a.b", id="two-segments"),
            pytest.param("a.b.c.d", id="four-segments"),
            pytest.param("...", id="empty-segments"),
        ],
    )
    async def test_something_that_is_not_a_token_is_refused_rather_than_crashing(
        self, verifier: TokenVerifier, token: str
    ) -> None:
        # Unauthenticated callers send these, so each has to come out as the ordinary
        # refusal rather than as a traceback from inside a JWT library.
        with pytest.raises(AuthenticationError):
            await verifier.verify(token)

    async def test_a_header_we_can_read_over_a_payload_we_cannot_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        # The two halves are read at different moments -- the key id first, to choose the
        # key -- so a token can get past the first read and fall over in the second.
        with pytest.raises(AuthenticationError):
            await verifier.verify(with_raw_payload(mint(), b"this is not json"))

    async def test_an_audience_that_is_a_list_is_refused(self, verifier: TokenVerifier) -> None:
        """A single string, and never a list.

        A token naming several audiences is one whose holder is entitled somewhere else as
        well, and since the scope this service grants is derived from the audience,
        "which of them did you mean" would have to be answered by guessing.
        """
        listed = sign(claims(aud=[PREFIX, "media-tool"]), kid=thumbprint())

        with pytest.raises(AuthenticationError):
            await verifier.verify(listed)


class TestIdentity:
    async def test_the_account_id_is_the_verified_subject(self, verifier: TokenVerifier) -> None:
        identity = await verifier.verify(mint(account_id="account-zed"))

        assert identity.account_id == "account-zed"

    async def test_the_audience_recorded_is_the_verified_one(self, verifier: TokenVerifier) -> None:
        # Recorded as ``asserted_by`` on every write: provenance the server derived rather
        # than provenance the writer claimed. An assistant can say anything about where a
        # fact came from and nothing at all about which token it was holding.
        identity = await verifier.verify(mint(account_id=ACCOUNT, audience="user.work"))

        assert identity.audience == "user.work"
        assert identity.granted_scope == "work"
        assert identity.account_id == ACCOUNT


class TestOneRefusalForEverything:
    async def test_every_way_a_token_can_be_refused_says_exactly_the_same_thing(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        """One message, byte for byte, whichever rule did the refusing.

        Every distinction a caller can tell apart is an oracle: "wrong issuer" and "bad
        signature" are two different next things to try, and somebody working out which
        forgery to attempt next is being helped by the difference. Which rule refused goes
        to the logs, where the operator reads it and nobody else does.
        """
        refusals = {
            "another service's audience": mint(audience="media-tool"),
            "a scope this deployment has not got": mint(audience="user.dinosaurs"),
            "another issuer": mint(issuer=OTHER_ISSUER),
            "hs256 signed with the public key": forge_hs256(),
            "no algorithm at all": forge_unsigned(),
            "an edited payload": with_payload(mint(), sub=OTHER_ACCOUNT),
            "a widened audience": with_payload(mint(), aud="user.health"),
            "a flipped signature": with_flipped_signature(mint()),
            "a borrowed signature": with_borrowed_signature(mint(), mint(account_id=OTHER_ACCOUNT)),
            "a swapped header": with_header(mint(), alg="HS256"),
            "no key id": sign(claims()),
            "a key id that is not a string": hand_signed(claims(), kid=7),
            "a key id nobody published": mint(kid="a-key-id-nobody-published"),
            "an audience that is a list": sign(
                claims(aud=[PREFIX, "media-tool"]), kid=thumbprint()
            ),
            "a payload that is not json": with_raw_payload(mint(), b"this is not json"),
            "not a token at all": "not-a-token",
            "two segments": "a.b",
            "nothing at all": "",
            **{f"no {claim} claim": mint(omit=claim) for claim in REQUIRED_CLAIMS},
        }

        messages: set[str] = set()
        for token in refusals.values():
            with pytest.raises(AuthenticationError) as refusal:
                await verifier.verify(token)
            messages.add(str(refusal.value))

        # Last, because it is the only refusal here that needs the clock moved, and moving
        # it would expire every token above as well.
        clock.advance(DEFAULT_TTL_SECONDS)
        with pytest.raises(AuthenticationError) as expired:
            await verifier.verify(mint())
        messages.add(str(expired.value))

        assert messages == {BAD_TOKEN}
