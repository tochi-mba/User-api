"""Turning a bearer token into an identity, or into one undifferentiated refusal.

Every rule about the token itself -- RS256 only, the issuer pinned, every claim keyring sets
required, expiry checked against the injected clock, one refusal for everything -- belongs to
:class:`keyring_client.TokenVerifier`, which every service in the family shares and which is
tested against keyring's own signer. What is left here is what only this service decides:

**The token must belong to the ``user`` family, and its audience is its scope.** ``user``
grants no scope beyond unscoped entries; ``user.health`` grants ``health``; an audience naming
a scope this deployment does not recognise is refused outright rather than treated as granting
nothing (:func:`~user_api.domain.scopes.granted_scope`). The scope is derived from the verified
audience and from nothing a caller supplies.

**The vocabulary.** The library's errors become this service's domain errors at this
boundary. Every refusal is :class:`~user_api.domain.errors.AuthenticationError` carrying
:data:`BAD_TOKEN`, whichever rule refused, and the reason goes to the log. Keyring being
unreachable stays :class:`~user_api.domain.errors.KeyringUnreachableError` -- a 503 rather than
a 401, because logging in again would not have helped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from keyring_client import ALGORITHM, REQUIRED_CLAIMS, AudienceFamily
from keyring_client import AuthenticationError as TokenRefusedError
from keyring_client import KeyringUnreachableError as KeysUnavailableError
from keyring_client import TokenVerifier as KeyringTokenVerifier

from user_api.auth.jwks import BAD_TOKEN, KEYS_UNAVAILABLE
from user_api.core.logging import get_logger
from user_api.domain.errors import AuthenticationError, InvalidScopeError, KeyringUnreachableError
from user_api.domain.scopes import granted_scope

if TYPE_CHECKING:
    from user_api.auth.jwks import JwksClient
    from user_api.core.clock import Clock

__all__ = ["ALGORITHM", "REQUIRED_CLAIMS", "Identity", "TokenVerifier"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Identity:
    """Who a request is for, and how much of their record it may see."""

    account_id: str
    """The token's verified ``sub``, and the only identity this service ever learns.

    Keyring's opaque account id rather than an email address. Nothing here can turn it back
    into a person, which is what makes it safe to put in a log line and safe to use as the key
    everything else is scoped by.
    """

    audience: str
    """The verified ``aud``, recorded as ``asserted_by`` on every write.

    Provenance the server derived rather than provenance the writer claimed, which is the
    distinction the ``entries`` table is split along: an assistant can say anything about where
    a fact came from, and it cannot say anything about which token it held.
    """

    granted_scope: str | None
    """The one scope this token grants, or ``None`` for the bare audience prefix.

    ``None`` is not "everything": it is a token that reads entries carrying no scope at all.
    See :mod:`user_api.domain.scopes`.
    """

    token: str | None = field(default=None, repr=False)
    """The bearer string, presented to settings-api as the user token.

    Held so a request can ask for this person's settings without reading the
    Authorization header a second time. ``repr=False`` because a log line or an error
    that rendered the identity would otherwise print a live credential. ``None`` is a
    caller constructed without one -- unit tests, a path that does not authenticate --
    and gets the deployment's caps.
    """


class TokenVerifier:
    """Checks tokens keyring minted, and works out which scope each one grants."""

    def __init__(
        self,
        *,
        jwks: JwksClient,
        issuer: str,
        audience_prefix: str,
        allowed_scopes: tuple[str, ...],
        clock: Clock,
    ) -> None:
        self._verifier = KeyringTokenVerifier(jwks=jwks, issuer=issuer, clock=clock, logger=logger)
        self._family = AudienceFamily(audience_prefix)
        self._audience_prefix = audience_prefix
        self._allowed_scopes = allowed_scopes

    async def verify(self, token: str) -> Identity:
        """Check a token and work out who it is for.

        Raises:
            AuthenticationError: any refusal the shared verifier makes, an audience outside the
                ``user`` family, or one naming a scope this deployment does not have. One
                message for all of them, on purpose.
            KeyringUnreachableError: keyring's keys could not be fetched, so whether this token
                is good is not something we know.
        """
        try:
            verified = await self._verifier.verify(token, audience=self._family)
        except TokenRefusedError as exc:
            raise AuthenticationError(BAD_TOKEN) from exc
        except KeysUnavailableError as exc:
            raise KeyringUnreachableError(KEYS_UNAVAILABLE) from exc

        return Identity(
            account_id=verified.account_id,
            audience=verified.audience,
            granted_scope=self._scope_of(verified.audience),
            token=token,
        )

    def _scope_of(self, audience: str) -> str | None:
        """What a verified audience grants, or a refusal.

        Derived here and passed in from nowhere, which is the property
        :mod:`user_api.domain.scopes` exists to hold: there is no route by which a caller's own
        claim about what it may read reaches this value.
        """
        try:
            scope = granted_scope(
                audience, prefix=self._audience_prefix, allowed=self._allowed_scopes
            )
        except InvalidScopeError as exc:
            # A scope this deployment does not have. Told what every other refusal is told.
            logger.info("token_rejected", reason="scope")
            raise AuthenticationError(BAD_TOKEN) from exc
        return scope
