"""Keyring's public keys, through the family's shared client.

Fetching, caching, the single-flight lock, the rate limit on unknown key ids, the floor after
a failed fetch, and the grace for cached keys through a keyring outage all live in
:class:`keyring_client.JwksClient`, in the keyring repository, where they are tested against
keyring's real JWKS endpoint. This module keeps the import path the rest of this service uses,
and nothing else.

Two terms of the arrangement are keyring's own and worth knowing here. **A signed token cannot
be revoked**: logging out of keyring ends a session there and nothing here, so an already
minted token works until it expires. **Nothing tells this service that an account is gone**:
the levers are local -- ``delete_user`` is the person's own, the database file the operator's.

Two messages are re-exported because they are contract: every token refusal says
:data:`BAD_TOKEN`, and the health check reports :data:`KEYS_UNAVAILABLE`.
"""

from __future__ import annotations

from keyring_client import BAD_TOKEN, KEYS_UNAVAILABLE, JwksClient

__all__ = ["BAD_TOKEN", "KEYS_UNAVAILABLE", "JwksClient"]
