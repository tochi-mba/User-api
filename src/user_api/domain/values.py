"""What may be stored in a field, and how big it may get.

A field value is JSON: a scalar, a list of scalars, or a shallow object. Not arbitrary
JSON, and the limits are not arbitrary either -- each one is a thing that goes wrong if it
is absent.

**Depth.** A deeply nested object in a field is a caller using this service as a document
store. It breaks rendering (nothing knows how to put it in a prompt), it breaks search (the
text is buried), and it breaks the schema endpoint (``value_type`` says ``object`` and
nothing more). Three levels is enough for ``{"home": {"city": "Lisbon"}}`` and not enough
for a config file.

**Size.** Every field comes back in the always-load block if it is pinned, and the
always-load block is a token budget. A 40 KB value in a pinned field is most of a context
window.

**Breadth.** A list of 100 or an object of 50 keys is generous for a fact about a person
and stingy for a data dump, which is the line this is drawing.

:func:`derive_value_type` is the reason ``value_type`` is never accepted from a caller: it
is computed from the value, so the stored pair cannot disagree with itself.
"""

from __future__ import annotations

import json

from user_api.domain.entries import ValueType
from user_api.domain.errors import InvalidValueError

MAX_LIST_ITEMS = 100
MAX_OBJECT_KEYS = 50

_SCALARS = (str, int, float, bool, type(None))


def derive_value_type(value: object) -> ValueType:
    """Work out which :class:`~user_api.domain.entries.ValueType` a value is.

    ``bool`` is checked before ``int`` deliberately: in Python ``True`` *is* an ``int``,
    and a boolean field reported as a number renders as ``1``, which is not what anybody
    wrote.

    Raises:
        InvalidValueError: for anything that is not JSON at all.
    """
    if isinstance(value, bool):
        return ValueType.BOOLEAN
    if value is None:
        return ValueType.NULL
    if isinstance(value, str):
        return ValueType.STRING
    if isinstance(value, int | float):
        return ValueType.NUMBER
    if isinstance(value, list):
        return ValueType.LIST
    if isinstance(value, dict):
        return ValueType.OBJECT

    msg = f"a field value must be JSON; {type(value).__name__} is not"
    raise InvalidValueError(msg)


def validate_value(value: object, *, max_bytes: int, max_depth: int) -> ValueType:
    """Check a value against every limit and return its type.

    Returns the type rather than ``None`` so a caller cannot validate and then derive the
    type separately -- which is two passes that can disagree after somebody edits one.

    Raises:
        InvalidValueError: too large, too deep, too many items, or not JSON. The message
            names the rule that failed and **never echoes the value**, because this
            message ends up in a 422 body and in a log line, and the value is the thing
            that must not be in either.
    """
    value_type = derive_value_type(value)
    _check_depth(value, limit=max_depth, depth=1)
    _check_breadth(value)

    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        # Reachable for things json.dumps rejects that the type check above admits --
        # NaN and Infinity are floats, and are not JSON.
        msg = "a field value must be JSON-serializable"
        raise InvalidValueError(msg) from exc

    size = len(encoded.encode())
    if size > max_bytes:
        msg = f"a field value may be at most {max_bytes} bytes serialized; this one is {size}"
        raise InvalidValueError(msg)

    return value_type


def _check_depth(value: object, *, limit: int, depth: int) -> None:
    """Refuse a structure nested past ``limit`` levels."""
    if not isinstance(value, dict | list):
        return

    if depth > limit:
        msg = f"a field value may be nested at most {limit} levels deep"
        raise InvalidValueError(msg)

    children = value.values() if isinstance(value, dict) else value
    for child in children:
        _check_depth(child, limit=limit, depth=depth + 1)


def _check_breadth(value: object) -> None:
    """Refuse a list or object with too many members, at any depth."""
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            msg = f"a list value may hold at most {MAX_LIST_ITEMS} items"
            raise InvalidValueError(msg)
        for item in value:
            _check_breadth(item)
    elif isinstance(value, dict):
        if len(value) > MAX_OBJECT_KEYS:
            msg = f"an object value may hold at most {MAX_OBJECT_KEYS} keys"
            raise InvalidValueError(msg)
        for key, item in value.items():
            if not isinstance(key, str):
                msg = "an object value must have string keys"
                raise InvalidValueError(msg)
            _check_breadth(item)


def searchable_text(value: object) -> str:
    """Render a value as the text the search index should match on.

    Structure is dropped and the leaves are kept: searching for ``Lisbon`` should find
    ``{"home": {"city": "Lisbon"}}``, and nobody wants to match on braces. Booleans and
    nulls contribute nothing -- ``true`` is not a word anybody searches for, and indexing
    it would make every boolean field a hit for it.
    """
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return " ".join(part for part in (searchable_text(item) for item in value) if part)

    # Narrowed rather than assumed: derive_value_type has already refused anything that
    # is not JSON, so this is a dict -- but saying so is what lets a type checker agree,
    # and an unchecked `.items()` here would be the one line that raises on a value the
    # validator let through.
    if not isinstance(value, dict):
        return ""

    pieces: list[str] = []
    for key, item in value.items():
        rendered = searchable_text(item)
        # The key is indexed too: a field holding {"city": "Lisbon"} should be findable
        # by "city" as well as by "Lisbon", because one of those is what somebody types.
        pieces.append(f"{key} {rendered}" if rendered else key)
    return " ".join(pieces)
