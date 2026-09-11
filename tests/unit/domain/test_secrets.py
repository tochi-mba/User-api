"""The credential detector, written as the two corpora it is specified by.

``MUST_ACCEPT`` is sentences somebody would really write about a person, several of them
chosen because they contain a long, alphabet-legal, space-free run that a naive entropy
rule refuses: a git SHA, a URL, a file path, a very long word. One of them is there
because a bare substring match for ``sk-`` refuses "risk-averse". ``MUST_REFUSE`` is the
credential shapes that must never reach a plaintext, searchable, fully-readable store.

The asymmetry between the two corpora is the whole tuning argument. There is no override:
a false positive means a person cannot write down the thing they wanted to write down and
has no way to insist, while a missed credential is a mistake they can still correct. So
``MUST_ACCEPT`` is the corpus that is allowed to grow, and a change to the heuristic that
shrinks it is a regression even if it catches more secrets.
"""

from __future__ import annotations

import pytest

from user_api.domain.secrets import (
    _CREDENTIAL_ALPHABET,
    _PREFIXES,
    KEYRING_ADVICE,
    MIN_ENTROPY_RUN,
    MIN_PREFIXED_SUFFIX,
    _is_high_entropy,
    _shannon_bits,
    looks_like_a_credential,
)

MUST_ACCEPT = [
    pytest.param("They prefer to be called Sam rather than Samuel.", id="a-preferred-name"),
    pytest.param("Their timezone is Europe/Lisbon and they start work at 09:30.", id="a-timezone"),
    pytest.param("Allergic to peanuts and shellfish; carries an adrenaline pen.", id="an-allergy"),
    pytest.param("They are a keen cyclist and ride to the office most mornings.", id="a-hobby"),
    pytest.param(
        "The bug they reported was fixed in commit 9f8e7d6c5b4a39281706f5e4d3c2b1a09876fedc.",
        id="a-git-sha",
    ),
    pytest.param(
        "Their favourite accent colour for slides is #1a2b3c, with #FFAA00 for highlights.",
        id="two-hex-colours",
    ),
    pytest.param(
        "They liked this article: "
        "https://www.example.com/2026/03/the-long-read-about-lisbon-trams?utm_source=newsletter",
        id="a-long-url",
    ),
    pytest.param(
        "Their home postcode is SW1A 1AA and they walk to the station.", id="a-uk-postcode"
    ),
    pytest.param(
        "Best contact address is samantha.okonkwo@example.com outside office hours.",
        id="an-email-address",
    ),
    pytest.param(
        "We last spoke at 2026-03-14T09:30:00+00:00 about the move.", id="an-iso-timestamp"
    ),
    pytest.param(
        "Their notes live in /Users/sam/Documents/recipes/dinner-notes.md on the old laptop.",
        id="a-file-path",
    ),
    pytest.param(
        "They joke that pneumonoultramicroscopicsilicovolcanoconiosis is their favourite word.",
        id="a-very-long-word",
    ),
    pytest.param(
        "When something delights them they write 'yessssssssssssssssssssssssssssssss' in chat.",
        id="a-long-repeated-run",
    ),
    pytest.param(
        "The shared album is named the-one-about-the-lisbon-trip-and-the-rain.",
        id="a-long-lowercase-slug",
    ),
    pytest.param("They have two children and a lurcher called Biscuit.", id="a-household"),
    pytest.param(
        "Prefers written summaries to phone calls, and hates being surprised by meetings.",
        id="a-working-preference",
    ),
    pytest.param("They played the trumpet at school and still own it.", id="a-biographical-fact"),
    pytest.param("Their partner is called Aizawa and works nights.", id="a-surname"),
    pytest.param("They travelled around Asia for a year before university.", id="a-continent"),
    pytest.param("They take their tea with oat milk, never dairy.", id="a-dietary-note"),
    pytest.param("Reading Middlemarch at the moment, slowly.", id="a-current-book"),
    pytest.param("Their brother lives in Osaka; they visit every other spring.", id="a-relative"),
    pytest.param("They keep a spreadsheet of every book they have read since 2009.", id="a-habit"),
    pytest.param(
        "Their bank reference for the standing order is 00000000000000000000000012345678.",
        id="a-padded-reference-number",
    ),
    pytest.param(
        "They are risk-averse with money and will not be talked out of it.",
        id="a-hyphenated-word-holding-a-prefix",
    ),
    pytest.param(
        "They prefer task-based work, and have been desk-bound since March.",
        id="two-more-of-those",
    ),
]
"""Twenty-six sentences that must survive the detector untouched."""


