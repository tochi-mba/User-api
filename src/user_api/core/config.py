"""Application configuration.

Every knob is an environment variable prefixed ``USER_API_``. Unknown variables under the
prefix are rejected rather than ignored (see :func:`check_for_unknown_env_vars`), so a
typo in a deployment surfaces at startup instead of silently leaving a security-relevant
default in place.

## What is deliberately absent

Keyring keeps a list of the settings it refused to add, and the habit is worth keeping,
because an absent setting is invisible in a diff and a present one is a single line away
from being set. There is:

* no setting that disables token verification;
* no setting that lets one account read another's record;
* no setting that turns off the credential refusal;
* no setting that accepts an unsigned or HS256 token;
* no setting by which a caller-supplied scope can widen what its token grants.

Each of those would be a one-variable route past the property the service is built
around. They are not configuration; they are the service.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from keyring_client import check_service_token
from pydantic import AfterValidator, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "USER_API_"
ENV_NESTED_DELIMITER = "__"

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


def _validated_service_token(value: SecretStr | None) -> SecretStr | None:
    """Refuse a token settings-api would never accept, without echoing it.

    Runs after wrapping as ``SecretStr``, so a validation error's input is the secret
    (asterisks), not the presented string.
    """
    if value is not None:
        check_service_token(value.get_secret_value())
    return value


ServiceToken = Annotated[SecretStr | None, AfterValidator(_validated_service_token)]


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """The complete runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        hide_input_in_errors=True,
    )

    # -- Identity ----------------------------------------------------------------------
    app_name: str = "user"
    environment: str = "local"

    # -- Observability -----------------------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- Serving -----------------------------------------------------------------------
    host: str = "127.0.0.1"
    """Loopback by default. This service belongs behind a TLS-terminating proxy."""

    port: PositiveInt = 8002

    # -- Storage -----------------------------------------------------------------------
    database_path: Path = Path("var/user.db")
    """The one file everything lives in.

    Nothing in it is encrypted. The file mode is therefore the only thing between this
    data and every other process on the box -- see ADR-0003, and
    :mod:`user_api.storage.database` for how 0600 is applied.
    """

    # -- Who we believe, and why -------------------------------------------------------
    keyring_jwks_url: str = "http://127.0.0.1:8001/.well-known/jwks.json"
    """Where keyring publishes the public half of its signing key.

    Fetched lazily and cached. This service never calls keyring at request time to ask
    *about* an account -- it only ever fetches keys, and only when it meets a ``kid`` it
    has not seen.
    """

    keyring_issuer: str = "http://127.0.0.1:8001"
    """Pinned against every token's ``iss``. A token from anywhere else is not a token."""

    audience_prefix: str = "user"
    """The family of audiences this service answers to.

    ``user`` alone grants nothing beyond unscoped entries; ``user.health`` grants
    the ``health`` scope. A token minted for ``example-tool`` is refused outright.
    """

    allowed_scopes: tuple[str, ...] = ("home", "work", "health", "family", "finance")
    """Every scope a token may name.

    Configuration rather than an open set, so an audience naming an unknown scope is a
    401 rather than a token that silently grants nothing and reads as a working
    configuration.
    """

    jwks_cache_seconds: PositiveFloat = 3_600.0
    jwks_min_refetch_seconds: PositiveFloat = 60.0
    """Floor between two fetches provoked by an unknown ``kid``.

    Without it, a stream of tokens carrying random ``kid`` values is one outbound fetch
    per request -- a DoS amplifier aimed at keyring, triggerable by anyone who can reach
    this service unauthenticated. There is a test for exactly that.
    """

    keyring_http_timeout_seconds: PositiveFloat = 5.0

    # -- Limits ------------------------------------------------------------------------
    max_entries_per_account: PositiveInt = 5_000
    max_fields_per_account: PositiveInt = 500
    max_pinned: PositiveInt = 40
    """The always-load block is a token budget before it is a preference."""

    max_value_bytes: PositiveInt = 4_096
    max_value_depth: PositiveInt = 3
    max_note_chars: PositiveInt = 4_000
    max_events: PositiveInt = 10_000
    """Per account. The log is trimmed to this in the same transaction that appends."""

    # -- Erasure -----------------------------------------------------------------------
    default_grace_days: NonNegativeInt = 30
    """How long a forgotten entry stays recoverable before the bytes go, by default.

    A per-account setting overrides it; this is only the value a new record starts with.
    """

    purge_interval_seconds: PositiveFloat = 3_600.0

    # -- Retrieval ---------------------------------------------------------------------
    search_default_limit: PositiveInt = 20
    search_max_limit: PositiveInt = 100

    # -- Per-person settings -----------------------------------------------------------
    settings_api_base_url: str | None = None
    """Where settings-api is. Unset, every person gets this configuration as it stands.

    Set, each request that needs a pin ceiling or a default search page reads that
    caller's ``user`` settings. The ceilings in this configuration still apply on top of
    what anybody chooses -- a person may narrow a cap and never raise it.

    ``erasure_mode``, ``grace_days`` and ``log_values`` stay on
    :class:`~user_api.users.sql_settings.SqlSettingsStore`. The erasure sweeper has no
    user token to present to settings-api, and ``log_values`` is written by the public
    PUT and read by the event log on the same row; dual-writing it without a token for
    the sweeper would be half-wiring.
    """

    settings_api_token: ServiceToken = None
    """This service's entry in settings-api's ``SETTINGS_API_SERVICES``.

    At least 32 characters, the rule settings-api enforces on its side. Its grant there
    needs ``audience_prefix`` equal to ``USER_API_AUDIENCE_PREFIX`` (``user`` unless the
    operator changed it): settings-api is shown the same user token keyring minted.
    """

    @property
    def settings_api(self) -> tuple[str, SecretStr] | None:
        """Where settings-api is and how to authenticate to it, or ``None`` when unused.

        One value rather than two optional ones, so that nothing downstream has to
        re-establish that the pair is whole: :meth:`_check_settings_api_is_whole` already
        refused to construct settings where it is not.
        """
        if self.settings_api_base_url is None or self.settings_api_token is None:
            return None
        return self.settings_api_base_url, self.settings_api_token

    @field_validator("database_path")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        """Resolve early so a relative path cannot mean two places after a chdir."""
        return value.expanduser().resolve()

    @field_validator("allowed_scopes")
    @classmethod
    def _check_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse a scope name that could not appear in an audience.

        The audience is ``{prefix}.{scope}``, so a scope containing a dot would make one
        audience parse as another. Caught at startup, where it is a typo, rather than at
        verification time, where it is an outage.
        """
        for scope in value:
            if not scope or not scope.replace("_", "").isalnum() or not scope.islower():
                msg = f"scope {scope!r} must be lowercase alphanumeric with underscores"
                raise ValueError(msg)
        if len(set(value)) != len(value):
            msg = "allowed_scopes contains a duplicate"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_limits(self) -> Self:
        if self.search_default_limit > self.search_max_limit:
            msg = "search_default_limit must not exceed search_max_limit"
            raise ValueError(msg)
        if self.max_pinned > self.max_entries_per_account:
            msg = "max_pinned must not exceed max_entries_per_account"
            raise ValueError(msg)
        if self.max_fields_per_account > self.max_entries_per_account:
            msg = "max_fields_per_account must not exceed max_entries_per_account"
            raise ValueError(msg)
        return self

    @field_validator("settings_api_base_url")
    @classmethod
    def _blank_is_unset(cls, value: str | None) -> str | None:
        """``USER_API_SETTINGS_API_BASE_URL=`` in a ``.env`` means off, not an empty URL."""
        return value or None

    @model_validator(mode="after")
    def _check_settings_api_is_whole(self) -> Self:
        """Refuse half a settings-api configuration, and a token that could never work.

        A URL with no token would be refused on every call, and a token with no URL is a
        secret configured for nothing. Either is somebody's mistake, and startup is the
        cheapest place to hear about it.
        """
        if (self.settings_api_base_url is None) != (self.settings_api_token is None):
            msg = "settings_api_base_url and settings_api_token must be set together"
            raise ValueError(msg)
        return self


class UnknownSettingError(ValueError):
    """A ``USER_API_``-prefixed variable is set that no setting corresponds to."""


def known_env_names(model: type[BaseModel] = Settings, prefix: str = ENV_PREFIX) -> set[str]:
    """Every environment variable name this configuration understands.

    Walks nested settings models, so a nested name would be recognised alongside the flat
    ones if one is ever added.
    """
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        env_name = f"{prefix}{field_name.upper()}"
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            names |= known_env_names(annotation, f"{env_name}{ENV_NESTED_DELIMITER}")
        else:
            names.add(env_name)
    return names


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Fail on a misspelled setting instead of quietly running with the default.

    pydantic-settings ignores prefixed variables it does not recognise, which for most
    services is a harmless convenience. Here it is not: ``USER_API_ALOWED_SCOPES`` would
    leave the scope list on its default with nothing in the logs to say so, and a
    deployment that believes it has compartmentalised an assistant would not have.

    Raises:
        UnknownSettingError: naming every unrecognised variable, so a deployment is fixed
            in one pass rather than one restart per typo.
    """
    present = environ if environ is not None else os.environ
    unknown = sorted(
        name for name in present if name.startswith(ENV_PREFIX) and name not in known_env_names()
    )
    if unknown:
        msg = f"unknown {ENV_PREFIX}* environment variables: {', '.join(unknown)}"
        raise UnknownSettingError(msg)


def load_settings() -> Settings:
    """Build settings from the environment and ``.env``."""
    check_for_unknown_env_vars()
    return Settings()
