"""What a person has decided about their own data.

One setting, really -- what ``DELETE`` means -- plus the two knobs that follow from it.
The default is the interesting part, so the reasoning is here rather than in a comment on
the enum.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErasureMode(StrEnum):
    """What ``DELETE /v1/user/entries/{id}`` actually does."""

    GRACE = "grace"
    """Mark it forgotten now, destroy it later. The default.

    Invisible everywhere immediately -- every read path filters it out, so from the
    assistant's point of view it is gone the moment it is asked for. The bytes go when the
    sweeper next runs after ``grace_days``.

    Default because the two failure modes are not symmetric. "It came back after I deleted
    it" is a broken promise; "I deleted it by mistake and had a month to say so" is a
    recovery. A person telling an assistant to forget something is often mid-conversation
    and sometimes wrong, and thirty days is long enough to notice and short enough that
    "deleted" still means something.
    """

    IMMEDIATE = "immediate"
    """Destroy it in the request. No recovery, and the bytes are gone before the response.

    For people for whom the grace window is itself the problem.
    """

    TOMBSTONE = "tombstone"
    """Mark it forgotten and never purge.

    For people who would rather keep the record of what they changed their mind about. The
    honest one to warn about: choosing this means "delete" never destroys anything, and
    the ADR says so where somebody will read it.
    """


@dataclass(frozen=True, slots=True)
class UserSettings:
    """One account's choices. Read through a port; see :mod:`user_api.users.settings`."""

    erasure_mode: ErasureMode = ErasureMode.GRACE
    grace_days: int = 30
    log_values: bool = False
    """Whether the event log keeps the old value when something changes.

    Off by default, and this is a privacy decision rather than a storage one. The event
    log is a **second copy of the personal data**: "changed diagnosis from X to Y" is the
    sensitive fact, and an event log recording it would survive the purge of the entry it
    describes unless something went looking. With this off, events record what changed and
    who changed it, and never to what.

    Turned on, the old value is kept and is purged along with its entry -- so the promise
    still holds, it is just doing more work.
    """

    @property
    def purges_on_forget(self) -> bool:
        """Whether forgetting destroys in the request rather than scheduling it."""
        return self.erasure_mode is ErasureMode.IMMEDIATE

    @property
    def ever_purges(self) -> bool:
        """Whether a forgotten entry is ever destroyed at all."""
        return self.erasure_mode is not ErasureMode.TOMBSTONE
