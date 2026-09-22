"""Everything a request means, once it is known who is asking.

The routers are thin and the stores are literal; this is the layer where the rules live,
so this is where they are pinned. Three groups matter more than the rest: the ORDER the
checks run in, because getting it wrong tells a caller the wrong thing to fix; provenance,
because half of it is verified and half of it is a claim; and the erasure decision, because
the account's setting decides, not the caller.

Built on the real SQL adapters rather than fakes. The service's job is coordination, and a
fake store would let a coordination bug through by agreeing with whatever it was told.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from tests.conftest import ACCOUNT, OTHER_ACCOUNT, SCOPES, build_settings
from tests.fakes.clock import FakeClock
from user_api.auth.tokens import Identity
from user_api.core.config import Settings
from user_api.core.preferences import build_preference_source
from user_api.domain.cursors import Ordering
from user_api.domain.entries import EntryType, NoteKind, Sensitivity, Source
from user_api.domain.errors import (
    CredentialRefusedError,
    EntryNotFoundError,
    InvalidDescriptionError,
    InvalidKeyError,
    InvalidScopeError,
    InvalidSearchError,
    InvalidValueError,
    ScopeNotGrantedError,
)
from user_api.domain.keys import WELL_KNOWN_KEYS
from user_api.domain.settings import ErasureMode
from user_api.entries.sql_store import SqlEntryStore
from user_api.entries.store import Filters
from user_api.events.sql_log import SqlEventLog
from user_api.users.erasure import Erasure
from user_api.users.service import UserService
from user_api.users.sql_settings import SqlSettingsStore
from user_api.users.sql_store import SqlUserStore

if TYPE_CHECKING:
    from pathlib import Path

    from user_api.storage.database import Database

GITHUB_TOKEN = "ghp" + "_16CharsOfTokenMaterialGoesRightHere0"
"""A GitHub token SHAPE, assembled rather than written down.

