"""Refusing to become a place people keep secrets.

keyring is next door and it is built for this: encrypted at rest, never returned by a read,
audited. This service is the opposite of all three -- plaintext on disk, returned in full
to any token whose scope permits it, and indexed for full-text search. An API key written
into a note would be a credential in a search index, which is about the worst place for
one.

So a value or a note body that looks like a credential is refused with a 422 naming keyring
as the right home. Not stripped, not masked, not stored-and-flagged: refused, so the caller
finds out immediately and puts it where it goes.

## Why the heuristic is deliberately timid

**There is no override.** A false positive blocks a legitimate memory, and the person on
the other end has no way to insist -- they just cannot write down the thing they wanted to
write down. That asymmetry sets the tuning: a missed credential is a mistake the person can
still correct, and a wrongly refused sentence is a feature that does not work.

So the entropy rule below demands *all* of length, a credential-shaped alphabet, high
character diversity, three character classes and genuine per-character entropy, and even
then it carves out the two shapes that keep coming up in real prose and pass every one of
those: a git hash and a hex colour.

Most of the things a reader expects to see exempted are not, because they never reach the
rule. A URL, an email address, a postcode and a phone number each contain a character the
credential alphabet does not admit -- a colon, an at-sign, a space -- so they are gone one
line earlier. There *was* an explicit URL exemption here; it was unreachable for exactly
that reason and was removed rather than left in as a comfort.

The specification for all of this is the pair of corpora in
``tests/unit/domain/test_secrets.py`` -- sentences that must be accepted, and credential
shapes that must be refused. Change the heuristic and the corpora are what tell you
whether you made it better or just different.
"""

from __future__ import annotations

import math
import re

KEYRING_ADVICE = "store credentials in keyring, not here"

MIN_ENTROPY_RUN = 32
"""Shorter than this and the false-positive rate stops being worth it.

A 32-character high-entropy blob is a token. A 24-character one is also sometimes a
Lisbon street address with the spaces removed, an order reference, or a filename.
"""

MIN_DISTINCT_CHARS = 16
"""How many different characters a run must use before it reads as random.

``aaaaaaaa...`` is long and base64-legal and is not a secret. Real tokens use most of
their alphabet; padded identifiers and repeated-character strings do not.
"""

REQUIRED_CHARACTER_CLASSES = 3
"""Lowercase, uppercase and digits -- all three, or it is not a token.

A long lowercase run is a hyphenated compound, a filename or a URL slug. A long uppercase
one is a reference number. Real credentials use the whole alphabet they were generated
from, and the ones that do not are caught by a prefix above.
"""

MIN_SHANNON_BITS = 3.5
"""Bits of entropy per character, over the run.

base64 tops out at 6 and English prose with no spaces sits near 3. The threshold is set
above prose and well below random, which is the gap the rule lives in.
"""

_PREFIXES: tuple[tuple[str, str], ...] = (
    ("sk-", "an API key"),
    ("ghp_", "a GitHub personal access token"),
    ("gho_", "a GitHub OAuth token"),
    ("ghs_", "a GitHub server token"),
    ("github_pat_", "a GitHub personal access token"),
    ("xoxb-", "a Slack bot token"),
    ("xoxa-", "a Slack app token"),
    ("xoxp-", "a Slack user token"),
    ("xoxs-", "a Slack workspace token"),
    ("xoxr-", "a Slack refresh token"),
    ("AKIA", "an AWS access key id"),
    ("ASIA", "an AWS temporary access key id"),
    ("AIza", "a Google API key"),
)
"""Known prefixes, checked case-sensitively and only at the start of a token.

Two rules, and both were added in response to a sentence that should have been accepted.

**Case matters.** ``aizawa`` is a surname and ``AIza`` is a Google API key, and folding the
case would refuse somebody's friend.

**Position matters more.** Matched as a bare substring, ``sk-`` refuses "They are
risk-averse with money", and so does every "task-based", "desk-bound" and "disk-encrypted"
sentence anybody will ever write. The prefix must therefore begin a token -- preceded by
the start of the string or by something that is not alphanumeric -- and be followed by
enough credential-shaped characters to be one. There is no override, so a false positive
here is a memory somebody simply cannot write down.
"""

