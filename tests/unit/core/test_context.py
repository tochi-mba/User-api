"""Request-scoped context: the request id, and who the request is for.

:class:`TestTaskLocality` is the one that matters. The account id is bound once, at the
edge, and read by everything below without being threaded through a single signature --
which is only safe because a context variable belongs to the task that set it. If it
leaked across tasks, one person's request would be served with another person's identity,
and every other isolation test in this suite would be proving a property the service does
not have.
"""

from __future__ import annotations

import asyncio

from user_api.core import context
from user_api.core.context import (
    bind_account_id,
    bind_request_id,
    get_account_id,
    get_request_id,
    new_request_id,
    set_account_id,
)


class TestRequestId:
    def test_a_fresh_request_id_is_unique(self) -> None:
        assert new_request_id() != new_request_id()

    def test_nothing_is_bound_outside_a_request(self) -> None:
        assert get_request_id() is None
        assert get_account_id() is None

    def test_binding_exposes_the_value_for_the_duration_of_the_block(self) -> None:
        with bind_request_id("req-1") as bound:
            assert bound == "req-1"
            assert get_request_id() == "req-1"

        assert get_request_id() is None

    def test_leaving_a_nested_bind_restores_the_outer_one_rather_than_clearing_it(self) -> None:
        # An internal call made while serving a request binds its own id; the outer
        # request's id has to survive that, or the rest of its log lines lose their thread.
        with bind_request_id("outer"):
            with bind_request_id("inner"):
                assert get_request_id() == "inner"

            assert get_request_id() == "outer"

        assert get_request_id() is None


class TestAccountId:
    def test_binding_exposes_the_account_and_unwinds_after(self) -> None:
        with bind_account_id("account-a") as bound:
            assert bound == "account-a"
            assert get_account_id() == "account-a"

        assert get_account_id() is None

    def test_leaving_a_nested_bind_restores_the_outer_account(self) -> None:
        with bind_account_id("account-a"):
            with bind_account_id("account-b"):
                assert get_account_id() == "account-b"

            assert get_account_id() == "account-a"

    async def test_a_set_account_outlives_the_call_that_set_it(self) -> None:
        # This is what `set_account_id` is for: the binding is made by a FastAPI
        # dependency and has to still be there when the handler runs, which a context
        # manager could not do because it cannot span the two.
        set_account_id("account-a")

        assert get_account_id() == "account-a"

    def test_there_is_no_unbind_for_the_set_form_by_design(self) -> None:
        """The absence is deliberate, so it is asserted rather than merely documented.

        An unbind would be an invitation to clear the account mid-request and continue
        serving it as nobody. The binding ends when the task does, which is the only
        moment at which ending it is correct.
        """
        assert not hasattr(context, "unset_account_id")
        assert not hasattr(context, "clear_account_id")
        assert not hasattr(context, "reset_account_id")


class TestTaskLocality:
    async def test_two_concurrent_tasks_never_see_each_other_s_account(self) -> None:
        """The mechanism the whole isolation story rests on.

        Both tasks bind before either reads, which is the interleaving that would catch a
        shared global: whichever bound second would win and both would report it.
        """
        bound = [asyncio.Event(), asyncio.Event()]

        async def serve(account_id: str, mine: asyncio.Event, theirs: asyncio.Event) -> str | None:
            set_account_id(account_id)
            mine.set()
            await theirs.wait()
            return get_account_id()

        seen = await asyncio.gather(
            serve("account-a", bound[0], bound[1]),
            serve("account-b", bound[1], bound[0]),
        )

        assert list(seen) == ["account-a", "account-b"]

    async def test_an_account_set_inside_a_task_does_not_escape_into_its_parent(self) -> None:
        # This is why `set_account_id` needs no unbind: a worker serving the next request
        # gets a fresh context rather than the last request's identity.
        async def serve() -> None:
            set_account_id("account-a")

        await asyncio.gather(serve())

        assert get_account_id() is None
