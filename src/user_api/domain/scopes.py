"""Where a caller's scope comes from, and why it cannot come from anywhere else.

This is the second of the five properties the service is built around, and the whole of it
fits in one function.

A caller that declares its own scope in a query parameter is asking politely. That is a
filter, not a boundary: anything that can send ``?scope=home`` can send ``?scope=health``,
and a service that trusted it would be compartmentalising nothing. So the scope comes from
the token's ``aud`` claim, which is signed by keyring, and keyring will only mint one
against a session -- which is a thing the person has and an assistant does not.

The audience is a family::

    aud              granted scope    can read
    ---------------  --------------   ------------------------------------------
    user             (none)           unscoped entries only
    user.home        home             unscoped entries, and entries tagged home
    user.health      health           unscoped entries, and entries tagged health

One scope per token, not a set. A token that granted two scopes would be a token whose
holder can correlate across two compartments, which is most of what compartmentalising was
for -- and the person minting it can mint two tokens if they mean two.

An audience naming a scope this deployment does not recognise is refused outright rather
than treated as granting nothing. The difference matters: a typo in a mint request would
otherwise produce a token that works, reads only unscoped entries, and looks like a
correctly configured assistant that just has not been told anything yet.
"""

from __future__ import annotations

from user_api.domain.errors import InvalidScopeError, ScopeNotGrantedError

AUDIENCE_SEPARATOR = "."


def granted_scope(audience: str, *, prefix: str, allowed: tuple[str, ...]) -> str | None:
    """Work out which scope a token's audience grants, if any.

    Args:
        audience: the token's verified ``aud`` claim.
        prefix: the audience family this service answers to, e.g. ``user``.
        allowed: every scope this deployment recognises.

    Returns:
        The granted scope, or ``None`` for the bare prefix -- which grants nothing beyond
        entries that carry no scopes at all.

    Raises:
        InvalidScopeError: the audience is for another service, is malformed, or names a
            scope not in ``allowed``. The caller turns every one of these into the same
            undifferentiated 401, so the distinction is for the logs, not for the wire.
    """
    if audience == prefix:
        return None

    head, separator, scope = audience.partition(AUDIENCE_SEPARATOR)
    if not separator or head != prefix:
        # Includes the case that matters most: a token minted for `media-tool` presented
        # here. Verifying it against our own audience would have failed anyway, but this
        # is where it is named.
        msg = f"audience {audience!r} is not in the {prefix!r} family"
        raise InvalidScopeError(msg)

    if scope not in allowed:
        msg = f"audience {audience!r} names a scope this deployment does not recognise"
        raise InvalidScopeError(msg)

    return scope


def check_writable(scopes: tuple[str, ...], *, granted: str | None) -> None:
    """Refuse a write whose scopes reach past what this token carries.

    A token may write an entry that is unrestricted, or one tagged with the single scope it
    was granted. It may not tag an entry with a scope it cannot read.

    The asymmetry is worth stating, because "write up" is a thing some systems allow: a
    ``user.home`` token writing a ``health``-scoped entry would be creating data it cannot
    read back, cannot revise, and cannot verify it wrote correctly -- and would be doing it
    on the strength of a token the person minted for the *home* assistant. There is no
    case where that is what somebody meant.

    Raises:
        ScopeNotGrantedError: naming the scopes that were refused. Specific on purpose:
            this is a fact about the caller's own token, not about what exists.
    """
    permitted = {granted} if granted is not None else set()
    refused = sorted(set(scopes) - permitted)
    if refused:
        held = granted or "none"
        msg = f"this token grants {held} and cannot write entries scoped to {', '.join(refused)}"
        raise ScopeNotGrantedError(msg)


def check_filterable(scope: str, *, granted: str | None) -> None:
    """Refuse a ``?scope=`` filter that would reach past the token.

    The filter exists to *narrow* -- "only the work entries, please" -- and narrowing
    within what a token already grants is useful. Widening is the thing this whole module
    exists to prevent, so asking for it is refused loudly rather than quietly returning
    nothing.

    Quietly returning nothing was the tempting implementation, and it is worse: a caller
    that asked for something it may not have and got an empty page cannot tell that from
    "there is nothing there", so it caches the emptiness and stops asking.

    Raises:
        ScopeNotGrantedError: if ``scope`` is not the one this token was granted.
    """
    if scope != granted:
        held = granted or "none"
        msg = f"this token grants {held} and cannot filter for {scope}"
        raise ScopeNotGrantedError(msg)


def check_known(scopes: tuple[str, ...], *, allowed: tuple[str, ...]) -> None:
    """Refuse a scope name this deployment does not recognise.

    Checked before :func:`check_writable`, so a caller that misspells the scope it *does*
    hold is told it misspelled it rather than told it lacks permission.

    Raises:
        InvalidScopeError: naming the unrecognised scopes and what is available.
    """
    unknown = sorted(set(scopes) - set(allowed))
    if unknown:
        msg = f"unknown scopes: {', '.join(unknown)}; this deployment has {', '.join(allowed)}"
        raise InvalidScopeError(msg)