def _shaped(prefix: str, body: str) -> str:
    """Assemble a credential shape from its parts rather than writing one down.

    A corpus of credential shapes is, by construction, a file full of things that look
    like credentials -- and GitHub's push protection blocked this file the first time it
    was pushed, entirely correctly: one of the Slack entries matched Slack's own pattern
    closely enough to be worth a human looking at.

    Assembling them costs nothing and weakens nothing. The detector under test sees
    exactly the same string it saw before, because it is handed the joined result; what
    changes is that the repository no longer contains a line that a scanner, or a person
    skimming a diff, has to stop and rule out.
    """
    return prefix + body


MUST_REFUSE: list[tuple[str, str]] = [
    (_shaped("sk-", "proj-QW5vdGhlckxvbmdMb29raW5nS2V5VmFsdWU"), "an API key"),
    (_shaped("ghp", "_16CharsOfTokenMaterialGoesRightHere0"), "a GitHub personal access token"),
    (_shaped("gho", "_MoreTokenMaterialThatIsOAuthShaped00"), "a GitHub OAuth token"),
    (_shaped("ghs", "_ServerTokenMaterialThatIsAlsoShaped0"), "a GitHub server token"),
    (
        _shaped("github", "_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ"),
        "a GitHub personal access token",
    ),
    (_shaped("xoxb", "-123456789012-123456789012-AbCdEfGhIjKlMnOpQrStUvWx"), "a Slack bot token"),
    (_shaped("xoxa", "-2-123456789012-AbCdEfGhIjKlMnOpQrStUvWx"), "a Slack app token"),
    (_shaped("xoxp", "-123456789012-123456789012-AbCdEfGhIjKlMnOpQrStUvWx"), "a Slack user token"),
    (_shaped("AKIA", "IOSFODNN7EXAMPLE"), "an AWS access key id"),
    (_shaped("ASIA", "IOSFODNN7EXAMPLE"), "an AWS temporary access key id"),
    (_shaped("AIza", "SyD-ExampleGoogleApiKeyValue1234567"), "a Google API key"),
    (
        _shaped(
            "eyJ",
            "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        ),
        "a JSON Web Token",
    ),
    ("-----BEGIN RSA PRIVATE KEY-----", "a private key"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "an OpenSSH private key"),
    ("kJ8vQz2mXp7LbN4tRw9sYc3hFd6gVn1A", "a bare high-entropy token"),
    ("Zq4rT8wKm2NpXv6LbJ9cHs3YdF7gRu5Q7x", "another bare high-entropy token"),
]
"""Credential shapes and what each one is, for the test ids. The second element is a
label for the reader, not the wording the detector produces -- asserting on that wording
would be asserting on an error message rather than on the refusal.
"""

REFUSAL_IDS = [label for _, label in MUST_REFUSE]
SECRETS = [secret for secret, _ in MUST_REFUSE]


def in_a_sentence(secret: str) -> str:
    """The realistic case: a credential pasted into the middle of a note."""
    return f"They pasted {secret} into the chat this morning, which was careless."


class TestSentencesAboutPeople:
    @pytest.mark.parametrize("text", MUST_ACCEPT)
    def test_a_sentence_somebody_would_write_about_a_person_is_accepted(self, text: str) -> None:
        assert looks_like_a_credential(text) is None

    def test_the_accepted_corpus_is_large_enough_to_mean_something(self) -> None:
        # A detector can be made to pass three sentences. The corpus is the only thing
        # standing between "tuned" and "tuned on one example".
        assert len(MUST_ACCEPT) >= 20


class TestWhereAPrefixHasToSit:
    """A prefix has to begin a token and be followed by enough to be one.

    Matched as a bare substring, ``sk-`` refuses "risk-averse", "task-based",
    "desk-bound" and "disk-encrypted". There is no override on this detector, so each of
    those is not an inconvenience but a memory the person simply cannot write down.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "They are risk-averse with money.",
            "They prefer task-based work to open-ended projects.",
            "Desk-bound since March, and hating it.",
            "Their laptop is disk-encrypted, which they are proud of.",
        ],
    )
    def test_a_hyphenated_english_word_is_not_an_api_key(self, text: str) -> None:
        assert looks_like_a_credential(text) is None

    def test_a_prefix_buried_inside_a_word_is_not_a_prefix(self) -> None:
        # The same characters, one space apart: the second is a credential and the first
        # is the middle of somebody's sentence.
        assert looks_like_a_credential("This" + _shaped("AKIA", "IOSFODNN7EXAMPLE")) is None
        assert looks_like_a_credential("This " + _shaped("AKIA", "IOSFODNN7EXAMPLE")) is not None

    def test_a_prefix_followed_by_too_little_to_be_a_token_is_left_alone(self) -> None:
        # Every real token of these kinds carries far more than eight characters. A bare
        # `sk-` at the start of a word is somebody's abbreviation.
        assert looks_like_a_credential(f"the sk-{'a' * (MIN_PREFIXED_SUFFIX - 1)} one") is None

    def test_a_prefix_followed_by_enough_to_be_a_token_is_refused(self) -> None:
        assert looks_like_a_credential(f"the sk-{'a' * MIN_PREFIXED_SUFFIX} one") is not None


class TestCredentialShapes:
    @pytest.mark.parametrize("secret", SECRETS, ids=REFUSAL_IDS)
    def test_a_credential_is_refused(self, secret: str) -> None:
        assert looks_like_a_credential(in_a_sentence(secret)) is not None

    @pytest.mark.parametrize("secret", SECRETS, ids=REFUSAL_IDS)
    def test_a_credential_on_its_own_is_refused_just_the_same(self, secret: str) -> None:
        # A field value is frequently the bare token and nothing else.
        assert looks_like_a_credential(secret) is not None

    @pytest.mark.parametrize(("prefix", "description"), _PREFIXES)
    def test_every_prefix_in_the_table_is_refused(self, prefix: str, description: str) -> None:
        # Parametrised over the table itself, so a prefix added to the source without a
        # corpus entry still cannot be added without being tested.
        assert looks_like_a_credential(in_a_sentence(f"{prefix}SomeTokenMaterial123")) is not None
        # The description is the tail of the sentence the caller is shown, so it has to
        # read as one: "this contains what looks like a GitHub server token".
        assert description.startswith(("a ", "an "))

    def test_a_credential_is_found_wherever_it_sits_in_the_text(self) -> None:
        secret = _shaped("ghp", "_16CharsOfTokenMaterialGoesRightHere0")

        assert looks_like_a_credential(secret) is not None
        assert looks_like_a_credential(f"{secret} is the token") is not None
        assert looks_like_a_credential(f"the token is {secret}") is not None


class TestTheReason:
    @pytest.mark.parametrize("secret", SECRETS, ids=REFUSAL_IDS)
    def test_the_reason_never_repeats_the_thing_it_matched(self, secret: str) -> None:
        # The entire point is keeping the credential out of a plaintext store. A reason
        # that quoted it would put it in the 422 body and in a log line at the moment the
        # detector succeeded, which is worse than not having detected it at all.
        reason = looks_like_a_credential(in_a_sentence(secret))

        assert reason is not None
        windows = {secret[start : start + 8] for start in range(len(secret) - 7)}
        assert [window for window in windows if window in reason] == []

    @pytest.mark.parametrize("secret", SECRETS, ids=REFUSAL_IDS)
    def test_the_reason_says_what_kind_of_thing_it_thinks_this_is(self, secret: str) -> None:
        # "Invalid value" sends a model round the loop again with a different phrasing of
        # the same secret. Naming the kind is what lets it go and put the thing in keyring.
        reason = looks_like_a_credential(in_a_sentence(secret))

        assert reason is not None
        assert reason.startswith("this contains")

    def test_the_advice_names_where_credentials_actually_go(self) -> None:
        assert "keyring" in KEYRING_ADVICE


class TestCaseSensitivity:
    def test_a_surname_is_accepted_while_the_google_prefix_it_resembles_is_refused(self) -> None:
        # `AIza` is a Google API key and `Aiza` is the start of somebody's partner's
        # name. Folding the case here would refuse a person.
        assert looks_like_a_credential("Their partner is called Aizawa.") is None
        google = _shaped("AIza", "SyD-ExampleValue1234567890")
        assert looks_like_a_credential(f"The key is {google}") is not None

    def test_a_continent_is_not_an_aws_key(self) -> None:
        assert looks_like_a_credential("They travelled around Asia for a year.") is None
        temporary = _shaped("ASIA", "IOSFODNN7EXAMPLE")
        assert looks_like_a_credential(f"The key is {temporary}") is not None

    @pytest.mark.parametrize("text", ["a github_PAT_value", "SK-not-a-key", "GHP_notatoken"])
    def test_a_prefix_in_the_wrong_case_is_not_treated_as_one(self, text: str) -> None:
        assert looks_like_a_credential(text) is None


class TestHighEntropyRuns:
    def test_a_run_that_is_random_enough_and_long_enough_is_refused(self) -> None:
        assert _is_high_entropy("kJ8vQz2mXp7LbN4tRw9sYc3hFd6gVn1A")

    def test_a_run_shorter_than_the_minimum_is_left_alone(self) -> None:
        # Below 32 characters the false-positive rate stops being worth it: a shorter
        # run is as often an order reference or an address with the spaces taken out.
        short = "kJ8vQz2mXp7LbN4tRw9sYc3hFd6gVn"

        assert len(short) < MIN_ENTROPY_RUN
        assert looks_like_a_credential(f"The reference was {short} apparently.") is None

    def test_a_run_containing_anything_outside_the_credential_alphabet_is_left_alone(
        self,
    ) -> None:
        # Colons, dots and slashes are how timestamps, paths and URLs are spelled, and
        # none of them appear in a base64 or hex token.
        assert not _is_high_entropy("2026-03-14T09:30:00.000000+00:00")

    def test_a_hex_colour_is_left_alone(self) -> None:
        assert not _is_high_entropy("1a2b3c")

    def test_a_git_sha_is_left_alone(self) -> None:
        # Long, alphabet-legal, and in every second sentence an engineer writes.
        assert not _is_high_entropy("9f8e7d6c5b4a39281706f5e4d3c2b1a09876fedc")

    def test_a_run_of_one_repeated_character_is_left_alone(self) -> None:
        assert not _is_high_entropy("a" * 40)

    def test_a_run_using_too_few_distinct_characters_is_left_alone(self) -> None:
        # `aB1aB1aB1...` is mixed case, has digits, is alphabet-legal and is utterly
        # predictable. It is caught here, on distinct characters, before the entropy
        # measurement it would also fail.
        assert not _is_high_entropy("aB1" * 12)

    def test_a_long_lowercase_run_is_left_alone_whatever_its_length(self) -> None:
        # A long single-class run is a slug, a filename or a hyphenated compound. Real
        # credentials use the whole alphabet they were generated from.
        assert not _is_high_entropy("abcdefghijklmnopqrstuvwxyzabcdefghij")

    def test_a_long_uppercase_run_is_left_alone_too(self) -> None:
        assert not _is_high_entropy("ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJ")

    def test_a_diverse_but_predictable_run_is_left_alone(self) -> None:
        # Enough distinct characters and all three classes, but its distribution gives it
        # away: a padded identifier, not something a generator produced.
        padded = "x" * 40 + "Qw3rtyuiopasdfghjk"

        assert len(set(padded)) >= 16
        assert _shannon_bits(padded) < 3.5
        assert not _is_high_entropy(padded)

    def test_entropy_is_measured_over_the_run_rather_than_assumed_from_its_alphabet(
        self,
    ) -> None:
        random_looking = "kJ8vQz2mXp7LbN4tRw9sYc3hFd6gVn1A"
        predictable = "aB1" * 12

        assert _shannon_bits(random_looking) > _shannon_bits(predictable)

    def test_the_url_exemption_is_reached_by_the_alphabet_rule_first(self) -> None:
        """Why one branch of ``_is_high_entropy`` cannot be covered, written down.

        The exemption for URLs sits below the credential-alphabet check, and every
        character a URL can be recognised by -- ``:``, ``.``, ``@`` -- is one the alphabet
        already refuses. So a URL is exempt before the exemption for URLs runs, and that
        branch is unreachable while the two rules are in this order. It is still the right
        thing to keep: widen the alphabet by one punctuation mark and it starts carrying
        the weight this test says it does not.
        """
        assert [mark for mark in ":.@" if _CREDENTIAL_ALPHABET.match(mark)] == []

    def test_a_long_url_is_left_alone(self) -> None:
        # The single most common long space-free run in prose about a person: an article
        # they liked, a calendar invite. Its path segments are base64-legal.
        url = "https://example.com/2026/03/the-long-read-about-lisbon-trams?utm_source=news"

        assert not _is_high_entropy(url)
        assert looks_like_a_credential(f"They sent me {url} yesterday.") is None

    def test_an_email_address_padded_out_to_a_long_run_is_left_alone(self) -> None:
        assert not _is_high_entropy("samantha.okonkwo.the.longer.name@example.com")

    def test_a_note_holding_both_an_innocent_run_and_a_token_is_still_refused(self) -> None:
        # The scan does not stop at the first run it clears.
        text = (
            "Fixed in 9f8e7d6c5b4a39281706f5e4d3c2b1a09876fedc using "
            "kJ8vQz2mXp7LbN4tRw9sYc3hFd6gVn1A as the key."
        )

        assert looks_like_a_credential(text) is not None