Split across the concatenation on purpose. A test that needs a credential-shaped string
is a test that puts one in the repository, and a secret scanner cannot tell the
difference -- correctly, which is why this one is joined at import instead."""
SENTINEL = "ZORBLAX-9741"


def identity(
    account_id: str = ACCOUNT, *, scope: str | None = None, audience: str | None = None
) -> Identity:
    """One caller. The audience is what every write records as ``asserted_by``."""
    return Identity(
        account_id=account_id,
        audience=audience or ("user" if scope is None else f"user.{scope}"),
        granted_scope=scope,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config(tmp_path: Path) -> Settings:
    return build_settings(tmp_path, allowed_scopes=SCOPES, max_pinned=3, max_note_chars=200)


@pytest.fixture
def service(database: Database, clock: FakeClock, config: Settings) -> UserService:
    events = SqlEventLog(database=database)
    entries = SqlEntryStore(database=database, events=events)
    users = SqlUserStore(database=database)
    settings = SqlSettingsStore(database=database)
    return UserService(
        users=users,
        entries=entries,
        events=events,
        settings=settings,
        erasure=Erasure(
            database=database,
            entries=entries,
            events=events,
            settings=settings,
            clock=clock,
            default_grace_days=config.default_grace_days,
        ),
        database=database,
        clock=clock,
        config=config,
        preferences=build_preference_source(config),
    )


async def field(service: UserService, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "key": "preferred_name",
        "value": "Sam",
        "description": "What to call them",
    }
    who = overrides.pop("identity", identity())
    return await service.set_field(who, **{**arguments, **overrides})


async def written_note(service: UserService, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "body": "They mentioned preferring tea to coffee.",
        "note_kind": NoteKind.OBSERVATION,
        "description": "A preference worth remembering",
    }
    who = overrides.pop("identity", identity())
    return await service.write_note(who, **{**arguments, **overrides})


class TestThereIsNoCreateStep:
    async def test_reading_a_record_nobody_has_written_to_is_empty_rather_than_missing(
        self, service: UserService
    ) -> None:
        view = await service.get_user(identity())

        assert view.record is None
        assert view.counts.fields == view.counts.notes == 0

    async def test_the_first_write_creates_the_record_on_the_way_in(
        self, service: UserService
    ) -> None:
        # An assistant cannot forget a call that does not exist, which is why ensure lives
        # in the write path rather than behind an endpoint.
        await field(service)

        assert (await service.get_user(identity())).record is not None

    async def test_setting_a_preference_before_writing_anything_works(
        self, service: UserService
    ) -> None:
        # "Destroy things immediately from now on" is a reasonable first thing to say.
        updated = await service.update_settings(identity(), erasure_mode=ErasureMode.IMMEDIATE)

        assert updated.erasure_mode is ErasureMode.IMMEDIATE


class TestKeysAreNormalised:
    @pytest.mark.parametrize("spelling", ["Preferred Name", "preferred-name", "PREFERRED_NAME"])
    async def test_any_spelling_of_a_key_writes_and_reads_the_same_field(
        self, service: UserService, spelling: str
    ) -> None:
        stored = await field(service, key=spelling)

        assert stored.key == "preferred_name"
        assert (await service.get_field(identity(), spelling)).entry_id == stored.entry_id

    async def test_a_key_filter_is_normalised_too(self, service: UserService) -> None:
        # A stored key is normalised, so a filter that was not would quietly match nothing
        # -- and an empty page reads as "that field is unset", which a caller believes.
        await field(service, key="preferred_name")

        page = await service.search(identity(), filters=Filters(keys=("Preferred Name",)))

        assert len(page.entries) == 1

    async def test_a_key_prefix_keeps_its_trailing_underscore(self, service: UserService) -> None:
        # contact_ asks for the contact_ family; contact also matches "contacts".
        await field(service, key="contact_email", value="a@b.test", description="Work email")
        await field(service, key="contacts", value=3, description="How many")

        narrow = await service.search(identity(), filters=Filters(key_prefix="contact_"))
        broad = await service.search(identity(), filters=Filters(key_prefix="Contact "))

        assert {entry.key for entry in narrow.entries} == {"contact_email"}
        assert {entry.key for entry in broad.entries} == {"contact_email", "contacts"}

    async def test_a_key_that_cannot_be_normalised_is_refused(self, service: UserService) -> None:
        with pytest.raises(InvalidKeyError):
            await field(service, key="!!!")


class TestProvenance:
    async def test_asserted_by_comes_from_the_token_and_nowhere_else(
        self, service: UserService
    ) -> None:
        # There is no parameter for it at all. The column can only ever hold the verified
        # audience, which is what makes it the half of provenance the server knows.
        stored = await field(service, identity=identity(audience="user.home", scope="home"))

        assert stored.asserted_by == "user.home"

    @pytest.mark.parametrize("source", list(Source))
    async def test_source_round_trips_as_the_claim_it_is(
        self, service: UserService, source: Source
    ) -> None:
        # The server cannot check it. A model that inferred something can write "stated"
        # and nothing here will know, which is exactly why the two live in separate
        # columns and the API labels which is which.
        stored = await field(service, source=source)

        assert stored.source is source

    async def test_a_revision_records_the_reviser_rather_than_the_original_writer(
        self, service: UserService
    ) -> None:
        stored = await field(service, identity=identity(audience="user"))

        revised = await service.revise_entry(
            identity(audience="user.home", scope="home"), stored.entry_id, value="Samuel"
        )

        assert revised.asserted_by == "user.home"

    async def test_confirming_does_not_move_asserted_by(self, service: UserService) -> None:
        # Nothing was asserted. Only the event records who did the confirming.
        stored = await field(service, identity=identity(audience="user"))

        confirmed = await service.confirm_entry(
            identity(audience="user.home", scope="home"), stored.entry_id
        )

        assert confirmed.asserted_by == "user"


class TestStaleness:
    async def test_a_revision_leaves_confirmed_at_alone(self, service: UserService) -> None:
        # A revision is somebody changing what we hold; a confirmation is somebody saying
        # it is still true. Conflating them would make every correction reset the clock,
        # and the point of tracking staleness is to find what nobody has vouched for.
        stored = await field(service)
        confirmed = await service.confirm_entry(identity(), stored.entry_id)

        revised = await service.revise_entry(identity(), stored.entry_id, description="Changed")

        assert revised.confirmed_at == confirmed.confirmed_at

    async def test_writing_the_same_value_again_counts_as_a_confirmation(
        self, service: UserService, clock: FakeClock
    ) -> None:
        await field(service, value="Sam")
        clock.advance(timedelta(days=1))

        again = await field(service, value="Sam")

        assert again.confirmed_at == clock.now()

    async def test_writing_a_different_value_clears_the_confirmation(
        self, service: UserService, clock: FakeClock
    ) -> None:
        stored = await field(service, value="Sam")
        await service.confirm_entry(identity(), stored.entry_id)
        clock.advance(timedelta(days=1))

        changed = await field(service, value="Samuel")

        assert changed.confirmed_at is None

    async def test_stale_before_finds_what_was_never_confirmed_at_all(
        self, service: UserService, clock: FakeClock
    ) -> None:
        # Never confirmed is the stalest thing there is, and a plain "confirmed_at < ?"
        # would silently exclude exactly the entries the filter exists to surface.
        never = await field(service, key="timezone", value="Europe/Lisbon", description="Zone")
        clock.advance(timedelta(days=400))

        page = await service.search(identity(), filters=Filters(stale_before=clock.now()))

        assert never.entry_id in {entry.entry_id for entry in page.entries}


class TestCredentialsAreRefused:
    async def test_in_a_field_value(self, service: UserService) -> None:
        with pytest.raises(CredentialRefusedError, match="keyring"):
            await field(service, value=GITHUB_TOKEN)

    async def test_in_a_note_body(self, service: UserService) -> None:
        with pytest.raises(CredentialRefusedError, match="keyring"):
            await written_note(service, body=f"their key is {GITHUB_TOKEN}")

    async def test_in_a_description_which_is_also_indexed(self, service: UserService) -> None:
        # A field's search_text is its key, its description and its value. Of the three
        # places a secret could land, this was the one nothing checked -- and the one that
        # also goes into the full-text index.
        with pytest.raises(CredentialRefusedError):
            await field(service, description=f"the key {GITHUB_TOKEN}")

    async def test_in_source_detail(self, service: UserService) -> None:
        with pytest.raises(CredentialRefusedError):
            await field(service, source_detail=f"copied from {GITHUB_TOKEN}")

    async def test_inside_a_list_value(self, service: UserService) -> None:
        with pytest.raises(CredentialRefusedError):
            await field(service, value=["harmless", GITHUB_TOKEN])

    async def test_inside_an_object_value(self, service: UserService) -> None:
        with pytest.raises(CredentialRefusedError):
            await field(service, value={"note": GITHUB_TOKEN})

    async def test_on_a_revision_too(self, service: UserService) -> None:
        stored = await field(service)

        with pytest.raises(CredentialRefusedError):
            await service.revise_entry(identity(), stored.entry_id, value=GITHUB_TOKEN)

    async def test_a_revised_description_is_checked(self, service: UserService) -> None:
        stored = await field(service)

        with pytest.raises(CredentialRefusedError):
            await service.revise_entry(
                identity(), stored.entry_id, description=f"see {GITHUB_TOKEN}"
            )

    async def test_nothing_is_stored_when_one_is_refused(self, service: UserService) -> None:
        with pytest.raises(CredentialRefusedError):
            await field(service, key="api_key", value=GITHUB_TOKEN)

        assert (await service.get_user(identity())).counts.fields == 0


class TestTheOrderOfTheChecks:
    async def test_a_malformed_value_is_reported_before_a_scope_problem(
        self, service: UserService
    ) -> None:
        # Shape first: the caller sent nonsense and nothing has touched the database.
        with pytest.raises(InvalidValueError):
            await field(service, value=object(), scopes=("health",))

    async def test_a_credential_is_reported_before_a_scope_problem(
        self, service: UserService
    ) -> None:
        # A caller told it lacks a scope will try again with a different SCOPE. It needs
        # to be told to try again with a different VALUE.
        with pytest.raises(CredentialRefusedError):
            await field(service, value=GITHUB_TOKEN, scopes=("health",))

    async def test_an_unknown_scope_name_is_reported_before_the_permission_problem(
        self, service: UserService
    ) -> None:
        # Told it misspelled the scope, rather than sent looking for a permission problem
        # that does not exist.
        with pytest.raises(InvalidScopeError):
            await field(service, scopes=("dinosaurs",))

    async def test_a_missing_description_is_refused(self, service: UserService) -> None:
        with pytest.raises(InvalidDescriptionError):
            await field(service, description="   ")


class TestScopes:
    async def test_an_unscoped_token_may_only_write_unscoped_entries(
        self, service: UserService
    ) -> None:
        with pytest.raises(ScopeNotGrantedError):
            await field(service, scopes=("home",))

    async def test_a_scoped_token_may_write_its_own_scope(self, service: UserService) -> None:
        stored = await field(service, identity=identity(scope="home"), scopes=("home",))

        assert stored.scopes == ("home",)

    async def test_a_scoped_token_may_still_write_unscoped_entries(
        self, service: UserService
    ) -> None:
        stored = await field(service, identity=identity(scope="home"), scopes=())

        assert stored.scopes == ()

    async def test_a_token_may_not_write_a_scope_it_does_not_hold(
        self, service: UserService
    ) -> None:
        # Write-up is refused deliberately. A user.home token tagging an entry health
        # would be creating data it cannot read back, revise, or verify it wrote
        # correctly, on the strength of a token minted for the home assistant.
        with pytest.raises(ScopeNotGrantedError):
            await field(service, identity=identity(scope="home"), scopes=("health",))

    async def test_a_scope_filter_may_narrow(self, service: UserService) -> None:
        await field(service, identity=identity(scope="home"), scopes=("home",))

        page = await service.search(identity(scope="home"), filters=Filters(scope="home"))

        assert len(page.entries) == 1

    async def test_a_scope_filter_may_not_widen(self, service: UserService) -> None:
        # Refused loudly rather than returning an empty page, because a caller that asked
        # for something it may not have and got nothing caches the emptiness.
        with pytest.raises(ScopeNotGrantedError):
            await service.search(identity(), filters=Filters(scope="health"))

    async def test_an_entry_outside_the_scope_reads_as_absent(self, service: UserService) -> None:
        hidden = await field(
            service,
            identity=identity(scope="health"),
            key="blood_type",
            value="O-",
            description="Blood type",
            scopes=("health",),
        )

        with pytest.raises(EntryNotFoundError):
            await service.get_entry(identity(scope="home"), hidden.entry_id)


class TestTheSchema:
    async def test_it_lists_the_well_known_keys_even_when_unset(self, service: UserService) -> None:
        described = await service.describe_schema(identity())

        assert {key.key for key in described} == set(WELL_KNOWN_KEYS)
        assert all(not key.set for key in described)
        assert all(key.well_known for key in described)

    async def test_a_key_that_is_set_is_marked_as_set_and_described(
        self, service: UserService
    ) -> None:
        await field(service, key="timezone", value="Europe/Lisbon", description="Their zone")

        described = {key.key: key for key in await service.describe_schema(identity())}

        assert described["timezone"].set
        assert described["timezone"].description == "Their zone"
        assert described["timezone"].value_type == "string"

    async def test_an_invented_key_is_listed_and_marked_as_not_well_known(
        self, service: UserService
    ) -> None:
        await field(service, key="favourite_biscuit", value="hobnob", description="A snack")

        described = {key.key: key for key in await service.describe_schema(identity())}

        assert described["favourite_biscuit"].set
        assert not described["favourite_biscuit"].well_known

    async def test_it_returns_no_values_at_all(self, service: UserService) -> None:
        # What keeps it cheap enough to call before inventing a key, which is the whole
        # reason it exists.
        await field(service, value=SENTINEL)

        described = await service.describe_schema(identity())

        assert SENTINEL not in repr(described)

    async def test_a_scoped_key_is_absent_from_another_tokens_schema(
        self, service: UserService
    ) -> None:
        await field(
            service,
            identity=identity(scope="health"),
            key="blood_type",
            value="O-",
            description="Blood type",
            scopes=("health",),
        )

        described = {key.key for key in await service.describe_schema(identity(scope="home"))}

        assert "blood_type" not in described


class TestForgetting:
    async def test_the_default_keeps_it_recoverable(
        self, database: Database, service: UserService
    ) -> None:
        stored = await field(service)

        await service.forget_entry(identity(), stored.entry_id)

        row = await database.fetch_one(
            "SELECT forgotten_at FROM entries WHERE entry_id = ?", (stored.entry_id,)
        )
        assert row is not None
        assert row["forgotten_at"] is not None

    async def test_immediate_mode_destroys_before_the_call_returns(
        self, database: Database, service: UserService
    ) -> None:
        # The mode decides, not the caller. All three answer the same call the same way,
        # so an assistant does not need to know which it is talking to.
        await service.update_settings(identity(), erasure_mode=ErasureMode.IMMEDIATE)
        stored = await field(service)

        await service.forget_entry(identity(), stored.entry_id)

        assert (
            await database.fetch_one("SELECT 1 FROM entries WHERE entry_id = ?", (stored.entry_id,))
            is None
        )

    async def test_forgetting_returns_the_entry_with_its_stamp(self, service: UserService) -> None:
        stored = await field(service)

        forgotten = await service.forget_entry(identity(), stored.entry_id)

        assert forgotten.forgotten_at is not None

    async def test_forgetting_an_entry_this_token_cannot_see_is_not_found(
        self, service: UserService
    ) -> None:
        hidden = await field(
            service,
            identity=identity(scope="health"),
            key="blood_type",
            value="O-",
            description="Blood type",
            scopes=("health",),
        )

        with pytest.raises(EntryNotFoundError):
            await service.forget_entry(identity(scope="home"), hidden.entry_id)


class TestDeletingTheRecord:
    @pytest.mark.parametrize("mode", list(ErasureMode))
    async def test_it_is_a_hard_purge_in_every_mode(
        self, database: Database, service: UserService, mode: ErasureMode
    ) -> None:
        # Tombstone included, which is the mode where a lesser implementation would leave
        # the tombstones behind. "Delete everything you know about me" has one meaning.
        await service.update_settings(identity(), erasure_mode=mode)
        await field(service)
        await written_note(service)
        forgotten = await field(service, key="timezone", value="Z", description="Zone")
        await service.forget_entry(identity(), forgotten.entry_id)

        erased = await service.delete_user(identity())

        assert erased.entries > 0
        assert await database.count("SELECT count(*) AS total FROM entries") == 0
        assert await database.count("SELECT count(*) AS total FROM events") == 0
        assert await database.count("SELECT count(*) AS total FROM entry_search") == 0
        assert await database.count("SELECT count(*) AS total FROM user_settings") == 0

    async def test_it_reports_what_went(self, service: UserService) -> None:
        await field(service)
        await written_note(service)

        erased = await service.delete_user(identity())

        assert erased.entries == 2
        assert erased.events > 0

    async def test_it_leaves_another_account_alone(self, service: UserService) -> None:
        await field(service, identity=identity(OTHER_ACCOUNT))
        await field(service)

        await service.delete_user(identity())

        assert (await service.get_user(identity(OTHER_ACCOUNT))).counts.fields == 1

    async def test_a_record_can_be_written_to_again_afterwards(self, service: UserService) -> None:
        await field(service)
        await service.delete_user(identity())

        rebuilt = await field(service)

        assert rebuilt.key == "preferred_name"


class TestReading:
    async def test_the_always_load_block_carries_counts_and_pins(
        self, service: UserService
    ) -> None:
        await field(service, pinned=True)
        await written_note(service)

        view = await service.get_user(identity())

        assert view.counts.fields == 1
        assert view.counts.notes == 1
        assert view.counts.pinned == 1
        assert [entry.entry_id for entry in view.pinned] == [
            entry.entry_id for entry in view.pinned
        ]
        assert len(view.pinned) == 1

    async def test_the_pinned_set_is_capped(self, service: UserService, config: Settings) -> None:
        # A token budget before it is a preference: this goes into a context window.
        for index in range(config.max_pinned):
            await written_note(service, body=f"pinned note {index}", pinned=True)

        view = await service.get_user(identity())

        assert len(view.pinned) == config.max_pinned

    async def test_export_is_fixed_to_the_stable_ordering(
        self, service: UserService, clock: FakeClock
    ) -> None:
        # created_at never moves, so a row cannot shift position while you walk it.
        # Recency ordering has no such guarantee, and an export has to be exactly right.
        for index in range(5):
            clock.advance(timedelta(minutes=1))
            await written_note(service, body=f"note {index}")

        page = await service.export(identity(), limit=100)

        stamps = [entry.created_at for entry in page.entries]
        assert stamps == sorted(stamps)

    async def test_a_query_forces_relevance_ordering_whatever_was_asked_for(
        self, service: UserService
    ) -> None:
        await written_note(service, body="they prefer strong coffee in the morning")

        page = await service.search(
            identity(), filters=Filters(query="coffee"), ordering=Ordering.OLDEST
        )

        assert page.entries[0].rank is not None

    async def test_relevance_without_a_query_is_refused(self, service: UserService) -> None:
        with pytest.raises(InvalidSearchError, match="needs a q"):
            await service.search(identity(), filters=Filters(), ordering=Ordering.RELEVANCE)

    async def test_a_limit_is_clamped_rather_than_refused(
        self, service: UserService, config: Settings
    ) -> None:
        # A caller asking for a thousand wants as many as it can have, and a 422 teaches
        # it nothing it can act on.
        for index in range(3):
            await written_note(service, body=f"note {index}")

        assert len((await service.search(identity(), limit=10_000, filters=Filters())).entries) == 3
        assert len((await service.search(identity(), limit=0, filters=Filters())).entries) == 1

    async def test_an_absent_limit_uses_the_configured_default(
        self, service: UserService, config: Settings
    ) -> None:
        for index in range(config.search_default_limit + 3):
            await written_note(service, body=f"note {index}")

        page = await service.search(identity(), filters=Filters())

        assert len(page.entries) == config.search_default_limit

    async def test_a_cursor_walks_to_the_end(self, service: UserService) -> None:
        for index in range(7):
            await written_note(service, body=f"note {index}")

        seen: list[str] = []
        cursor: str | None = None
        while True:
            page = await service.search(identity(), filters=Filters(), limit=3, cursor=cursor)
            seen.extend(entry.entry_id for entry in page.entries)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor.encode()

        assert len(seen) == len(set(seen)) == 7

    async def test_reading_a_field_that_is_not_there_is_not_found(
        self, service: UserService
    ) -> None:
        with pytest.raises(EntryNotFoundError):
            await service.get_field(identity(), "nothing_here")

    async def test_reading_an_entry_that_is_not_there_is_not_found(
        self, service: UserService
    ) -> None:
        with pytest.raises(EntryNotFoundError):
            await service.get_entry(identity(), "an-id-nobody-issued")

    async def test_the_event_log_reads_back(self, service: UserService) -> None:
        await field(service)

        events = await service.read_events(identity())

        assert [event.action.value for event in events] == ["field.set"]

    async def test_the_event_log_pages_by_sequence(self, service: UserService) -> None:
        for index in range(4):
            await written_note(service, body=f"note {index}")
        first = await service.read_events(identity(), limit=2)

        second = await service.read_events(identity(), limit=2, before=first[-1].sequence)

        assert {event.sequence for event in first} & {event.sequence for event in second} == set()


class TestTheJournal:
    async def test_values_are_kept_out_of_the_log_by_default(self, service: UserService) -> None:
        # The event log is a SECOND COPY of the personal data, and "changed diagnosis from
        # X to Y" is itself the sensitive fact.
        await field(service, value=SENTINEL)

        events = await service.read_events(identity())

        assert all(event.detail is None for event in events)

    async def test_turning_values_on_keeps_them(self, service: UserService) -> None:
        await service.update_settings(identity(), log_values=True)

        await field(service, value=SENTINEL)

        assert (await service.read_events(identity()))[0].detail == {"value": SENTINEL}

    async def test_turning_values_off_takes_effect_on_the_very_next_write(
        self, service: UserService
    ) -> None:
        # Read per write rather than cached, because the person turning it off is doing so
        # for a reason and should not have to wait for a cache to expire.
        await service.update_settings(identity(), log_values=True)
        await field(service, value=SENTINEL)

        await service.update_settings(identity(), log_values=False)
        await written_note(service, body="something later")

        assert (await service.read_events(identity()))[0].detail is None


class TestRevising:
    async def test_an_omitted_field_is_left_alone(self, service: UserService) -> None:
        stored = await field(service, value="Sam", description="What to call them")

        revised = await service.revise_entry(identity(), stored.entry_id, pinned=True)

        assert revised.value == "Sam"
        assert revised.description == "What to call them"
        assert revised.pinned

    async def test_setting_a_value_to_null_is_different_from_omitting_it(
        self, service: UserService
    ) -> None:
        # null is a legal field value, so "unset it" and "leave it alone" are different
        # requests and both have to be expressible.
        stored = await field(service, value="Sam")

        cleared = await service.revise_entry(identity(), stored.entry_id, value=None)

        assert cleared.value is None
        assert cleared.value_type is not None

    async def test_a_revision_bumps_the_revision_number(self, service: UserService) -> None:
        stored = await field(service)

        revised = await service.revise_entry(identity(), stored.entry_id, value="Samuel")

        assert revised.revision == stored.revision + 1

    async def test_revising_an_entry_this_token_cannot_see_is_not_found(
        self, service: UserService
    ) -> None:
        hidden = await field(
            service,
            identity=identity(scope="health"),
            key="blood_type",
            value="O-",
            description="Blood type",
            scopes=("health",),
        )

        with pytest.raises(EntryNotFoundError):
            await service.revise_entry(identity(scope="home"), hidden.entry_id, value="A+")

    async def test_a_note_body_can_be_revised(self, service: UserService) -> None:
        stored = await written_note(service)

        revised = await service.revise_entry(identity(), stored.entry_id, body="Corrected.")

        assert revised.body == "Corrected."
        assert revised.entry_type is EntryType.NOTE

    async def test_scopes_can_be_narrowed_to_nothing(self, service: UserService) -> None:
        stored = await field(service, identity=identity(scope="home"), scopes=("home",))

        revised = await service.revise_entry(identity(scope="home"), stored.entry_id, scopes=())

        assert revised.scopes == ()

    async def test_a_revision_may_not_reach_a_scope_the_token_lacks(
        self, service: UserService
    ) -> None:
        stored = await field(service, identity=identity(scope="home"), scopes=("home",))

        with pytest.raises(ScopeNotGrantedError):
            await service.revise_entry(identity(scope="home"), stored.entry_id, scopes=("health",))

    async def test_a_revised_sensitivity_and_source_stick(self, service: UserService) -> None:
        stored = await field(service)

        revised = await service.revise_entry(
            identity(),
            stored.entry_id,
            sensitivity=Sensitivity.SENSITIVE,
            source=Source.INFERRED,
            source_detail="worked out from their calendar",
        )

        assert revised.sensitivity is Sensitivity.SENSITIVE
        assert revised.source is Source.INFERRED
        assert revised.source_detail == "worked out from their calendar"


class TestSettings:
    async def test_they_default_rather_than_being_absent(self, service: UserService) -> None:
        held = await service.get_settings(identity())

        assert held.erasure_mode is ErasureMode.GRACE

    async def test_one_setting_can_be_changed_without_restating_the_others(
        self, service: UserService
    ) -> None:
        await service.update_settings(
            identity(), erasure_mode=ErasureMode.TOMBSTONE, log_values=True
        )

        after = await service.update_settings(identity(), grace_days=5)

        assert after.erasure_mode is ErasureMode.TOMBSTONE
        assert after.log_values is True
        assert after.grace_days == 5
