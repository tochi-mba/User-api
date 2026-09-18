"""Turning a bearer token into an identity, or into one undifferentiated refusal.

The token rules themselves -- the pinned algorithm, the pinned issuer, every required claim,
the injected clock, the JWKS rate limits -- belong to :class:`keyring_client.TokenVerifier` and
are tested exhaustively in the keyring repository, against keyring's own signer. The few kept
here prove this adapter hands the verifier this service's issuer, clock and keys.

The rest is what only this service decides: that the audience is a family whose suffix is the
scope, that an unknown scope is refused rather than granting nothing, and that every refusal,
from every path, is the same refusal.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT, SCOPES
from tests.fakes.clock import EPOCH, FakeClock
from tests.fakes.keyring import (
    DEFAULT_TTL_SECONDS,
    ISSUER,
    JWKS_URL,
    ROTATED_KEY,
    FakeKeyring,
    forge_hs256,
    forge_unsigned,
    mint,
)
from user_api.auth.jwks import BAD_TOKEN, JwksClient
from user_api.auth.tokens import ALGORITHM, REQUIRED_CLAIMS, Identity, TokenVerifier
from user_api.domain.errors import AuthenticationError, KeyringUnreachableError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PREFIX = "user"
OTHER_ISSUER = "https://keyring.other.test"


@pytest.fixture
async def jwks_client(clock: FakeClock, keyring: FakeKeyring) -> AsyncIterator[JwksClient]:
    client = JwksClient(url=JWKS_URL, clock=clock, transport=keyring.transport())
    yield client
    await client.aclose()


@pytest.fixture
def verifier(jwks_client: JwksClient, clock: FakeClock) -> TokenVerifier:
    return TokenVerifier(
        jwks=jwks_client,
        issuer=ISSUER,
        audience_prefix=PREFIX,
        allowed_scopes=SCOPES,
        clock=clock,
    )


class TestTheSharedRulesAreWiredIn:
    def test_the_rules_are_the_familys(self) -> None:
        assert ALGORITHM == "RS256"
        assert set(REQUIRED_CLAIMS) == {"exp", "iat", "iss", "sub", "aud"}

    @pytest.mark.parametrize("forged", [forge_unsigned(), forge_hs256()], ids=["none", "hs256"])
    async def test_the_algorithm_confusion_attacks_are_refused(
        self, verifier: TokenVerifier, forged: str
    ) -> None:
        with pytest.raises(AuthenticationError):
            await verifier.verify(forged)

    async def test_a_key_keyring_does_not_publish_is_refused(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(key=ROTATED_KEY))

    async def test_this_deployments_issuer_is_the_one_pinned(self, verifier: TokenVerifier) -> None:
        # Signed with the right key, for the right audience, well within its lifetime. The
        # only thing wrong with it is who minted it.
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(issuer=OTHER_ISSUER))

    async def test_a_token_stops_working_at_the_second_it_expires_on_the_injected_clock(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        clock.advance(DEFAULT_TTL_SECONDS - 1)
        assert (await verifier.verify(mint())).account_id == ACCOUNT

        clock.advance(1)

        with pytest.raises(AuthenticationError):
            await verifier.verify(mint())

    async def test_a_token_issued_ahead_of_the_wall_clock_is_judged_on_the_injected_one(
        self, jwks_client: JwksClient
    ) -> None:
        next_decade = EPOCH + timedelta(days=3_650)
        verifier = TokenVerifier(
            jwks=jwks_client,
            issuer=ISSUER,
            audience_prefix=PREFIX,
            allowed_scopes=SCOPES,
            clock=FakeClock(start=next_decade),
        )

        identity = await verifier.verify(mint(issued_at=next_decade))

        assert identity.account_id == ACCOUNT

    async def test_keyring_being_unreachable_is_not_turned_into_a_refusal(
        self, verifier: TokenVerifier, keyring: FakeKeyring
    ) -> None:
        # A 503, not a 401: the token may be perfectly good and we cannot tell.
        keyring.error = httpx.ConnectError("down")

        with pytest.raises(KeyringUnreachableError):
            await verifier.verify(mint())


class TestAudience:
    async def test_the_bare_audience_grants_no_scope_at_all(self, verifier: TokenVerifier) -> None:
        """``None`` is not "everything".

        A token for ``user`` reads the entries that carry no scope, and nothing else. The bare
        audience being the *weakest* token rather than the strongest is the whole shape of the
        family, and it is the one that would be most expensive to get wrong.
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

    @pytest.mark.parametrize("audience", ["downstream-tool", "users", "user-api", "settings.user"])
    async def test_a_token_minted_for_another_service_is_refused(
        self, verifier: TokenVerifier, audience: str
    ) -> None:
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(audience=audience))


class TestUnknownScope:
    async def test_an_audience_naming_a_scope_this_deployment_has_not_got_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        """Refused, rather than treated as granting nothing.

        Granting nothing is the tempting reading and the dangerous one: a typo in a mint
        request would produce a token that authenticates, reads the unscoped entries, and looks
        exactly like a correctly configured assistant that nobody has told anything yet.
        """
        with pytest.raises(AuthenticationError):
            await verifier.verify(mint(audience="user.helth"))

    async def test_a_scope_another_deployment_configures_is_still_refused_here(
        self, jwks_client: JwksClient, clock: FakeClock
    ) -> None:
        narrow = TokenVerifier(
            jwks=jwks_client,
            issuer=ISSUER,
            audience_prefix=PREFIX,
            allowed_scopes=("home",),
            clock=clock,
        )

        with pytest.raises(AuthenticationError):
            await narrow.verify(mint(audience="user.health"))


class TestIdentity:
    async def test_the_account_id_is_the_verified_subject(self, verifier: TokenVerifier) -> None:
        identity = await verifier.verify(mint(account_id="account-zed"))

        assert identity.account_id == "account-zed"

    async def test_the_audience_recorded_is_the_verified_one(self, verifier: TokenVerifier) -> None:
        # Recorded as `asserted_by` on every write: provenance the server derived rather than
        # provenance the writer claimed.
        raw = mint(account_id=ACCOUNT, audience="user.work")
        identity = await verifier.verify(raw)

        assert identity == Identity(
            account_id=ACCOUNT, audience="user.work", granted_scope="work", token=raw
        )
        assert raw not in repr(identity)


class TestOneRefusalForEverything:
    async def test_every_way_a_token_can_be_refused_says_exactly_the_same_thing(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        """One message, byte for byte, whichever rule did the refusing -- the shared rules or
        this service's own scope rule. Which rule refused goes to the logs."""
        refusals = [
            mint(audience="downstream-tool"),
            mint(audience="user.dinosaurs"),
            mint(issuer=OTHER_ISSUER),
            forge_hs256(),
            forge_unsigned(),
            mint(key=ROTATED_KEY),
            mint(kid="a-key-id-nobody-published"),
            mint(account_id=OTHER_ACCOUNT, omit="aud"),
            "not-a-token",
            "",
        ]

        messages: set[str] = set()
        for token in refusals:
            with pytest.raises(AuthenticationError) as refusal:
                await verifier.verify(token)
            messages.add(str(refusal.value))

        clock.advance(DEFAULT_TTL_SECONDS)
        with pytest.raises(AuthenticationError) as expired:
            await verifier.verify(mint())
        messages.add(str(expired.value))

        assert messages == {BAD_TOKEN}
