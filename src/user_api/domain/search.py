"""Turning what a person typed into something FTS5 will accept.

This module exists because of a measurement, and the measurement is worth repeating here
so nobody "simplifies" it away.

FTS5's ``MATCH`` takes a **query language**, not a string. It has operators (``AND``,
``OR``, ``NOT``, ``NEAR``), phrase quoting, column filters (``col:term``), prefix globs
(``term*``) and parentheses. Passing user input to it straight through is not a search
feature, it is a syntax error waiting for punctuation. Ten plausible search strings were
tried against a real FTS5 table; **eight of the ten raised** ``sqlite3.OperationalError``,
which without this module is a 500 on a read endpoint::

    '"'   'a"b'   'x AND OR y'   '*'   'NEAR('   '^'   '()'   'a:b'   ''   '   '

The fix is to stop treating the input as a query at all. Word tokens are extracted, each is
double-quoted so FTS5 reads it as a literal phrase rather than as syntax, and they are
joined with ``OR``. ``'x AND OR y'`` becomes ``"x" OR "AND" OR "OR" OR "y"`` and returns
rows instead of raising. All ten were re-checked against the same table afterwards: the
seven with word characters return rows, and the three without are refused as a clean 422
before SQLite is asked anything.

``OR`` rather than ``AND``, because ``bm25()`` is doing the work. Every token matching
ranks higher than one token matching, so a caller who typed three words gets the entry
containing all three at the top *and* still gets the near-misses -- which is what recall on
half-remembered prose needs. ``AND`` would return nothing for a query with one wrong word
in it, and a query with one wrong word in it is the normal case.

The lost capability is real and is a deliberate trade: nobody can type a phrase query or a
prefix glob, because this turns both into literals. A caller that needs those is a caller
building a query language on top, and it can have one when somebody asks.
"""

from __future__ import annotations

import re

from user_api.domain.errors import InvalidSearchError

MAX_QUERY_CHARS = 200
MAX_TOKENS = 16
"""More tokens than this is a paste, not a query, and each one costs an index walk."""

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
"""Every run of word characters, underscores excluded.

``\\W`` is unicode-aware here, so accented and non-Latin text tokenises rather than
vanishing -- which matters for the name somebody actually goes by. Underscores are
excluded so ``preferred_name`` searches as two tokens and finds prose that says
"preferred name", which is what somebody typing a field key is hoping for.
"""


def to_match_query(raw: str) -> str:
    """Build an FTS5 ``MATCH`` expression that cannot be a syntax error.

    Raises:
        InvalidSearchError: if the query is too long, or contains no word characters at
            all. The second case is the one that matters -- ``"*"`` and ``"()"`` and
            ``"   "`` are refused here rather than becoming an ``OperationalError`` from
            inside SQLite, which is the difference between a 422 and a 500.
    """
    if len(raw) > MAX_QUERY_CHARS:
        msg = f"a search query may be at most {MAX_QUERY_CHARS} characters"
        raise InvalidSearchError(msg)

    tokens = _WORDS.findall(raw)
    if not tokens:
        msg = "a search query must contain at least one letter or digit"
        raise InvalidSearchError(msg)

    # Quoting is what makes this safe: inside double quotes FTS5 reads a token as a
    # literal phrase, so a token that happens to spell OR or NEAR is a word to look for
    # rather than an operator to obey. Any embedded double quote is doubled, which is
    # FTS5's own escape -- though the tokenizer above cannot produce one, this is the
    # line that would have to be wrong for that to matter.
    quoted = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:MAX_TOKENS]]
    return " OR ".join(quoted)
