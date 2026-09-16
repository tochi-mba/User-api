"""What one person has chosen, and what user-api does when it cannot ask.

The deployment's configuration says how user-api behaves for everybody. settings-api
holds what each person has chosen within that, and this module is the one place the two
meet: it turns a caller's token into the pin ceiling a write is held to, and the page
size a search uses when they name none. Nothing is read at startup, and with no
settings-api configured every person gets the configuration as it stands -- exactly what
user-api did before it read anybody's settings at all.

Three rules shape it.

**A person may narrow a ceiling and never raise it.** The deployment's cap on pinned
entries and the default search page still apply on top of what somebody chose; the
catalogue says so under those entries, and this is where that becomes true.
``search_max_limit`` is not theirs at all -- it is the hard cap a request that names a
page size is held to.

**An outage degrades per setting.** Both ``user`` entries this service reads fall back
to a default, and when settings-api has never answered, the configuration is that
default. ``erasure_mode``, ``grace_days`` and ``log_values`` stay on the SQL store:
the sweeper has no user token to present, and ``log_values`` is written by the public
PUT and read by the event log on the same row. Dual-writing it without a token for the
sweeper would be half-wiring.

**A refusal is not an outage.** settings-api answering 401 or 403 means this service is
misconfigured -- a missing grant, a wrong token -- and serving defaults would hide that
behind behaviour that happens to work. The request fails instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from settings_client import (
    HttpSettingsClient,
    SettingsRefused,
    SettingsRejected,
    SettingsUnavailable,
)

from user_api.core.logging import get_logger
from user_api.domain.errors import PreferencesUnavailableError

if TYPE_CHECKING:
    from settings_client import ResolvedSettings, SettingsClient

    from user_api.core.config import Settings

logger = get_logger(__name__)

NAMESPACE = "user"

REFUSED = "settings-api did not accept this service's request for your settings"
NOT_GUESSED = "one of your settings could not be read from settings-api and must not be guessed"


@dataclass(frozen=True, slots=True)
class Preferences:
    """One person's choices, as this service applies them to one request."""

    max_pinned: int
    """How many entries this person may pin into the always-load block."""

    search_default_limit: int
    """Rows a search, export or event page returns when the caller does not say."""


class PreferenceSource(Protocol):
    """Where a request's preferences come from."""

    async def for_token(self, user_token: str | None, /) -> Preferences:
        """The preferences of whoever ``user_token`` belongs to.

        ``None`` is no caller at all -- a path that does not authenticate -- and gets the
        configuration.

        Raises:
            PreferencesUnavailableError: settings-api refused this service, or cannot
                read a setting that must not be guessed.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever this holds open."""
        ...


def deployment_preferences(settings: Settings) -> Preferences:
    """What everybody gets when nobody's own choices are known: the configuration as it is."""
    return Preferences(
        max_pinned=settings.max_pinned,
        search_default_limit=settings.search_default_limit,
    )


class DeploymentPreferences:
    """Everybody gets the configuration: what user-api did before it read settings-api."""

    def __init__(self, settings: Settings) -> None:
        self._preferences = deployment_preferences(settings)

    async def for_token(self, _user_token: str | None, /) -> Preferences:
        return self._preferences

    async def aclose(self) -> None:
        """Nothing is held open."""


class SettingsApiPreferences:
    """Each person's own choices, read from settings-api, inside the deployment's ceilings."""

    def __init__(self, *, client: SettingsClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._deployment = deployment_preferences(settings)

    async def for_token(self, user_token: str | None, /) -> Preferences:
        if user_token is None:
            return self._deployment

        try:
            resolved = await self._client.resolve(NAMESPACE, user_token=user_token)
        except SettingsUnavailable:
            # Never answered, so not even settings-api's own defaults are known. The
            # configuration stands in for every key that falls back.
            logger.warning("settings_unavailable", namespace=NAMESPACE)
            return self._deployment
        except SettingsRejected as error:
            # The status only: settings-api's own detail names grants and namespaces, which
            # an operator reads in its log rather than a caller reading it in ours.
            logger.warning("settings_rejected", namespace=NAMESPACE, status_code=error.status_code)
            raise PreferencesUnavailableError(REFUSED) from error

        if resolved.stale:
            logger.info("settings_stale", namespace=NAMESPACE)

        try:
            return self._apply(resolved)
        except SettingsRefused as error:
            # No ``user`` entry this service reads refuses today. If one ever does,
            # carrying on with the configuration in its place is exactly the guess that
            # flag exists to prevent.
            logger.warning("settings_refused", namespace=NAMESPACE, key=error.key)
            raise PreferencesUnavailableError(NOT_GUESSED) from error

    async def aclose(self) -> None:
        await self._client.aclose()

    def _apply(self, resolved: ResolvedSettings) -> Preferences:
        """Turn one person's resolved namespace into the caps this request is held to."""
        settings = self._settings
        pinned = _whole_number(resolved, "max_pinned", minimum=1)
        search = _whole_number(resolved, "search_default_limit", minimum=1)
        return Preferences(
            max_pinned=_narrow(settings.max_pinned, pinned),
            search_default_limit=_narrow(
                settings.search_default_limit, search, ceiling=settings.search_max_limit
            ),
        )


def build_preference_source(
    settings: Settings, *, client: SettingsClient | None = None
) -> PreferenceSource:
    """Choose where preferences come from, and say which in the log.

    Args:
        settings: the configuration, which also says whether settings-api is in use.
        client: substituted by tests with :class:`settings_client.testing.FakeSettingsClient`,
            and used in place of building one from ``settings``.
    """
    if client is None:
        configured = settings.settings_api
        if configured is None:
            logger.info("per_person_settings_off")
            return DeploymentPreferences(settings)
        base_url, token = configured
        client = HttpSettingsClient(base_url=base_url, service_token=token.get_secret_value())

    logger.info("per_person_settings_on", namespace=NAMESPACE)
    return SettingsApiPreferences(client=client, settings=settings)


def _narrow(deployment: int, chosen: int | None, *, ceiling: int | None = None) -> int:
    """The deployment's cap, or the person's if they asked for less.

    ``ceiling`` is a second hard cap the person cannot raise either -- ``search_max_limit``
    for the default page, so an omitted limit can never return more rows than a named one
    is allowed to ask for.
    """
    value = deployment if chosen is None else min(deployment, chosen)
    return value if ceiling is None else min(value, ceiling)


def _whole_number(resolved: ResolvedSettings, key: str, *, minimum: int) -> int | None:
    """``key`` as a whole number no smaller than ``minimum``, or ``None`` if there is none.

    A deployment running an older settings-api may not have the key, and a value of the
    wrong shape is settings-api's bug rather than a reason to fail somebody's write.
    Either way the configuration stands in. The key is logged; the value never is.
    """
    value = resolved.get(key, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    if value is not None:
        logger.warning("setting_unusable", namespace=NAMESPACE, key=key)
    return None


__all__ = [
    "DeploymentPreferences",
    "PreferenceSource",
    "Preferences",
    "SettingsApiPreferences",
    "build_preference_source",
    "deployment_preferences",
]
