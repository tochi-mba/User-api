"""Translating exceptions into RFC 9457 problem responses.

The only place in the service that maps a failure to a status code, which is what keeps
the handlers thin: they raise domain errors and let this decide what that means over HTTP.

Three of the mappings are decisions rather than lookups.

**An entry the caller may not see is 404, never 403.** A 403 confirms the entry exists,
which across accounts would leak that somebody else has one and within an account would
tell a ``user.home`` assistant which health entries are there to be found. It is the same
answer a caller gets for an id nobody has ever used.

**A scope refusal is 403, and says so.** The exception: this is a fact about the *caller's
own token*, not about what exists, and a caller that cannot tell "refused" from "absent"
retries forever. :class:`~user_api.domain.errors.ScopeConflictError` is the deliberately
awkward 409 that goes with it -- ADR-0004 argues that trade.

**Keyring being unreachable is 503, not 401.** They are genuinely different: a 401 tells a
person to log in again, and they would be logging in again because *we* could not fetch a
public key. The Retry-After says come back rather than start over.

**An unexpected exception's text never reaches the caller.** It can carry a path, a
hostname, or a fragment of what somebody wrote down. The caller gets a request id to quote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from user_api.api.schemas.common import PROBLEM_CONTENT_TYPE, FieldError, Problem
from user_api.core.context import get_request_id
from user_api.core.logging import get_logger
from user_api.domain.errors import (
    AuthenticationError,
    CredentialRefusedError,
    EntryNotFoundError,
    InvalidCursorError,
    InvalidDescriptionError,
    InvalidKeyError,
    InvalidNoteError,
    InvalidScopeError,
    InvalidSearchError,
    InvalidValueError,
    KeyringUnreachableError,
    LimitExceededError,
    PreferencesUnavailableError,
    ScopeConflictError,
    ScopeNotGrantedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

logger = get_logger(__name__)

PROBLEM_BASE_URI = "https://user-api.invalid/problems"
RETRY_AFTER_HEADER = "Retry-After"
KEYRING_RETRY_AFTER = "5"

_STATUS_TITLES = {
    status.HTTP_400_BAD_REQUEST: "Bad request",
    status.HTTP_401_UNAUTHORIZED: "Unauthorized",
    status.HTTP_403_FORBIDDEN: "Forbidden",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Validation failed",
    status.HTTP_429_TOO_MANY_REQUESTS: "Too many requests",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "Internal server error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service unavailable",
}

# Domain errors that map cleanly onto a status code. Anything absent is a bug and becomes
# a 500 with its detail withheld.
_DOMAIN_STATUS: dict[type[Exception], int] = {
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    # Not 404. This is about the caller's own token rather than about what exists, so
    # being specific leaks nothing and saves a caller from retrying forever.
    ScopeNotGrantedError: status.HTTP_403_FORBIDDEN,
    # The one place this service says more than it strictly must: a field key is taken by
    # an entry this token cannot see. Not 404, because a 404 on a PUT that would have
    # succeeded a moment ago is a caller that never stops trying.
    ScopeConflictError: status.HTTP_409_CONFLICT,
    # Not 403. A 403 would confirm the entry exists, which is precisely the fact that must
    # not leak -- across accounts, and across scopes within one.
    EntryNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidKeyError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidValueError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidNoteError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidDescriptionError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidScopeError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidSearchError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    InvalidCursorError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    CredentialRefusedError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    LimitExceededError: status.HTTP_409_CONFLICT,
    # Not a 4xx. The caller did nothing wrong: this service is misconfigured, or a
    # setting that must not be guessed at could not be read. Serving deployment
    # defaults would hide a missing grant behind behaviour that happened to work.
    PreferencesUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}


# PLR0913: six keyword-only fields, because RFC 9457 has six fields. Grouping them into an
# object would add a type whose only job is to be unpacked one line later.
def problem_response(  # noqa: PLR0913
    *,
    status_code: int,
    detail: str,
    problem_type: str | None = None,
    title: str | None = None,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a problem+json response carrying the current request id."""
    slug = problem_type or _slug_for(status_code)
    problem = Problem(
        type=f"{PROBLEM_BASE_URI}/{slug}",
        title=title or _STATUS_TITLES.get(status_code, "Error"),
        status=status_code,
        detail=detail,
        request_id=get_request_id(),
        errors=errors,
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def _slug_for(status_code: int) -> str:
    return _STATUS_TITLES.get(status_code, "error").lower().replace(" ", "-")


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler the app needs. Called once, by the app factory."""

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Reshape FastAPI's validation errors into the one error format this API uses.

        Only the location and the message are copied. FastAPI's raw errors include the
        offending **input**, and on this service every request body is somebody's personal
        data -- so the default handler would put a rejected note body into a response, a
        client log and quite possibly an aggregator. There is a test that sends a sentinel
        value in a request that fails validation and asserts it appears nowhere.
        """
        return problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the request failed validation",
            problem_type="validation-failed",
            errors=[
                FieldError(
                    location=".".join(str(part) for part in error["loc"]),
                    message=error["msg"],
                )
                for error in exc.errors()
            ],
        )

    @app.exception_handler(KeyringUnreachableError)
    async def _keyring_down(_request: Request, exc: Exception) -> JSONResponse:
        """Say come back, not start over.

        A 401 here would tell a person to log in again because this service could not
        fetch a public key -- sending them through keyring for a problem that is not theirs
        and that logging in would not fix.
        """
        return problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            problem_type="keyring-unreachable",
            headers={RETRY_AFTER_HEADER: KEYRING_RETRY_AFTER},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(status_code=exc.status_code, detail=str(exc.detail))

    for error_type, status_code in _DOMAIN_STATUS.items():
        app.add_exception_handler(error_type, _domain_handler(status_code))


def unhandled_problem_response(exc: BaseException) -> JSONResponse:
    """Render an unexpected exception as a 500.

    The exception's own message is withheld: it can carry filesystem paths, internal
    hostnames, or a fragment of what somebody wrote down. The request id ties the response
    to the log record that does have the detail -- which is why this is invoked from inside
    :class:`~user_api.api.middleware.RequestContextMiddleware`, while the id is still
    bound, rather than from Starlette's outermost error middleware, where the binding has
    already unwound and the response would carry no id at all.

    Only the exception's **type name** is logged, never its arguments: a ``ValueError``
    raised deep in a write path routinely carries the value in its message.
    """
    logger.exception("unhandled_exception", error_type=type(exc).__name__)
    return problem_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="an unexpected error occurred; quote the request id when reporting it",
    )


def _domain_handler(
    status_code: int,
) -> Callable[[Request, Exception], Coroutine[Any, Any, JSONResponse]]:
    """Build a handler that renders a domain error at ``status_code``."""

    async def handler(_request: Request, exc: Exception) -> JSONResponse:
        return problem_response(status_code=status_code, detail=str(exc))

    return handler
