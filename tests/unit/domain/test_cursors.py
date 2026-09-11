"""Keyset pagination cursors: what they carry, and what they refuse to be read as.

Two properties are doing the work here. A cursor is opaque, so nobody builds one by hand
and starts relying on its shape; and a cursor names the ordering it was issued for, so
replaying it against a different one is an error rather than a silently wrong window.

The second is the one that would be expensive to get wrong. A relevance cursor carries a
bm25 score and a recency cursor carries an ISO timestamp, and ``WHERE sort_key < ?``
compares the two happily and meaninglessly -- an export that skips rows and looks exactly
like working software.
"""

from __future__ import annotations

import base64
import json
import string

import pytest

from user_api.domain.cursors import CURSOR_VERSION, Cursor, Ordering, decode_cursor
from user_api.domain.errors import InvalidCursorError

TIMESTAMP = "2026-03-14T09:30:00+00:00"
ENTRY_ID = "8d5f2c1b9a7e4d3c2b1a0f9e8d7c6b5a"

URL_SAFE = set(string.ascii_letters + string.digits + "-_")
"""Everything that may appear in a cursor, which is base64url with the padding removed."""

MISMATCHED_PAIRS = [
    (issued, walked)
    for issued in Ordering
    for walked in Ordering
    if issued is not walked
]


def encoded_payload(payload: object) -> str:
    """Encode an arbitrary payload the way :meth:`Cursor.encode` does.

    Hand-rolled rather than reusing the encoder, because these are the payloads the
    encoder cannot produce -- which is the whole point of testing the decoder against
    them.
    """
    packed = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(packed).rstrip(b"=").decode()


class TestRoundTrip:
    @pytest.mark.parametrize("ordering", list(Ordering))
    def test_a_cursor_survives_a_round_trip_in_the_ordering_that_issued_it(
        self, ordering: Ordering
    ) -> None:
        cursor = Cursor(ordering=ordering, sort_key=TIMESTAMP, entry_id=ENTRY_ID)

        assert decode_cursor(cursor.encode(), expected=ordering) == cursor

    def test_a_string_sort_key_comes_back_as_the_same_string(self) -> None:
        cursor = Cursor(ordering=Ordering.OLDEST, sort_key=TIMESTAMP, entry_id=ENTRY_ID)

        assert decode_cursor(cursor.encode(), expected=Ordering.OLDEST).sort_key == TIMESTAMP

    def test_a_float_sort_key_comes_back_as_the_same_float(self) -> None:
        # bm25 ranks are negative floats, and a cursor that rounded one to an int would
        # put the walk back somewhere it has already been.
        cursor = Cursor(ordering=Ordering.RELEVANCE, sort_key=-1.2345, entry_id=ENTRY_ID)

        assert decode_cursor(cursor.encode(), expected=Ordering.RELEVANCE).sort_key == -1.2345

    def test_a_whole_number_sort_key_is_accepted_as_a_number(self) -> None:
        # JSON does not distinguish 1 from 1.0, so a rank that happens to be whole comes
        # back as an int. Refusing it would refuse a cursor this service had issued.
        raw = encoded_payload({"v": CURSOR_VERSION, "o": "relevance", "k": 7, "i": ENTRY_ID})

        assert decode_cursor(raw, expected=Ordering.RELEVANCE).sort_key == 7


class TestOpacity:
    def test_a_cursor_does_not_show_its_contents_to_a_reader(self) -> None:
        # A caller that can read a cursor is a caller that will construct one, and then
        # the ordering tag stops being a safety check and starts being a thing to work
        # around.
        encoded = Cursor(
            ordering=Ordering.RECENT, sort_key=TIMESTAMP, entry_id=ENTRY_ID
        ).encode()

        assert ENTRY_ID not in encoded
        assert TIMESTAMP not in encoded

    def test_a_cursor_is_url_safe_so_it_needs_no_escaping_in_a_query_string(self) -> None:
        # The Greek letter is chosen because its UTF-8 bytes are exactly the ones
        # standard base64 renders as "+" and "/", both of which mean something else in a
        # query string.
        encoded = Cursor(ordering=Ordering.RECENT, sort_key="Ͽ", entry_id=ENTRY_ID).encode()

        assert set(encoded) <= URL_SAFE
        assert "-" in encoded or "_" in encoded

    def test_a_cursor_carries_no_padding(self) -> None:
        # "=" is legal in a query string but is percent-encoded by some clients and not
        # by others, and a cursor that came back re-spelled would not decode.
        encoded = Cursor(ordering=Ordering.RECENT, sort_key="Ͽ", entry_id=ENTRY_ID).encode()

        assert "=" not in encoded

    def test_an_unpadded_cursor_still_decodes(self) -> None:
        # The padding is stripped on the way out and restored on the way in. If those two
        # ever disagreed, every cursor this service issued would be rejected by it.
        for length in range(1, 8):
            cursor = Cursor(ordering=Ordering.RECENT, sort_key="x" * length, entry_id="e")

            assert decode_cursor(cursor.encode(), expected=Ordering.RECENT) == cursor


