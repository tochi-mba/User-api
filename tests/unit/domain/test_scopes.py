"""Where a caller's scope comes from, and why it cannot come from anywhere else.

The scope is read from the token's signed ``aud`` claim and from nowhere else. A caller
that declares its own scope in a query parameter is asking politely, and a service that
believed it would be compartmentalising nothing: anything that can send ``?scope=home``
can send ``?scope=health``.

So the tests here are mostly about refusals, and about the shape of each refusal. Two of
them are deliberate design decisions rather than omissions -- an audience naming an
unknown scope is an error rather than a token that grants nothing, and a filter for a
scope the token lacks is an error rather than an empty page -- and each has a test named
after the thing that goes wrong without it.
"""

from __future__ import annotations

import pytest

from user_api.domain.errors import InvalidScopeError, ScopeNotGrantedError
from user_api.domain.scopes import check_filterable, check_known, check_writable, granted_scope

PREFIX = "user"
ALLOWED = ("home", "work", "health")


class TestGrantedScope:
    def test_the_bare_prefix_grants_nothing_beyond_the_unscoped_entries(self) -> None:
        assert granted_scope("user", prefix=PREFIX, allowed=ALLOWED) is None

    @pytest.mark.parametrize("scope", ALLOWED)
    def test_a_family_member_grants_the_scope_it_names(self, scope: str) -> None:
        assert granted_scope(f"user.{scope}", prefix=PREFIX, allowed=ALLOWED) == scope

    @pytest.mark.parametrize(
        "audience", ["media-tool", "media-tool.home", "keyring", "", "userx", "user-health"]
    )
    def test_an_audience_for_another_service_is_refused(self, audience: str) -> None:
        # The case that matters most: a token minted for another service, presented here.
        # Verifying it against our own audience would have failed anyway, but this is
        # where it is named -- and the name is what an operator reads in the log.
        with pytest.raises(InvalidScopeError, match="family"):
            granted_scope(audience, prefix=PREFIX, allowed=ALLOWED)

    @pytest.mark.parametrize("audience", ["user.helth", "user.HEALTH", "user.", "user.home.work"])
    def test_an_audience_naming_an_unrecognised_scope_is_refused_rather_than_ignored(
        self, audience: str
    ) -> None:
        """A typo in a mint request must not produce a token that works.

        Treating an unknown scope as granting nothing is the tempting reading, and it is
        the dangerous one: the result is a token that authenticates, reads the unscoped
        entries, and looks exactly like a correctly configured assistant that has not
        been told anything yet. The person would be left wondering why their health
        assistant knows nothing about their health.
        """
        with pytest.raises(InvalidScopeError, match="does not recognise"):
            granted_scope(audience, prefix=PREFIX, allowed=ALLOWED)

    def test_a_scope_this_deployment_does_not_configure_is_refused_even_if_it_is_plausible(
        self,
    ) -> None:
        # `allowed` is deployment configuration, not a fixed vocabulary.
        with pytest.raises(InvalidScopeError):
            granted_scope("user.health", prefix=PREFIX, allowed=("home", "work"))

    def test_the_refusal_names_the_audience_so_an_operator_can_find_the_mint_request(
        self,
    ) -> None:
        with pytest.raises(InvalidScopeError) as caught:
            granted_scope("user.helth", prefix=PREFIX, allowed=ALLOWED)

        assert "user.helth" in str(caught.value)


class TestWriting:
    def test_an_unscoped_token_may_write_an_unscoped_entry(self) -> None:
        check_writable((), granted=None)

    @pytest.mark.parametrize("scope", ALLOWED)
    def test_an_unscoped_token_may_not_write_any_scope_at_all(self, scope: str) -> None:
        with pytest.raises(ScopeNotGrantedError, match="grants none"):
            check_writable((scope,), granted=None)

    def test_a_scoped_token_may_write_its_own_scope(self) -> None:
        check_writable(("home",), granted="home")

    def test_a_scoped_token_may_still_write_an_unscoped_entry(self) -> None:
        # Holding a scope narrows what a token can read, not what it is allowed to say
        # about the person in general.
        check_writable((), granted="home")

    def test_a_scoped_token_may_not_write_up_into_another_scope(self) -> None:
        """Write-up is refused deliberately, and this is the test that says so.

        A ``user.home`` token writing a ``health`` entry would be creating data it cannot
        read back, cannot revise and cannot verify it wrote correctly -- on the strength
        of a token the person minted for the *home* assistant. Some systems allow
        write-up. There is no case here where it is what somebody meant.
        """
        with pytest.raises(ScopeNotGrantedError):
            check_writable(("health",), granted="home")

    def test_a_write_naming_its_own_scope_and_another_is_refused_for_the_other(self) -> None:
        with pytest.raises(ScopeNotGrantedError) as caught:
            check_writable(("home", "health"), granted="home")

        assert "health" in str(caught.value)
        assert "home, health" not in str(caught.value)

    def test_the_refusal_names_every_scope_it_refused(self) -> None:
        # Specific on purpose: this is a fact about the caller's own token, not about
        # what exists. A caller that cannot tell "refused" from "absent" retries forever.
        with pytest.raises(ScopeNotGrantedError) as caught:
            check_writable(("health", "work"), granted="home")

        assert "health, work" in str(caught.value)
        assert "this token grants home" in str(caught.value)

    def test_a_scope_named_twice_is_refused_once(self) -> None:
        with pytest.raises(ScopeNotGrantedError) as caught:
            check_writable(("health", "health"), granted="home")

        assert str(caught.value).count("health") == 1


class TestFiltering:
    @pytest.mark.parametrize("scope", ALLOWED)
    def test_a_token_may_narrow_to_the_scope_it_holds(self, scope: str) -> None:
        check_filterable(scope, granted=scope)

    def test_a_filter_for_another_scope_is_an_error_and_not_an_empty_page(self) -> None:
        """Returning nothing was the tempting implementation, and it is worse.

        A caller that asked for something it may not have and got an empty page cannot
        tell that from "there is nothing there". So it caches the emptiness, stops asking,
        and the person is told their assistant knows nothing about their health.
        """
        with pytest.raises(ScopeNotGrantedError, match="cannot filter for health"):
            check_filterable("health", granted="home")

    def test_an_unscoped_token_cannot_filter_for_a_scope(self) -> None:
        with pytest.raises(ScopeNotGrantedError, match="grants none"):
            check_filterable("home", granted=None)


class TestKnownScopes:
    def test_the_scopes_this_deployment_configures_are_accepted(self) -> None:
        check_known(ALLOWED, allowed=ALLOWED)

    def test_naming_no_scopes_at_all_is_fine(self) -> None:
        check_known((), allowed=ALLOWED)

    def test_an_unknown_scope_is_refused_before_the_permission_check_runs(self) -> None:
        # Checked first so a caller that misspells the scope it does hold is told it
        # misspelled it, rather than told it lacks a permission it actually has.
        with pytest.raises(InvalidScopeError, match="unknown scopes: helth"):
            check_known(("helth",), allowed=ALLOWED)

    def test_the_refusal_lists_what_this_deployment_does_have(self) -> None:
        with pytest.raises(InvalidScopeError) as caught:
            check_known(("helth", "hoem"), allowed=ALLOWED)

        assert "helth, hoem" in str(caught.value)
        assert "home, work, health" in str(caught.value)
