"""A keyring, in as much detail as this service can tell: the family's shared fake.

The real RSA key, the real JWKS document, the transport that counts fetches, and the
hand-assembled forgeries all live in :mod:`keyring_client.testing`, so every service in the
family tests against one fake that answers the way keyring does -- and that fake is itself
checked against keyring's real app in the keyring repository.

This module only sets this service's defaults: tokens are minted for the ``user`` audience with
keyring's own fifteen-minute lifetime. Tests that move the clock by days pass a longer
``ttl_seconds``, so the token in hand does not expire underneath them.
"""

from __future__ import annotations

from typing import Any

from keyring_client.testing import (
    ISSUER,
    JWKS_URL,
    ROTATED_KEY,
    SIGNING_KEY,
    FakeKeyring,
    jwks,
    private_pem,
    public_pem,
    thumbprint,
)
from keyring_client.testing import forge_hs256 as _forge_hs256
from keyring_client.testing import forge_unsigned as _forge_unsigned
from keyring_client.testing import mint as _mint

__all__ = [
    "AUDIENCE",
    "DEFAULT_TTL_SECONDS",
    "ISSUER",
    "JWKS_URL",
    "ROTATED_KEY",
    "SIGNING_KEY",
    "FakeKeyring",
    "forge_hs256",
    "forge_unsigned",
    "jwks",
    "mint",
    "private_pem",
    "public_pem",
    "thumbprint",
]

AUDIENCE = "user"
"""The bare audience, which grants unscoped entries only."""

DEFAULT_TTL_SECONDS = 900
"""Keyring's own access token lifetime. Short, because a signed token cannot be revoked."""


def mint(
    *, audience: str = AUDIENCE, ttl_seconds: float = DEFAULT_TTL_SECONDS, **overrides: Any
) -> str:
    """Mint a token the way keyring's ``issue_service_token`` does, for this service."""
    return _mint(audience=audience, ttl_seconds=ttl_seconds, **overrides)


def forge_hs256(*, account_id: str = "account-a", audience: str = AUDIENCE) -> str:
    """The algorithm-confusion attack: HS256, signed with the published public key."""
    return _forge_hs256(account_id=account_id, audience=audience)


def forge_unsigned(*, account_id: str = "account-a", audience: str = AUDIENCE) -> str:
    """``alg: none`` with an empty signature: the other half of the same attack."""
    return _forge_unsigned(account_id=account_id, audience=audience)