MIN_PREFIXED_SUFFIX = 8
"""Characters a known prefix must be followed by before it reads as a credential.

Every real token of these kinds carries far more than eight. A bare ``sk-`` at the start of
a word is somebody's abbreviation.
"""

_PREFIXED = tuple(
    (
        re.compile(rf"(?<![A-Za-z0-9]){re.escape(prefix)}[A-Za-z0-9_\-]{{{MIN_PREFIXED_SUFFIX},}}"),
        description,
    )
    for prefix, description in _PREFIXES
)
"""The prefixes as anchored patterns. Built once, at import, rather than per call."""

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
"""Three base64url segments separated by dots, the first starting ``eyJ``.

Anchored on ``eyJ`` -- which is ``{"`` in base64 -- rather than on the bare three-segment
shape, because ``1.2.3-alpha.4`` and ``en.wikipedia.org`` are three dot-separated
base64url-legal segments and neither is a JWT.
"""

_RUNS = re.compile(rf"\S{{{MIN_ENTROPY_RUN},}}")

_HEX_COLOUR = re.compile(r"^#?[0-9a-fA-F]{3,8}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_CREDENTIAL_ALPHABET = re.compile(r"^[A-Za-z0-9+/=_-]+$")
"""The characters a base64 or hex credential is built from, and nothing else.

This is also, incidentally, the URL exemption. There was an explicit one here -- a check
for ``://``, a leading ``www.`` or an ``@`` -- added because a long article URL is the
single most common long space-free run in prose about a person. It was **unreachable**:
every character those patterns need is a colon, a dot or an at-sign, and this alphabet
admits none of the three, so a URL has already been let through one line earlier. It was
removed rather than left in as documentation, because the coverage gate is what found it
and a branch no input can take is a branch that stops being true without anybody noticing.
"""
_HAS_DIGIT = re.compile(r"\d")
_HAS_UPPER = re.compile(r"[A-Z]")
_HAS_LOWER = re.compile(r"[a-z]")


def looks_like_a_credential(text: str) -> str | None:
    """Return why ``text`` looks like a credential, or ``None`` if it does not.

    Returns the *reason* rather than a bool so the refusal can say which rule fired --
    "this looks like a GitHub personal access token" is actionable, "invalid value" sends
    the caller round the loop again with a different phrasing of the same secret.

    The reason names the kind of thing, never the matched text. The whole point is to keep
    the value out of the store; echoing it into an error body and a log line would defeat
    that at the moment of success.
    """
    for pattern, description in _PREFIXED:
        if pattern.search(text):
            return f"this contains what looks like {description}"

    if _PRIVATE_KEY.search(text):
        return "this contains what looks like a private key"

    if _JWT.search(text):
        return "this contains what looks like a JSON Web Token"

    for run in _RUNS.findall(text):
        if _is_high_entropy(run):
            return "this contains a long high-entropy string that looks like a secret"

    return None


def _is_high_entropy(run: str) -> bool:
    """Whether one space-free run reads as random rather than as language.

    Every condition must hold. The exemptions come first because they are the ones that
    were added in response to a real sentence being refused, and each is a shape that is
    long, alphabet-legal and diverse without being a secret.
    """
    if not _CREDENTIAL_ALPHABET.match(run):
        return False

    # Hex is its own case: a git SHA and a colour are both long, both alphabet-legal, and
    # neither is a credential. A 16-character alphabet also cannot reach the Shannon
    # threshold honestly, so this mostly documents the intent.
    if _HEX_COLOUR.match(run) or _GIT_SHA.match(run):
        return False

    if len(set(run)) < MIN_DISTINCT_CHARS:
        return False

    classes = sum(bool(pattern.search(run)) for pattern in (_HAS_DIGIT, _HAS_UPPER, _HAS_LOWER))
    if classes < REQUIRED_CHARACTER_CLASSES:
        return False

    return _shannon_bits(run) >= MIN_SHANNON_BITS


def _shannon_bits(run: str) -> float:
    """Bits of entropy per character, measured over the run itself.

    Measured over the string rather than assumed from its alphabet, which is what
    distinguishes ``aB1aB1aB1aB1...`` -- mixed case, digits, alphabet-legal, and utterly
    predictable -- from a token.
    """
    length = len(run)
    counts: dict[str, int] = {}
    for character in run:
        counts[character] = counts.get(character, 0) + 1
    return -sum((count / length) * math.log2(count / length) for count in counts.values())
