"""Configuration: what the environment may say, and what it may never say.

Two of these are load-bearing rather than ordinary.

:class:`TestUnknownVariables` is the important one. pydantic-settings ignores a prefixed
variable it does not recognise, so a typo leaves a security-relevant setting on its
default with nothing in the logs to say so. ``USER_API_ALOWED_SCOPES`` would leave the
scope list wide open in a deployment that believes it has compartmentalised an assistant.

:class:`TestDeliberateAbsences` asserts things that are *not* there. An absent setting is
invisible in a diff and a present one is a single line away from being set, so the absence
is written down as a test rather than only as prose in the module docstring.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel, Field, ValidationError

from user_api.core.config import (
    ENV_PREFIX,
    LogFormat,
    Settings,
    UnknownSettingError,
    check_for_unknown_env_vars,
    known_env_names,
    load_settings,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


def build(**overrides: Any) -> Settings:
    """Settings built from arguments alone, with no ambient ``.env`` or environment."""
    values: dict[str, Any] = {"_env_file": None, **overrides}
    return Settings(**values)


@contextmanager
def environment(**values: str) -> Iterator[None]:
    """Set environment variables for the duration of the block, restoring them after.

    Hand-written rather than reached for from a plugin, so that a test which is *about*
    the environment carries its whole setup in this file.
    """
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, was in previous.items():
            if was is None:
                del os.environ[name]
            else:
                os.environ[name] = was


class _Nested(BaseModel):
    """A nested settings model, of the shape one would take if one were ever added."""

    time_cost: int = 1


class _WithNested(BaseModel):
    """A model with a nested one under it, for the name-walking test."""

    argon2: _Nested = Field(default_factory=_Nested)
    port: int = 8002


class TestDefaults:
    def test_it_binds_to_loopback_unless_somebody_says_otherwise(self) -> None:
        # This service holds a person's private record and terminates no TLS of its own,
        # so a public bind has to be a deliberate act rather than what happens by default.
        assert build().host == "127.0.0.1"

    def test_logs_are_json_so_a_deployment_can_query_them(self) -> None:
        assert build().log_format is LogFormat.JSON

    def test_the_default_limits_satisfy_their_own_cross_field_rules(self) -> None:
        settings = build()

        assert settings.search_default_limit <= settings.search_max_limit
        assert settings.max_pinned <= settings.max_entries_per_account
        assert settings.max_fields_per_account <= settings.max_entries_per_account


class TestUnknownVariables:
    def test_every_unknown_prefixed_variable_is_named_in_one_message(self) -> None:
        # One restart per typo is a bad way to fix a deployment, so the check names every
        # offender at once rather than dying on the first one it meets.
        with pytest.raises(UnknownSettingError) as caught:
            check_for_unknown_env_vars(
                {"USER_API_ALOWED_SCOPES": "home", "USER_API_MAX_PINED": "3", "PATH": "/usr/bin"}
            )

        assert "USER_API_ALOWED_SCOPES" in str(caught.value)
        assert "USER_API_MAX_PINED" in str(caught.value)

    def test_a_variable_outside_the_prefix_is_none_of_our_business(self) -> None:
        check_for_unknown_env_vars({"PATH": "/usr/bin", "HOME": "/root", "LANG": "en_GB.UTF-8"})

    def test_a_correctly_spelled_variable_passes(self) -> None:
        check_for_unknown_env_vars({"USER_API_PORT": "9000", "USER_API_LOG_LEVEL": "DEBUG"})

    def test_a_variable_read_from_the_environment_overrides_the_default(self) -> None:
        with environment(USER_API_PORT="9123"):
            assert load_settings().port == 9123

    def test_a_misspelled_variable_stops_startup_rather_than_leaving_the_default(self) -> None:
        # The failure this prevents: the scope list stays on its default, every audience
        # the deployment meant to forbid is accepted, and nothing anywhere says so.
        with (
            environment(USER_API_ALOWED_SCOPES="home"),
            pytest.raises(UnknownSettingError, match="USER_API_ALOWED_SCOPES"),
        ):
            load_settings()


class TestKnownNames:
    def test_every_flat_setting_has_an_environment_name(self) -> None:
        known = known_env_names()

        assert f"{ENV_PREFIX}PORT" in known
        assert f"{ENV_PREFIX}ALLOWED_SCOPES" in known
        assert f"{ENV_PREFIX}DATABASE_PATH" in known
        assert len(known) == len(Settings.model_fields)

    def test_a_nested_model_contributes_its_own_prefixed_names(self) -> None:
        """The walk into nested models, exercised on a model shaped like one.

        No setting is nested today, so this is the only thing standing between adding one
        and having :func:`check_for_unknown_env_vars` reject the very variable that
        configures it.
        """
        assert known_env_names(_WithNested, ENV_PREFIX) == {
            f"{ENV_PREFIX}ARGON2__TIME_COST",
            f"{ENV_PREFIX}PORT",
        }


class TestScopeNames:
    @pytest.mark.parametrize(
        "scope",
        [
            pytest.param("ho.me", id="a dot would make one audience parse as another"),
            pytest.param("Home", id="uppercase never appears in a minted audience"),
            pytest.param("", id="empty would produce the bare prefix with a trailing dot"),
            pytest.param("home work", id="a space is not a name"),
        ],
    )
    def test_a_scope_that_could_not_appear_in_an_audience_is_refused_at_startup(
        self, scope: str
    ) -> None:
        # The audience is `{prefix}.{scope}`, so `user.ho.me` would be parsed as the `me`
        # scope of a `user.ho` service. Caught here, where it is a typo, rather than at
        # verification time, where it is an outage.
        with pytest.raises(ValidationError, match="lowercase alphanumeric"):
            build(allowed_scopes=(scope,))

    def test_a_duplicated_scope_is_refused(self) -> None:
        # A duplicate is always a mistake, and a silent one: the list still works, so
        # whatever the second entry was meant to say is simply missing.
        with pytest.raises(ValidationError, match="duplicate"):
            build(allowed_scopes=("home", "work", "home"))

    def test_underscores_are_allowed_because_a_scope_name_may_need_two_words(self) -> None:
        assert build(allowed_scopes=("health_records",)).allowed_scopes == ("health_records",)


class TestCrossFieldLimits:
    def test_the_default_search_limit_may_not_exceed_the_maximum_one(self) -> None:
        # Otherwise every unparametrised search asks for more than the endpoint will ever
        # return, and the default silently becomes the maximum.
        with pytest.raises(ValidationError, match="search_default_limit"):
            build(search_default_limit=50, search_max_limit=20)

    def test_more_pins_than_entries_is_refused(self) -> None:
        # Only the pinned cap is out of line here; the field cap is set below the entry
        # cap on purpose so that this test fails for exactly one reason.
        with pytest.raises(ValidationError, match="max_pinned"):
            build(max_entries_per_account=10, max_pinned=11, max_fields_per_account=5)

    def test_more_fields_than_entries_is_refused(self) -> None:
        # A field *is* an entry, so a field cap above the entry cap is a cap that can
        # never be reached and an operator who believes it is in force.
        with pytest.raises(ValidationError, match="max_fields_per_account"):
            build(max_entries_per_account=10, max_fields_per_account=11, max_pinned=1)


class TestDatabasePath:
    def test_it_is_resolved_eagerly_so_a_chdir_cannot_move_it(self) -> None:
        # A relative path must not mean two different places before and after a chdir,
        # which for this file would mean a second, empty database and a person's record
        # apparently gone.
        assert build(database_path="var/user.db").database_path.is_absolute()

    def test_a_home_relative_path_is_expanded(self) -> None:
        expanded = build(database_path="~/user.db").database_path

        assert expanded == Path.home() / "user.db"


class TestDeliberateAbsences:
    """Settings that do not exist, asserted as settings that must not come to exist.

    Each of these would be a one-variable route past the property the service is built
    around, and each is the kind of thing that gets added at two in the morning to make an
    integration test pass. The absence is the feature, so it is pinned here.
    """

    @pytest.mark.parametrize(
        "fragment",
        ["disable", "insecure", "unsafe", "verify", "allow_any", "skip", "bypass", "trust"],
    )
    def test_no_setting_is_named_anything_that_could_turn_a_guarantee_off(
        self, fragment: str
    ) -> None:
        offenders = [name for name in Settings.model_fields if fragment in name]

        assert offenders == []

    def test_there_is_no_setting_that_lets_one_account_read_another(self) -> None:
        settings = build()

        assert not hasattr(settings, "allow_cross_account_reads")
        assert not hasattr(settings, "shared_account_ids")

    def test_there_is_no_setting_that_accepts_an_unsigned_or_symmetric_token(self) -> None:
        # The algorithm is pinned in code. A setting here would be an attacker's single
        # most valuable request of an operator.
        settings = build()

        assert not hasattr(settings, "allowed_algorithms")
        assert not hasattr(settings, "jwt_shared_secret")


SETTINGS_API_TOKEN = "settings-api-token-for-user-api-tests01"
SETTINGS_API_URL = "https://settings.test"


class TestSettingsApi:
    """Per-person settings are off unless configured, and configured whole or not at all."""

    def test_it_is_off_unless_configured(self) -> None:
        assert build().settings_api is None

    def test_a_base_url_and_a_token_together_turn_it_on(self) -> None:
        settings = build(
            settings_api_base_url=SETTINGS_API_URL, settings_api_token=SETTINGS_API_TOKEN
        )

        assert settings.settings_api is not None
        base_url, token = settings.settings_api
        assert base_url == SETTINGS_API_URL
        assert token.get_secret_value() == SETTINGS_API_TOKEN

    @pytest.mark.parametrize(
        "half",
        [
            {"settings_api_base_url": SETTINGS_API_URL},
            {"settings_api_token": SETTINGS_API_TOKEN},
        ],
    )
    def test_half_a_configuration_refuses_to_start(self, half: dict[str, str]) -> None:
        with pytest.raises(ValidationError, match="set together"):
            build(**half)

    def test_a_blank_base_url_means_off(self) -> None:
        assert build(settings_api_base_url="").settings_api_base_url is None

    def test_a_short_token_is_refused_without_being_echoed(self) -> None:
        with pytest.raises(ValidationError) as caught:
            build(settings_api_base_url=SETTINGS_API_URL, settings_api_token="short-token")

        # The message we raise never interpolates the presented value. pydantic's error
        # envelope may still name the input -- that is its record of what was passed, not
        # ours -- so the assertion is on the messages we wrote.
        messages = [error["msg"] for error in caught.value.errors()]
        assert messages
        assert all("short-token" not in message for message in messages)
        assert any("32" in message for message in messages)

    def test_the_token_does_not_render_itself(self) -> None:
        settings = build(
            settings_api_base_url=SETTINGS_API_URL, settings_api_token=SETTINGS_API_TOKEN
        )

        assert SETTINGS_API_TOKEN not in repr(settings)

    def test_the_prefixed_names_are_recognised(self) -> None:
        names = known_env_names()

        assert f"{ENV_PREFIX}SETTINGS_API_BASE_URL" in names
        assert f"{ENV_PREFIX}SETTINGS_API_TOKEN" in names
