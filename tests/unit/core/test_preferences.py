"""One person's choices, and what user-api does with them -- and without them.

Every test that reads settings-api here uses its shared fake, including with it switched
off, because the outage is the case most services forget and the one their users notice.
"""

from __future__ import annotations

from typing import Any

import pytest
from settings_client import Fallback, OnUnavailable
from settings_client.testing import FakeSettingsClient

from user_api.core.config import LogFormat, Settings
from user_api.core.logging import configure_logging
from user_api.core.preferences import (
    NOT_GUESSED,
    REFUSED,
    DeploymentPreferences,
    Preferences,
    SettingsApiPreferences,
    build_preference_source,
    deployment_preferences,
)
from user_api.domain.errors import PreferencesUnavailableError

USER_TOKEN = "a-user-token-from-keyring"
SETTINGS_API_TOKEN = "settings-api-token-for-user-api-tests01"

FALLBACKS = {
    "max_pinned": Fallback(default=40, on_unavailable=OnUnavailable.USE_DEFAULT),
    "search_default_limit": Fallback(default=20, on_unavailable=OnUnavailable.USE_DEFAULT),
}


@pytest.fixture(autouse=True)
def _logs() -> None:
    configure_logging(level="INFO", log_format=LogFormat.CONSOLE)


def settings_with(**overrides: Any) -> Settings:
    return Settings(**{"_env_file": None, **overrides})


def reading(client: FakeSettingsClient, **overrides: Any) -> SettingsApiPreferences:
    return SettingsApiPreferences(client=client, settings=settings_with(**overrides))


class TestWithoutSettingsApi:
    def test_the_configuration_is_what_everybody_gets(self) -> None:
        settings = settings_with(max_pinned=4, search_default_limit=7)

        assert deployment_preferences(settings) == Preferences(max_pinned=4, search_default_limit=7)

    async def test_nobody_is_asked_when_settings_api_is_not_configured(self) -> None:
        settings = settings_with()
        source = build_preference_source(settings)

        assert isinstance(source, DeploymentPreferences)
        assert await source.for_token(USER_TOKEN) == deployment_preferences(settings)
        await source.aclose()

    async def test_a_configured_settings_api_is_asked_per_person(self) -> None:
        source = build_preference_source(
            settings_with(
                settings_api_base_url="https://settings.test",
                settings_api_token=SETTINGS_API_TOKEN,
            )
        )

        assert isinstance(source, SettingsApiPreferences)
        await source.aclose()

    async def test_a_substituted_client_is_the_one_asked(self) -> None:
        client = FakeSettingsClient()

        await build_preference_source(settings_with(), client=client).for_token(USER_TOKEN)

        assert client.resolves == 1


class TestAPersonsChoices:
    async def test_they_become_the_caps_their_writes_are_held_to(self) -> None:
        client = FakeSettingsClient()
        client.seed("user", {"max_pinned": 3, "search_default_limit": 5})

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences == Preferences(max_pinned=3, search_default_limit=5)

    async def test_a_ceiling_can_be_narrowed_and_never_raised(self) -> None:
        client = FakeSettingsClient()
        client.seed("user", {"max_pinned": 40, "search_default_limit": 80})

        preferences = await reading(client, max_pinned=4, search_default_limit=10).for_token(
            USER_TOKEN
        )

        assert preferences == Preferences(max_pinned=4, search_default_limit=10)

    async def test_the_search_default_cannot_exceed_the_hard_cap(self) -> None:
        client = FakeSettingsClient()
        client.seed("user", {"search_default_limit": 80})

        preferences = await reading(client, search_default_limit=50, search_max_limit=50).for_token(
            USER_TOKEN
        )

        assert preferences.search_default_limit == 50

    async def test_a_request_with_no_caller_asks_nobody(self) -> None:
        client = FakeSettingsClient()
        settings = settings_with()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(None)

        assert preferences == deployment_preferences(settings)
        assert client.resolves == 0


class TestWhenSettingsApiCannotBeReached:
    async def test_never_having_answered_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.unavailable = True
        settings = settings_with(max_pinned=4)

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences == deployment_preferences(settings)

    async def test_known_fallbacks_are_used_inside_the_deployment_ceilings(self) -> None:
        client = FakeSettingsClient(fallbacks={"user": FALLBACKS})
        client.unavailable = True

        preferences = await reading(client, max_pinned=4).for_token(USER_TOKEN)

        assert preferences.max_pinned == 4
        assert preferences.search_default_limit == 20

    async def test_a_user_setting_that_refuses_fails_rather_than_being_guessed(self) -> None:
        refusing = {"max_pinned": Fallback(default=40, on_unavailable=OnUnavailable.REFUSE)}
        client = FakeSettingsClient(fallbacks={"user": refusing})
        client.unavailable = True

        with pytest.raises(PreferencesUnavailableError, match=NOT_GUESSED):
            await reading(client).for_token(USER_TOKEN)


class TestWhenSettingsApiRefusesThisService:
    async def test_the_refusal_is_not_hidden_behind_defaults(self) -> None:
        client = FakeSettingsClient()
        client.rejects["user"] = (403, "user-api was not granted user")

        with pytest.raises(PreferencesUnavailableError) as caught:
            await reading(client).for_token(USER_TOKEN)

        assert str(caught.value) == REFUSED
        assert "granted" not in str(caught.value)

    async def test_the_refusal_logs_the_status_and_never_the_detail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeSettingsClient()
        client.rejects["user"] = (403, "user-api was not granted user")

        with pytest.raises(PreferencesUnavailableError):
            await reading(client).for_token(USER_TOKEN)

        output = capsys.readouterr().out
        assert "403" in output
        assert "granted" not in output
        assert USER_TOKEN not in output


class TestValuesThatCannotBeUsed:
    @pytest.mark.parametrize("value", [True, "3", 0, -1, None])
    async def test_an_unusable_pin_ceiling_leaves_the_configuration(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("user", {"max_pinned": value})

        preferences = await reading(client, max_pinned=4).for_token(USER_TOKEN)

        assert preferences.max_pinned == 4

    @pytest.mark.parametrize("value", [True, "3", 0, -1, None])
    async def test_an_unusable_search_default_leaves_the_configuration(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("user", {"search_default_limit": value})

        preferences = await reading(client, search_default_limit=7).for_token(USER_TOKEN)

        assert preferences.search_default_limit == 7

    async def test_a_missing_setting_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("user", {})
        settings = settings_with()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences == deployment_preferences(settings)

    async def test_the_key_is_logged_and_the_value_never_is(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeSettingsClient()
        client.seed("user", {"max_pinned": "a-value-nobody-should-read"})

        await reading(client).for_token(USER_TOKEN)

        output = capsys.readouterr().out
        assert "max_pinned" in output
        assert "a-value-nobody-should-read" not in output
        assert USER_TOKEN not in output
