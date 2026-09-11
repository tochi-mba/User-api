"""The error vocabulary, and the two properties that matter about it.

Every error shares one base, so a handler can catch the lot. The errors that are
*validation* failures are also :class:`ValueError`, so generic validation machinery catches
them without importing this module -- and the ones that are not validation failures
deliberately are not, because a 404 or a 401 that generic machinery turned into a 422
would be telling the caller something quite different from what happened.
"""

from __future__ import annotations

import pytest

from user_api.domain import errors
from user_api.domain.errors import (
    AuthenticationError,
    CredentialRefusedError,
    DomainError,
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
    ScopeConflictError,
    ScopeNotGrantedError,
)

BAD_INPUT = [
    InvalidKeyError,
    InvalidValueError,
    InvalidNoteError,
    InvalidDescriptionError,
    InvalidScopeError,
    InvalidSearchError,
    InvalidCursorError,
    CredentialRefusedError,
]
"""Errors that say "what you sent cannot be stored as given"."""

NOT_BAD_INPUT = [
    AuthenticationError,
    KeyringUnreachableError,
    ScopeNotGrantedError,
    ScopeConflictError,
    EntryNotFoundError,
    LimitExceededError,
]
"""Errors about who the caller is, what exists, or what this service could do."""

EVERY_ERROR = BAD_INPUT + NOT_BAD_INPUT

SITUATIONS_THAT_ARE_ALL_A_404 = (
    "the entry never existed",
    "the entry belongs to another account",
    "the entry has been forgotten",
    "the entry is scoped away from this token",
)

SIBLINGS_THAT_WOULD_BE_AN_ORACLE = (
    "EntryForbiddenError",
    "EntryForgottenError",
    "EntryOutOfScopeError",
    "CrossAccountError",
    "WrongAccountError",
)


class TestOneBaseToCatch:
    @pytest.mark.parametrize("error", EVERY_ERROR)
    def test_every_error_shares_one_base(self, error: type[Exception]) -> None:
        assert issubclass(error, DomainError)

    def test_the_list_in_this_file_is_the_whole_vocabulary(self) -> None:
        # So a new error cannot be added upstairs without somebody deciding here whether
        # it is a validation failure -- which is the decision that picks its status code.
        defined = {
            value
            for value in vars(errors).values()
            if isinstance(value, type) and issubclass(value, DomainError)
        }

        assert defined == {DomainError, *EVERY_ERROR}


class TestWhichOnesAreValueErrors:
    @pytest.mark.parametrize("error", BAD_INPUT)
    def test_an_error_about_the_input_is_also_a_value_error(self, error: type[Exception]) -> None:
        # So a caller validating input with generic machinery catches it without
        # importing this module.
        assert issubclass(error, ValueError)

    @pytest.mark.parametrize("error", NOT_BAD_INPUT)
    def test_an_error_that_is_not_about_the_input_is_not_a_value_error(
        self, error: type[Exception]
    ) -> None:
        # A 401 or a 404 swept up by a `except ValueError` that renders 422 would tell the
        # caller its request was malformed, which is both untrue and, for the 404, an
        # oracle: "malformed" and "not yours" are different answers.
        assert not issubclass(error, ValueError)

    @pytest.mark.parametrize("error", EVERY_ERROR)
    def test_an_error_carries_the_message_it_was_raised_with(self, error: type[Exception]) -> None:
        reason = "the reason"

        with pytest.raises(error) as caught:
            raise error(reason)

        assert str(caught.value) == reason


class TestNotFoundCoversFourSituations:
    def test_one_error_covers_every_way_an_entry_can_be_missing(self) -> None:
        """A documentation test, and the thing it documents is a security boundary.

        An entry that never existed, one belonging to another account, one that has been
        forgotten, and one scoped away from this token all raise
        :class:`EntryNotFoundError` and become the same 404. In particular a cross-account
        read gets this and not a 403, because a 403 would confirm the entry exists.
        """
        raised = [EntryNotFoundError("no such entry") for _ in SITUATIONS_THAT_ARE_ALL_A_404]

        assert len(raised) == 4
        assert {type(error) for error in raised} == {EntryNotFoundError}
        assert {str(error) for error in raised} == {"no such entry"}

    def test_there_is_no_second_error_that_would_tell_those_situations_apart(self) -> None:
        # The enforceable half of the test above: adding any of these names is how the
        # distinction would come back, and it would come back as a distinguishable status
        # code on a read path.
        assert [name for name in SIBLINGS_THAT_WOULD_BE_AN_ORACLE if hasattr(errors, name)] == []

    def test_a_scope_refusal_is_deliberately_not_the_same_as_a_missing_entry(self) -> None:
        # Within one account the rule reverses: telling a caller "that scope is not in
        # your token" is telling it about itself, and a caller that cannot tell "refused"
        # from "absent" retries forever.
        assert not issubclass(ScopeNotGrantedError, EntryNotFoundError)
        assert not issubclass(EntryNotFoundError, ScopeNotGrantedError)
