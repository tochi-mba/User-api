"""FastAPI dependency wiring.

The container is built once at startup and parked on the app; these turn it into typed
parameters so handlers never reach into application state themselves.

:data:`IdentityDep` is the important one, and it is where the first of this service's
properties is enforced rather than merely intended: **no account id appears in any path,
ever.** The record is addressed as ``/v1/user``, and which record that is comes out of the
verified token's ``sub``. There is no ``/v1/users/{account_id}``, no query parameter naming
a subject, and therefore no cross-account read that could be written by mistake -- it
cannot be expressed. This is keyring's ``/v1/internal`` trick applied to a whole surface.

The scope comes from the same place, for the same reason. A caller that declared its own
scope in a parameter would be asking politely; the audience is signed, and keyring will
only mint one against a session, which is a thing the person has and an assistant does not.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from user_api.auth.tokens import Identity
from user_api.core.container import Container
from user_api.core.context import set_account_id
from user_api.domain.errors import AuthenticationError

bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "A short-lived signed token from keyring's `create_service_token`, minted with "
        "audience `user` or `user.<scope>`."
    ),
)
"""``auto_error=False`` so a missing header raises our error, in our problem+json shape.

Left to itself, HTTPBearer raises a bare 403 with a plain JSON body -- a different status
and a different shape from every other failure this service produces, which a caller then
has to special-case.
"""

MISSING_CREDENTIALS = "a token is required"


def get_container(request: Request) -> Container:
    """Return the container assembled during startup."""
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_identity(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Identity:
    """Turn the bearer token into the only identity this service ever learns.

    Binds the account into the request context as a side effect, so every log record
    produced by the rest of the request says who it was for without any handler passing it
    along. Context variables are task-local, so one person's request can never see
    another's binding -- there is a concurrency test for exactly that.

    Raises:
        AuthenticationError: no token, or one that is not accepted -- expired, forged, for
            another service, from another issuer, or naming a scope this deployment does
            not have. All of them look identical on the wire.
        KeyringUnreachableError: the public keys could not be fetched, so the token could
            not be checked either way. A 503, not a 401: the token may be perfectly good.
    """
    if credentials is None:
        raise AuthenticationError(MISSING_CREDENTIALS)

    identity = await container.verifier.verify(credentials.credentials)
    set_account_id(identity.account_id)
    return identity


IdentityDep = Annotated[Identity, Depends(get_identity)]
