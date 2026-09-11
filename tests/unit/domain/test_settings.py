"""What a person has decided about their own data.

One setting, really -- what ``DELETE`` means -- and two questions the rest of the service
asks of it: does forgetting destroy the row now, and will it ever destroy it at all.
"""

from __future__ import annotations

import pytest

from user_api.domain.settings import ErasureMode, UserSettings


class TestDefaults:
    def test_a_person_who_has_said_nothing_gets_the_grace_window(self) -> None:
        """The default is the interesting part, because the failure modes are not symmetric.

        "It came back after I deleted it" is a broken promise. "I deleted it by mistake
        and had a month to say so" is a recovery. A person telling an assistant to forget
        something is often mid-conversation and sometimes wrong.
        """
        assert UserSettings().erasure_mode is ErasureMode.GRACE

    def test_the_grace_window_is_thirty_days(self) -> None:
        # Long enough to notice, short enough that "deleted" still means something.
        assert UserSettings().grace_days == 30

    def test_the_event_log_does_not_keep_old_values_unless_asked(self) -> None:
        # The event log is a second copy of the personal data: "changed diagnosis from X
        # to Y" is the sensitive fact, and an event recording it would outlive the entry
        # it describes unless something went looking. Off by default is a privacy
        # decision, not a storage one.
        assert UserSettings().log_values is False


class TestErasureModes:
    @pytest.mark.parametrize(
        ("mode", "purges_now"),
        [
            (ErasureMode.IMMEDIATE, True),
            (ErasureMode.GRACE, False),
            (ErasureMode.TOMBSTONE, False),
        ],
    )
    def test_only_immediate_destroys_the_row_inside_the_request(
        self, mode: ErasureMode, purges_now: bool
    ) -> None:
        assert UserSettings(erasure_mode=mode).purges_on_forget is purges_now

    @pytest.mark.parametrize(
        ("mode", "ever"),
        [
            (ErasureMode.IMMEDIATE, True),
            (ErasureMode.GRACE, True),
            (ErasureMode.TOMBSTONE, False),
        ],
    )
    def test_only_tombstone_keeps_the_bytes_for_ever(self, mode: ErasureMode, ever: bool) -> None:
        # The honest one to warn about: choosing tombstone means "delete" never destroys
        # anything, and the sweeper has nothing to do.
        assert UserSettings(erasure_mode=mode).ever_purges is ever

    def test_the_two_questions_between_them_describe_every_mode(self) -> None:
        # Three modes, three distinct answers. If two modes ever answered identically,
        # one of them would be a setting a person can choose and nothing would act on.
        answers = {
            (
                UserSettings(erasure_mode=mode).purges_on_forget,
                UserSettings(erasure_mode=mode).ever_purges,
            )
            for mode in ErasureMode
        }

        assert len(answers) == len(ErasureMode)

    @pytest.mark.parametrize(
        ("mode", "spelling"),
        [
            (ErasureMode.GRACE, "grace"),
            (ErasureMode.IMMEDIATE, "immediate"),
            (ErasureMode.TOMBSTONE, "tombstone"),
        ],
    )
    def test_a_mode_is_stored_and_returned_as_its_own_name(
        self, mode: ErasureMode, spelling: str
    ) -> None:
        # The value goes into a database column and comes back out on the wire.
        assert mode == spelling
