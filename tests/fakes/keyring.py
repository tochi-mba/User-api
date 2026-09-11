"""A keyring, in as much detail as this service can tell.

This service's entire relationship with keyring is: fetch a JWKS document over HTTP, and
verify RS256 tokens against it. So a convincing fake is a real RSA key, a real JWKS
document, and a transport that serves it -- no network, no ``unittest.mock``, and no
stubbing of the thing under test.

The signing key is generated **once per process** rather than per test. Generating a
2048-bit RSA key takes a tenth of a second or so, and a suite that mints a few hundred
tokens would otherwise spend most of its time doing arithmetic that proves nothing.

``forge`` is the important part. PyJWT refuses to *sign* with a public key, which is a
guard on the signing side and not on the verifying side -- so the algorithm-confusion
tokens have to be assembled by hand, exactly as an attacker would. A test that used
``jwt.encode`` for them would be testing PyJWT's refusal to help rather than this
service's refusal to accept.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.fakes.clock import EPOCH

if TYPE_CHECKING:
    from datetime import datetime

ISSUER = "https://keyring.test"
JWKS_URL = "https://keyring.test/.well-known/jwks.json"
DEFAULT_TTL_SECONDS = 900
"""Keyring's own access token lifetime. Short, because a signed token cannot be revoked."""

_SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ROTATED_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
"""A second key, for the rotation tests. Generated once, for the reason above."""


def _b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()


def _segment(payload: dict[str, Any]) -> bytes:
    packed = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(packed).rstrip(b"=")


def thumbprint(key: rsa.RSAPrivateKey = _SIGNING_KEY) -> str:
    """A stable key id derived from the key, the way keyring derives its own.

    Derived rather than random so it survives a restart -- which is also what makes
    "the kid changed" mean "the key was replaced" rather than "the process restarted".
    """
    numbers = key.public_key().public_numbers()
    return hashlib.sha256(f"{numbers.n}:{numbers.e}".encode()).hexdigest()[:16]


def jwks(*keys: rsa.RSAPrivateKey) -> dict[str, Any]:
    """A JWKS document naming each key, in the shape keyring publishes."""
    chosen = keys or (_SIGNING_KEY,)
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": thumbprint(key),
                "n": _b64(key.public_key().public_numbers().n),
                "e": _b64(key.public_key().public_numbers().e),
            }
            for key in chosen
        ]
    }


def private_pem(key: rsa.RSAPrivateKey = _SIGNING_KEY) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_pem(key: rsa.RSAPrivateKey = _SIGNING_KEY) -> bytes:
    """The public half, exactly as an attacker would get it from the JWKS endpoint."""
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# One parameter per claim, so a test can bend exactly one and leave the rest alone.
def mint(
    *,
    account_id: str = "account-a",
    audience: str = "user",
    issuer: str = ISSUER,
    issued_at: datetime = EPOCH,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    key: rsa.RSAPrivateKey = _SIGNING_KEY,
    kid: str | None = None,
    omit: str | None = None,
) -> str:
    """Mint a token the way keyring's ``create_service_token`` does.

    Args:
        omit: drop one required claim, for the parametrised missing-claim tests. PyJWT
            verifies most claims only when they are present, so "no audience" would be a
            token that passes the audience check -- which is what ``require`` is for and
            what these tests prove.
    """
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": account_id,
        "aud": audience,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    if omit is not None:
        claims.pop(omit)
    return jwt.encode(
        claims, private_pem(key), algorithm="RS256", headers={"kid": kid or thumbprint(key)}
    )


def forge_hs256(*, account_id: str = "account-a", audience: str = "user") -> str:
    """The algorithm-confusion attack: HS256, signed with the published public key.

    Assembled by hand because PyJWT will not sign with a public key. That refusal protects
    somebody writing a signer; it does nothing for a verifier, and a verifier that did not
    pin its algorithm would accept this from anyone who can fetch the JWKS document --
    which is everyone, because that is what a JWKS document is for.
    """
    header = _segment({"alg": "HS256", "typ": "JWT", "kid": thumbprint()})
    payload = _segment(
        {
            "iss": ISSUER,
            "sub": account_id,
            "aud": audience,
            "iat": int(EPOCH.timestamp()),
            "exp": int(EPOCH.timestamp()) + DEFAULT_TTL_SECONDS,
        }
    )
    signing_input = header + b"." + payload
    signature = base64.urlsafe_b64encode(
        hmac.new(public_pem(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    return (signing_input + b"." + signature).decode()


def forge_unsigned(*, account_id: str = "account-a", audience: str = "user") -> str:
    """``alg: none`` with an empty signature. The other half of the same attack."""
    header = _segment({"alg": "none", "typ": "JWT", "kid": thumbprint()})
    payload = _segment(
        {
            "iss": ISSUER,
            "sub": account_id,
            "aud": audience,
            "iat": int(EPOCH.timestamp()),
            "exp": int(EPOCH.timestamp()) + DEFAULT_TTL_SECONDS,
        }
    )
    return (header + b"." + payload + b".").decode()


class FakeKeyring:
    """Serves a JWKS document, and counts how often it was asked.

    The count is the assertion in three separate tests -- that a warm cache does not
    refetch, that an unknown ``kid`` refetches at most once a window, and that ten
    concurrent cold requests make one fetch, not ten. Those are all statements about *how
    many times we called keyring*, so counting is the only way to make them.
    """

    def __init__(self, *keys: rsa.RSAPrivateKey) -> None:
        self.keys = list(keys) or [_SIGNING_KEY]
        self.fetches = 0
        self.status = 200
        self.body: dict[str, Any] | None = None
        self.error: Exception | None = None

    def rotate(self, key: rsa.RSAPrivateKey = _ROTATED_KEY) -> None:
        """Replace the published key, as keyring would on a key replacement."""
        self.keys = [key]

    def transport(self) -> httpx.MockTransport:
        """An httpx transport serving this keyring. Hand-written; no mocking library."""

        def handle(_request: httpx.Request) -> httpx.Response:
            self.fetches += 1
            if self.error is not None:
                raise self.error
            if self.body is not None:
                return httpx.Response(self.status, json=self.body)
            return httpx.Response(self.status, json=jwks(*self.keys))

        return httpx.MockTransport(handle)


SIGNING_KEY = _SIGNING_KEY
ROTATED_KEY = _ROTATED_KEY
