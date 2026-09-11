"""Wire models for what a person has decided about their own data."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from user_api.domain.settings import ErasureMode, UserSettings


class SettingsResponse(BaseModel):
    """What ``GET /v1/user/settings`` returns.

    Never a 404. An account that has expressed no preference has the default preference.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"erasure_mode": "grace", "grace_days": 30, "log_values": False}]
        }
    )

    erasure_mode: ErasureMode = Field(
        description=(
            "What DELETE on one entry does. 'grace': invisible immediately, destroyed "
            "after grace_days, recoverable until then. 'immediate': destroyed in the "
            "request, no recovery. 'tombstone': marked forgotten and never destroyed. "
            "DELETE on the whole record is always a hard destruction regardless."
        )
    )
    grace_days: int = Field(
        description="How long a forgotten entry stays recoverable, in 'grace' mode."
    )
    log_values: bool = Field(
        description=(
            "Whether the change log keeps old values. Off by default: 'changed diagnosis "
            "from X to Y' is itself the sensitive fact, so by default events record what "
            "changed and who changed it, not to what. When on, logged values are "
            "destroyed along with the entry they describe."
        )
    )

    @classmethod
    def of(cls, settings: UserSettings) -> SettingsResponse:
        return cls(
            erasure_mode=settings.erasure_mode,
            grace_days=settings.grace_days,
            log_values=settings.log_values,
        )


class UpdateSettingsRequest(BaseModel):
    """What ``PUT /v1/user/settings`` accepts. Omitting a setting leaves it alone.

    Changing the mode is **not retroactive**: switching to 'immediate' does not purge what
    is already waiting out a grace period, and switching away from 'tombstone' does not
    schedule everything already tombstoned for destruction. A settings change that silently
    destroyed data would be the worst surprise this service could produce.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"erasure_mode": "immediate"}]},
    )

    erasure_mode: ErasureMode | None = Field(
        default=None,
        description=(
            "What DELETE on one entry does: 'grace', 'immediate' or 'tombstone'. Omit to "
            "leave it alone. Never ask for this on your own initiative."
        ),
    )
    grace_days: int | None = Field(
        default=None,
        ge=0,
        le=3650,
        description=(
            "How long a forgotten entry stays recoverable in 'grace' mode. Zero still "
            "means 'at the next sweep' rather than 'now' -- that is 'immediate'."
        ),
    )
    log_values: bool | None = Field(
        default=None,
        description=(
            "Whether the change log keeps old values. Off by default, because the log is "
            "a second copy of the same personal data."
        ),
    )