class TestTheOrderingCheck:
    @pytest.mark.parametrize(("issued", "walked"), MISMATCHED_PAIRS)
    def test_a_cursor_from_one_ordering_is_refused_by_another(
        self, issued: Ordering, walked: Ordering
    ) -> None:
        # The check the whole design turns on. Without it a bm25 float is compared to an
        # ISO timestamp, which SQLite does happily and meaninglessly.
        cursor = Cursor(ordering=issued, sort_key=TIMESTAMP, entry_id=ENTRY_ID)

        with pytest.raises(InvalidCursorError, match="different ordering"):
            decode_cursor(cursor.encode(), expected=walked)

    def test_the_refusal_says_which_ordering_to_restart_the_walk_with(self) -> None:
        # The caller cannot repair a cursor, so the only actionable thing to tell it is
        # which walk to start again.
        cursor = Cursor(ordering=Ordering.RELEVANCE, sort_key=-1.0, entry_id=ENTRY_ID)

        with pytest.raises(InvalidCursorError) as caught:
            decode_cursor(cursor.encode(), expected=Ordering.OLDEST)

        assert "oldest" in str(caught.value)

    def test_an_ordering_this_service_has_never_heard_of_is_refused(self) -> None:
        raw = encoded_payload({"v": CURSOR_VERSION, "o": "sideways", "k": 1.0, "i": ENTRY_ID})

        with pytest.raises(InvalidCursorError):
            decode_cursor(raw, expected=Ordering.RECENT)


class TestMalformedCursors:
    @pytest.mark.parametrize(
        "raw",
        [
            "!!!!",
            "a",
            "~~~~",
            "%%%",
            "zzz zzz",
            "not-a-cursor-at-all",
            "=",
            "\x00",
        ],
    )
    def test_a_cursor_that_is_not_base64_is_refused(self, raw: str) -> None:
        with pytest.raises(InvalidCursorError):
            decode_cursor(raw, expected=Ordering.RECENT)

    def test_base64_that_does_not_hold_json_is_refused(self) -> None:
        raw = base64.urlsafe_b64encode(b"not json at all").rstrip(b"=").decode()

        with pytest.raises(InvalidCursorError):
            decode_cursor(raw, expected=Ordering.RECENT)

    def test_base64_that_does_not_hold_text_is_refused(self) -> None:
        # Arbitrary bytes decode cleanly from base64 and then fail to be UTF-8, which is
        # a different exception from the one above and would otherwise be a 500.
        raw = base64.urlsafe_b64encode(b"\xff\xfe\xfd\xfc").rstrip(b"=").decode()

        with pytest.raises(InvalidCursorError):
            decode_cursor(raw, expected=Ordering.RECENT)

    @pytest.mark.parametrize("payload", [[1, 2, 3], "hello", 42, None, True, 1.5])
    def test_json_that_is_not_an_object_is_refused(self, payload: object) -> None:
        with pytest.raises(InvalidCursorError):
            decode_cursor(encoded_payload(payload), expected=Ordering.RECENT)

    @pytest.mark.parametrize("version", [0, 2, "1", None, 1.5])
    def test_a_cursor_from_another_version_of_the_payload_is_refused(
        self, version: object
    ) -> None:
        # A cursor is short-lived by nature -- nobody bookmarks page four -- so refusing
        # an old one outright is cheaper than a compatibility shim nobody will remove.
        raw = encoded_payload({"v": version, "o": "recent", "k": TIMESTAMP, "i": ENTRY_ID})

        with pytest.raises(InvalidCursorError):
            decode_cursor(raw, expected=Ordering.RECENT)

    @pytest.mark.parametrize(
        "payload",
        [
            {"v": CURSOR_VERSION, "o": "recent", "k": TIMESTAMP},
            {"v": CURSOR_VERSION, "o": "recent", "k": TIMESTAMP, "i": None},
            {"v": CURSOR_VERSION, "o": "recent", "k": TIMESTAMP, "i": 3},
            {"v": CURSOR_VERSION, "o": "recent", "k": TIMESTAMP, "i": ["e"]},
            {"v": CURSOR_VERSION, "o": "recent", "i": ENTRY_ID},
            {"v": CURSOR_VERSION, "o": "recent", "k": None, "i": ENTRY_ID},
            {"v": CURSOR_VERSION, "o": "recent", "k": ["x"], "i": ENTRY_ID},
            {"v": CURSOR_VERSION, "o": "recent", "k": {"x": 1}, "i": ENTRY_ID},
            {"v": CURSOR_VERSION, "k": TIMESTAMP, "i": ENTRY_ID},
            {},
        ],
    )
    def test_a_payload_missing_a_field_or_carrying_the_wrong_type_is_refused(
        self, payload: object
    ) -> None:
        with pytest.raises(InvalidCursorError):
            decode_cursor(encoded_payload(payload), expected=Ordering.RECENT)

    def test_every_malformed_cursor_is_refused_in_the_same_words(self) -> None:
        # A caller cannot act differently on the distinction between "not base64", "not
        # JSON" and "wrong version": the fix for all three is to start the walk again.
        reasons = set()
        for raw in ["!!!!", encoded_payload([1]), encoded_payload({"v": 2})]:
            with pytest.raises(InvalidCursorError) as caught:
                decode_cursor(raw, expected=Ordering.RECENT)
            reasons.add(str(caught.value))

        assert reasons == {"this cursor is not one we issued"}
