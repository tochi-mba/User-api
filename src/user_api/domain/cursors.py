"""Keyset pagination, and why it is not ``OFFSET``.

``LIMIT 20 OFFSET 40`` is one line shorter and quietly wrong. It counts rows from the
start of a result set that is being written to while you walk it: an entry added above
your position shifts everything down, so page three repeats a row page two already gave
you, and an entry removed above your position shifts everything up, so a row is skipped
and never seen at all.

For a search box that is a cosmetic glitch. For ``GET /v1/user/export`` on an account with
five thousand entries it is a corrupt export -- and "export everything you know about me"
is one of the two requests in this service that has to be exactly right.

So a cursor carries *where you were*, not *how far in*: the sort key and entry id of the
last row handed out. The next page is "strictly after that", which is a position in the
data rather than a count of rows, and is therefore stable against writes either side of it.

## Two orderings, and why the cursor names which

:data:`Ordering.RECENT` is what a person wants -- most recently touched first. It is not
stable under a concurrent *update*: a row you have not reached yet can be revised, jump to
the top, and be missed. That is an acceptable trade for a search box and an unacceptable
one for an export.

:data:`Ordering.OLDEST` sorts by ``created_at``, which never changes after the insert. A
row cannot move, so a walk cannot skip one. Export uses it, always, and a caller who needs
the guarantee more than they need recency can ask for it by name.

:data:`Ordering.RELEVANCE` is bm25 rank, and exists only when there is a query to rank
against.

The ordering is baked into the cursor and checked on the way back in. Replaying a cursor
from one ordering against another would compare a bm25 score to a timestamp and silently
return the wrong window -- an error that looks exactly like working software.
"""

from __future__ import annotations

import base64
import binascii
import json
from enum import StrEnum
from typing import NamedTuple

from user_api.domain.errors import InvalidCursorError

CURSOR_VERSION = 1
"""Bumped if the payload shape ever changes, so an old cursor is refused rather than
misread. A cursor is short-lived by nature -- nobody bookmarks page four -- so refusing
outright is cheaper than a compatibility shim nobody will remove.
"""


class Ordering(StrEnum):
    """How a page of entries is sorted. See the module docstring for the trade."""

    RECENT = "recent"
    """``updated_at`` descending. The default, and what a person means by "latest"."""

    OLDEST = "oldest"
    """``created_at`` ascending. Stable under concurrent writes; what export uses."""

    RELEVANCE = "relevance"
    """bm25 rank, best first. Only available with a query."""


class Cursor(NamedTuple):
    """A position in a result set: the last row handed out, and how it was sorted."""

    ordering: Ordering
    sort_key: str | float
    entry_id: str

    def encode(self) -> str:
        """Render as an opaque string a caller passes straight back.

        base64url of compact JSON. Opaque rather than readable on purpose -- a caller that
        can read a cursor is a caller that will construct one, and then the ordering tag
        below stops being a safety check and starts being a thing to work around.

        Not signed. A forged cursor can only move a caller around **its own** result set:
        the account filter and the scope filter are applied to the query regardless, so
        the worst a tampered cursor achieves is a page of the caller's own data in the
        wrong order. Signing it would cost a key to manage for no boundary gained.
        """
        payload = {
            "v": CURSOR_VERSION,
            "o": self.ordering.value,
            "k": self.sort_key,
            "i": self.entry_id,
        }
        packed = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        return base64.urlsafe_b64encode(packed).rstrip(b"=").decode()


def decode_cursor(raw: str, *, expected: Ordering) -> Cursor:
    """Parse a cursor and check it belongs to the ordering being walked.

    Raises:
        InvalidCursorError: malformed, wrong version, or from a different ordering. One
            error for all three: a caller cannot act differently on the distinction, and
            the fix for every one of them is to start the walk again.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        msg = "this cursor is not one we issued"
        raise InvalidCursorError(msg) from exc

    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        msg = "this cursor is not one we issued"
        raise InvalidCursorError(msg)

    ordering = payload.get("o")
    sort_key = payload.get("k")
    entry_id = payload.get("i")

    if not isinstance(entry_id, str) or not isinstance(sort_key, str | float | int):
        msg = "this cursor is not one we issued"
        raise InvalidCursorError(msg)

    if ordering != expected.value:
        # The check the whole design turns on. A relevance cursor replayed against a
        # recency walk would compare a negative bm25 score to an ISO timestamp, which
        # SQLite compares happily and meaninglessly.
        msg = f"this cursor was issued for a different ordering; restart the walk with {expected}"
        raise InvalidCursorError(msg)

    return Cursor(ordering=Ordering(ordering), sort_key=sort_key, entry_id=entry_id)
