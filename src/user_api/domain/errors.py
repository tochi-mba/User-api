"""Domain error vocabulary.

These describe what went wrong in business terms. Translating them into HTTP status codes
is the API layer's job -- nothing here knows what a status code is.

Two rules shape the module, and they pull in opposite directions, so it is worth being
explicit about where the line is.

**Across accounts, nothing is distinguishable.** Another account's entry reads back
exactly like one that never existed: :class:`EntryNotFoundError`, 404. There is no
"belongs to someone else". This is keyring's rule and it is not negotiable.

**Within one account, a refusal may say what it refused.** The caller here is the person's
own assistant, holding a token the person minted for it. Telling it "that scope is not in
your token" is telling it about *itself*, and a caller that cannot tell "refused" from
"absent" retries forever. So :class:`ScopeNotGrantedError` is specific -- and
:class:`ScopeConflictError` deliberately admits that a field exists outside the caller's
scope, which is the one place this service trades a little concealment for a caller that
can act on the answer. ADR-0004 argues that trade.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error this package raises deliberately."""


class AuthenticationError(DomainError):
    """A token was not accepted.

    Deliberately undifferentiated. Whether the signature is wrong, the audience names
    another service, the issuer is not keyring, the token expired or a required claim is
    missing, the caller is told the same thing -- because each distinction is an oracle
    that helps somebody forge the next one. The specific reason goes to the logs, where
    only the operator reads it.
    """


class KeyringUnreachableError(DomainError):
    """Keyring's public keys could not be fetched, so no token can be verified.

    Distinct from :class:`AuthenticationError` because it is *our* failure, not the
    caller's: the token may be perfectly good and we cannot tell. It is a 503 with a
    Retry-After, not a 401, so a caller retries rather than throwing its token away and
    sending a person back through a login it did not need.
    """


class ScopeNotGrantedError(DomainError):
    """The caller asked to read or write a scope its token does not carry.

    Safe to be specific: this is a fact about the caller's own token, not about whether
    anything exists. A token minted for ``user.home`` that asks to write a ``health``
    entry is told exactly that, because the fix -- mint a different token, which requires
    a keyring session the assistant does not have -- is the person's to make.
    """


class ScopeConflictError(DomainError):
    """A field with this key already exists, outside what this token may see.

    The awkward case, and the one place this service says more than it strictly must. A
    ``user.home`` token doing ``PUT /v1/user/fields/blood_type`` cannot be allowed to
    overwrite a health-scoped value it cannot read, and cannot be told "created" when
    nothing was. It could be told 404, but a 404 on a PUT that would have succeeded a
    moment ago is a caller that retries forever.

    So it is told there is a conflict. What leaks is the existence of a key -- not its
    value, not its scope. ADR-0004 records the trade.
    """


class EntryNotFoundError(DomainError):
    """No such entry, for this account, within this token's scope.

    One error for four situations -- it never existed, it belongs to another account, it
    is forgotten, or it is scoped away from this token -- because the caller must not be
    able to tell them apart. In particular a cross-account read gets this and not a 403:
    a 403 would confirm the entry exists.
    """


class InvalidKeyError(DomainError, ValueError):
    """A field key cannot be stored as given.

    Also a :class:`ValueError` so callers validating input with generic machinery catch it
    without importing this module.
    """


class InvalidValueError(DomainError, ValueError):
    """A field value is the wrong shape, too large, or too deeply nested."""


class InvalidNoteError(DomainError, ValueError):
    """A note body cannot be stored as given."""


class InvalidDescriptionError(DomainError, ValueError):
    """A description is missing or too long.

    Required on create, which is the whole anti-sprawl mechanism: the schema endpoint is
    only worth calling if what it returns says what each key means, and it only says that
    if writing one made you say it.
    """


class InvalidScopeError(DomainError, ValueError):
    """A scope name is not one this deployment recognises."""


class InvalidSearchError(DomainError, ValueError):
    """A search query contains nothing that could be matched.

    Raised for a query with no word characters at all -- ``"*"``, ``"()"``, ``"   "``.
    FTS5's MATCH has its own query syntax and most punctuation is a syntax error in it, so
    without this the caller gets a 500 from inside SQLite. See
    :mod:`user_api.domain.search`.
    """


class InvalidCursorError(DomainError, ValueError):
    """A pagination cursor is malformed, or belongs to a different ordering.

    The second half matters: a cursor encodes where it was in a particular sort order, and
    replaying it against another order would silently skip rows rather than fail.
    """


class CredentialRefusedError(DomainError, ValueError):
    """The value looks like a credential, and this is not where credentials go.

    Names keyring as the right home, because the caller is a model that will otherwise try
    again with the same value somewhere else. There is no override -- see
    :mod:`user_api.domain.secrets` for why the detector is deliberately conservative.
    """


class LimitExceededError(DomainError):
    """A per-account limit would be exceeded. Names the limit, so a caller can act on it."""


class PreferencesUnavailableError(DomainError):
    """A person's settings were needed and could not be read honestly.

    Either settings-api refused this service -- a grant it was not given, a token it does
    not recognise -- or it cannot be reached and the setting in question is one that must
    not be guessed at. Neither is the caller's doing, so it is not a 4xx.
    """
