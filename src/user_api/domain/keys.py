"""Field keys, and the normalisation that stops them multiplying.

This is the entire anti-sprawl mechanism on the write side, and it is three lines of
string handling doing a job that would otherwise need a taxonomy.

An assistant writing a field picks a name. Next week, a different conversation picks
``"Preferred Name"``. A month later, ``preferred-name``. Without normalisation those are
three fields, the newest of which is right and the older two of which are wrong and will
be read by something. With normalisation they are one field with three revisions, which is
what the person meant.

The other half of the mechanism is on the read side: ``GET /v1/user/schema`` is cheap
enough to call before inventing a key, and every key it returns carries the description
that was required when it was created. Normalisation makes reuse *possible*; the schema
endpoint makes it *likely*.

There is deliberately no fuzzy matching. ``preferred_name`` and ``name_preferred`` stay
two keys, because collapsing them needs a judgement this module has no business making,
and a store that silently merged two fields would be much worse than one that kept two.
"""

from __future__ import annotations

import re

from user_api.domain.errors import InvalidKeyError

MAX_KEY_LENGTH = 64

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
"""What a normalised key looks like. Starts with a letter, then lowercase, digits, ``_``.

Starting with a letter rather than a digit so a key is a legal identifier in whatever a
consumer renders it into, and so ``2fa_backup`` normalises to something that fails loudly
rather than to something subtly different.
"""

_SEPARATORS = re.compile(r"[\s\-]+")
_ILLEGAL = re.compile(r"[^a-z0-9_]+")
_REPEATS = re.compile(r"_{2,}")

WELL_KNOWN_KEYS: tuple[str, ...] = (
    "preferred_name",
    "pronouns",
    "timezone",
    "locale",
    "forms_of_address",
)
"""Keys an assistant should look for before inventing its own.

A documented convention, **not a schema**. Nothing enforces them, nothing requires them,
and an account that uses none of them is not malformed. They are listed in
``describe_schema`` output even when unset, which is the point: a model that needs to know
what to call somebody can find the answer without guessing at a name, and a model that
learns their pronouns has somewhere obvious to put them.
"""


def normalize_key(raw: str) -> str:
    """Fold a caller's field name into the one canonical form.

    ``"Preferred Name"``, ``"preferred-name"`` and ``"PREFERRED_NAME"`` are one key rather
    than three. The steps, in order: lowercase, turn whitespace and hyphens into
    underscores, drop everything that is still not legal, collapse runs of underscores,
    and strip them from the ends.

    Raises:
        InvalidKeyError: if nothing legal survives, if the result would start with a digit,
            or if it is longer than :data:`MAX_KEY_LENGTH`. Refused rather than truncated:
            a key silently cut at 64 characters could collide with a different key that
            happened to share a prefix, and the caller would never know.
    """
    folded = _REPEATS.sub("_", _ILLEGAL.sub("", _SEPARATORS.sub("_", raw.strip().lower()))).strip(
        "_"
    )

    if not folded:
        msg = f"{raw!r} contains no characters that can form a field key"
        raise InvalidKeyError(msg)

    if len(folded) > MAX_KEY_LENGTH:
        msg = (
            f"a field key may be at most {MAX_KEY_LENGTH} characters; "
            f"{raw!r} normalises to {len(folded)}"
        )
        raise InvalidKeyError(msg)

    if not KEY_PATTERN.match(folded):
        # Reachable only for a key that normalises to something starting with a digit --
        # every other illegal character is gone by now. Kept as a real check rather than
        # an assertion because it is the one case the substitutions above cannot fix.
        msg = f"a field key must start with a letter; {raw!r} normalises to {folded!r}"
        raise InvalidKeyError(msg)

    return folded
