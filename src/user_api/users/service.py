"""Everything a request means, once it is known who is asking.

The routers below this are thin on purpose: they parse, they call one method here, and
they render. Every rule about what a write is allowed to contain lives in this file, which
is what makes "is a credential refused on this path too?" a question with one place to
look rather than six.

## The order the checks run in

It is not arbitrary, and getting it wrong leaks things:

1. **Shape** -- normalise the key, validate the value, check the description. A caller who
   sent nonsense is told so, and nothing has touched the database.
2. **Credentials** -- refuse anything that looks like a secret, naming keyring. Before the
   scope check, so a caller pasting an API key is told what is actually wrong rather than
   being told it lacks a scope and trying again with a different one.
3. **Scopes** -- are these names real, and does this token carry them. Facts about the
   caller's own token, so they may be specific.
4. **Existence and caps** -- inside the store's transaction, where they cannot go stale.

## Provenance, and which half we verified

``asserted_by`` is the token's audience, taken from the verified claims and never from the
request body. ``source`` is what the writer claims and the server cannot check it -- a
model that inferred something can say ``stated`` and nothing here will know. Both are
stored, both come back, and the API labels which is which. There is a test asserting that
an ``asserted_by`` in a request body is ignored.

## A record is data, never instructions

Nothing in this file interprets an entry's content. An assistant that reads web pages and
email is writing into this service from untrusted text, so an entry is a *reported claim*
and the docs tell the consumer to render it as one -- "your notes say" -- never as a system
instruction. The API cannot enforce that. What it can do is make the honest shape the easy
one, by attaching provenance to everything that comes out.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from user_api.core.logging import get_logger
from user_api.domain.cursors import Cursor, Ordering, decode_cursor
from user_api.domain.entries import (
    Entry,
    NoteKind,
    Sensitivity,
    Source,
    validate_description,
    validate_note_body,
)
from user_api.domain.errors import CredentialRefusedError, EntryNotFoundError
from user_api.domain.keys import WELL_KNOWN_KEYS, normalize_key, normalize_key_prefix
from user_api.domain.scopes import check_filterable, check_known, check_writable
from user_api.domain.secrets import KEYRING_ADVICE, looks_like_a_credential
from user_api.domain.values import searchable_text, validate_value
from user_api.entries.store import UNSET, Counts, Filters, Journal, Page, _Unset

if TYPE_CHECKING:
    from datetime import datetime

    from user_api.auth.tokens import Identity
    from user_api.core.clock import Clock
    from user_api.core.config import Settings
    from user_api.domain.settings import ErasureMode, UserSettings
    from user_api.entries.store import EntryStore
    from user_api.events.log import Event, EventLog
    from user_api.storage.database import Database
    from user_api.users.erasure import Erasure
    from user_api.users.settings import SettingsStore
    from user_api.users.store import Erased, UserRecord, UserStore

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UserView:
    """The always-load block: one call at the start of a conversation.

    Counts plus every pinned entry the token's scope permits, and nothing else. An
    assistant reads this once and knows who it is talking to; everything further is a
    deliberate second call.
    """

    account_id: str
    record: UserRecord | None
    granted_scope: str | None
    counts: Counts
    events: int
    pinned: tuple[Entry, ...]


@dataclass(frozen=True, slots=True)
class SchemaKey:
    """One key in ``describe_schema``: what it means, not what it holds."""

    key: str
    set: bool
    well_known: bool
    description: str | None = None
    value_type: str | None = None
    updated_at: datetime | None = None
    confirmed_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    pinned: bool = False


class UserService:
    """The one place a request becomes a change."""

    # Five stores, the erasure path, the transaction boundary, the clock and the
    # configuration. This is the composition root's output, not a call site's argument
    # list -- it is constructed once.
    def __init__(  # noqa: PLR0913
        self,
        *,
        users: UserStore,
        entries: EntryStore,
        events: EventLog,
        settings: SettingsStore,
        erasure: Erasure,
        database: Database,
        clock: Clock,
        config: Settings,
    ) -> None:
        self._users = users
        self._entries = entries
        self._events = events
        self._settings = settings
        self._erasure = erasure
        self._db = database
        self._clock = clock
        self._config = config

    # -- reads -------------------------------------------------------------------------

    async def get_user(self, identity: Identity) -> UserView:
        """The always-load block. Never a 404: an account that has written nothing has an
        empty record, which is the truth rather than an error.
        """
        return UserView(
            account_id=identity.account_id,
            record=await self._users.get(identity.account_id),
            granted_scope=identity.granted_scope,
            counts=await self._entries.counts(identity.account_id, granted=identity.granted_scope),
            events=await self._events.count_for_account(identity.account_id),
            pinned=await self._entries.pinned(
                identity.account_id,
                granted=identity.granted_scope,
                limit=self._config.max_pinned,
            ),
        )

    async def describe_schema(self, identity: Identity) -> tuple[SchemaKey, ...]:
        """Every key this token can see, plus the well-known ones whether set or not.

        The unset well-known keys are the point of including them. A model that needs to
        know what to call somebody can find ``preferred_name`` here without guessing at a
        name, and a model that has just *learned* their pronouns has somewhere obvious to
        put them -- which is how five well-chosen conventions prevent fifty invented keys.
        """
        described = await self._entries.describe(
            identity.account_id, granted=identity.granted_scope
        )
        present = {summary.key: summary for summary in described}

        keys = [
            SchemaKey(
                key=summary.key,
                set=True,
                well_known=summary.key in WELL_KNOWN_KEYS,
                description=summary.description,
                value_type=summary.value_type,
                updated_at=summary.updated_at,
                confirmed_at=summary.confirmed_at,
                scopes=summary.scopes,
                pinned=summary.pinned,
            )
            for summary in described
        ]
        keys.extend(
            SchemaKey(key=key, set=False, well_known=True)
            for key in WELL_KNOWN_KEYS
            if key not in present
        )
        return tuple(sorted(keys, key=lambda item: item.key))

    async def get_entry(self, identity: Identity, entry_id: str) -> Entry:
        """One entry. Raises :class:`EntryNotFoundError` for anything it may not see."""
        entry = await self._entries.get(
            identity.account_id, entry_id, granted=identity.granted_scope
        )
        return _found(entry)

    async def get_field(self, identity: Identity, key: str) -> Entry:
        """One field by key. The key is normalised first, so ``Preferred Name`` finds it."""
        entry = await self._entries.get_field(
            identity.account_id, normalize_key(key), granted=identity.granted_scope
        )
        return _found(entry)

    # account, scope filter, the filter bundle, ordering, limit, cursor -- six independent
    # axes, which is what a single flexible read endpoint costs.
    async def search(
        self,
        identity: Identity,
        *,
        filters: Filters,
        ordering: Ordering = Ordering.RECENT,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page:
        """The one flexible read behind ``GET /v1/user/entries``."""
        # Normalised here rather than at the edge, because a stored key is normalised and
        # a filter that was not would quietly match nothing: ?keys=Preferred%20Name would
        # return an empty page rather than the field it plainly means.
        filters = _normalise_key_filters(filters)

        if filters.scope is not None:
            # Narrowing within what the token already grants is useful; widening is the
            # thing the whole scope design exists to prevent. Refused loudly rather than
            # quietly returning nothing, because a caller that asked for something it may
            # not have and got an empty page caches the emptiness and stops asking.
            check_filterable(filters.scope, granted=identity.granted_scope)

        effective = Ordering.RELEVANCE if filters.query is not None else ordering
        return await self._entries.search(
            identity.account_id,
            granted=identity.granted_scope,
            filters=filters,
            ordering=effective,
            limit=self._limit(limit),
            cursor=_cursor(cursor, expected=effective),
        )

    async def export(
        self, identity: Identity, *, limit: int | None = None, cursor: str | None = None
    ) -> Page:
        """Everything this token can see, oldest first.

        Fixed to :data:`~user_api.domain.cursors.Ordering.OLDEST` rather than offering a
        choice. ``created_at`` never changes after the insert, so a row cannot move while
        the walk is in progress and the walk therefore cannot skip one. Recency ordering
        has no such guarantee, and "export everything you know about me" is the one read in
        this service that has to be exactly right.
        """
        return await self._entries.search(
            identity.account_id,
            granted=identity.granted_scope,
            filters=Filters(),
            ordering=Ordering.OLDEST,
            limit=self._limit(limit),
            cursor=_cursor(cursor, expected=Ordering.OLDEST),
        )

    async def read_events(
        self, identity: Identity, *, limit: int | None = None, before: int | None = None
    ) -> list[Event]:
        """The change log, newest first."""
        return await self._events.read(
            identity.account_id, limit=self._limit(limit), before_sequence=before
        )

    # -- writes ------------------------------------------------------------------------

    async def set_field(  # noqa: PLR0913
        self,
        identity: Identity,
        *,
        key: str,
        value: object,
        description: str,
        source: Source = Source.STATED,
        source_detail: str | None = None,
        scopes: tuple[str, ...] = (),
        sensitivity: Sensitivity = Sensitivity.NORMAL,
        pinned: bool = False,
    ) -> Entry:
        """Create or replace one field. Idempotent by key, so a retry cannot duplicate."""
        normalized = normalize_key(key)
        validate_value(
            value,
            max_bytes=self._config.max_value_bytes,
            max_depth=self._config.max_value_depth,
        )
        described = validate_description(description)
        _refuse_credentials(value)
        self._check_scopes(scopes, identity)

        now = self._clock.now()
        # Before the write, not after: entries reference the record by foreign key, so a
        # first write with no record would fail the constraint rather than create one.
        # Doing it here is what makes "there is no create step" true -- an assistant
        # cannot forget a call that does not exist.
        await self._users.ensure(identity.account_id, now=now)
        journal = await self._journal(identity.account_id)
        entry = await self._entries.put_field(
            account_id=identity.account_id,
            key=normalized,
            value=value,
            description=described,
            source=source,
            source_detail=source_detail,
            asserted_by=identity.audience,
            scopes=scopes,
            sensitivity=sensitivity,
            pinned=pinned,
            granted=identity.granted_scope,
            now=now,
            entry_cap=self._config.max_entries_per_account,
            field_cap=self._config.max_fields_per_account,
            pin_cap=self._config.max_pinned,
            journal=journal,
        )
        await self._users.touch(identity.account_id, now=now)
        # The key is metadata and is safe to record. The value is not, and is not here.
        logger.info("field_set", key=normalized, entry_id=entry.entry_id)
        return entry

    async def write_note(  # noqa: PLR0913
        self,
        identity: Identity,
        *,
        body: str,
        note_kind: NoteKind,
        description: str,
        source: Source = Source.INFERRED,
        source_detail: str | None = None,
        scopes: tuple[str, ...] = (),
        sensitivity: Sensitivity = Sensitivity.NORMAL,
        pinned: bool = False,
    ) -> Entry:
        """Append a note. Always creates -- notes have no natural key."""
        text = validate_note_body(body, max_chars=self._config.max_note_chars)
        described = validate_description(description)
        _refuse_credentials(text)
        self._check_scopes(scopes, identity)

        now = self._clock.now()
        await self._users.ensure(identity.account_id, now=now)
        journal = await self._journal(identity.account_id)
        entry = await self._entries.write_note(
            account_id=identity.account_id,
            body=text,
            note_kind=note_kind,
            description=described,
            source=source,
            source_detail=source_detail,
            asserted_by=identity.audience,
            scopes=scopes,
            sensitivity=sensitivity,
            pinned=pinned,
            now=now,
            entry_cap=self._config.max_entries_per_account,
            pin_cap=self._config.max_pinned,
            journal=journal,
        )
        await self._users.touch(identity.account_id, now=now)
        logger.info("note_written", entry_id=entry.entry_id, note_kind=note_kind.value)
        return entry

    async def revise_entry(  # noqa: PLR0913
        self,
        identity: Identity,
        entry_id: str,
        *,
        value: object | _Unset = UNSET,
        body: str | None = None,
        description: str | None = None,
        sensitivity: Sensitivity | None = None,
        pinned: bool | None = None,
        scopes: tuple[str, ...] | None = None,
        source: Source | None = None,
        source_detail: str | None = None,
    ) -> Entry:
        """Change part of an entry. Leaves ``confirmed_at`` alone -- see the port."""
        if not isinstance(value, _Unset):
            validate_value(
                value,
                max_bytes=self._config.max_value_bytes,
                max_depth=self._config.max_value_depth,
            )
            _refuse_credentials(value)
        if body is not None:
            body = validate_note_body(body, max_chars=self._config.max_note_chars)
            _refuse_credentials(body)
        if description is not None:
            description = validate_description(description)
        if scopes is not None:
            self._check_scopes(scopes, identity)

        now = self._clock.now()
        entry = await self._entries.revise(
            account_id=identity.account_id,
            entry_id=entry_id,
            granted=identity.granted_scope,
            now=now,
            asserted_by=identity.audience,
            pin_cap=self._config.max_pinned,
            journal=await self._journal(identity.account_id),
            value=value,
            body=body,
            description=description,
            sensitivity=sensitivity,
            pinned=pinned,
            scopes=scopes,
            source=source,
            source_detail=source_detail,
        )
        await self._users.touch(identity.account_id, now=now)
        logger.info("entry_revised", entry_id=entry_id, revision=entry.revision)
        return entry

    async def confirm_entry(self, identity: Identity, entry_id: str) -> Entry:
        """Record that a human says this is still true. The cheap half of staleness."""
        return await self._entries.confirm(
            account_id=identity.account_id,
            entry_id=entry_id,
            granted=identity.granted_scope,
            now=self._clock.now(),
            asserted_by=identity.audience,
            journal=await self._journal(identity.account_id),
        )

    async def forget_entry(self, identity: Identity, entry_id: str) -> Entry:
        """Forget one entry, and destroy it now if that is what this account asked for.

        The mode decides, not the caller. A person who set ``immediate`` gets destruction
        before the response goes out; one on the default gets a grace period they can
        change their mind inside; one on ``tombstone`` gets a marker that never becomes a
        deletion. All three answer this call the same way, so an assistant does not need to
        know which it is talking to.
        """
        now = self._clock.now()
        entry = await self._entries.forget(
            account_id=identity.account_id,
            entry_id=entry_id,
            granted=identity.granted_scope,
            now=now,
            asserted_by=identity.audience,
            journal=await self._journal(identity.account_id),
        )
        await self._users.touch(identity.account_id, now=now)

        settings = await self._settings.get(
            identity.account_id, default_grace_days=self._config.default_grace_days
        )
        if settings.purges_on_forget:
            await self._erasure.purge_now(identity.account_id, entry_id)

        logger.info("entry_forgotten", entry_id=entry_id, mode=settings.erasure_mode.value)
        return entry

    async def delete_user(self, identity: Identity) -> Erased:
        """Destroy everything, whatever the erasure mode says.

        The mode governs what forgetting *one* entry means and gets no say here: "delete
        everything you know about me" has one honest reading, and a tombstone is not it.
        The checkpoint is what turns the deletes into an erasure -- see
        :mod:`user_api.users.erasure`.
        """
        erased = await self._users.delete(identity.account_id)
        await self._db.checkpoint_truncate()
        logger.info("user_deleted", entries=erased.entries, events=erased.events)
        return erased

    # -- settings ----------------------------------------------------------------------

    async def get_settings(self, identity: Identity) -> UserSettings:
        """This account's choices, defaulted rather than absent."""
        return await self._settings.get(
            identity.account_id, default_grace_days=self._config.default_grace_days
        )

    async def update_settings(
        self,
        identity: Identity,
        *,
        erasure_mode: ErasureMode | None = None,
        grace_days: int | None = None,
        log_values: bool | None = None,
    ) -> UserSettings:
        """Change some settings. Never retroactive -- see the port for why that matters.

        Spelled out rather than taking ``**changes``, which was the first shape and was
        worse in a way worth recording: kwargs made the router's ``model_dump`` the only
        thing deciding which settings exist, so a field renamed on the wire would sail
        through to the store and fail there, at runtime, rather than here, at a type check.
        """
        now = self._clock.now()
        # A person may set a preference before writing anything at all -- "destroy things
        # immediately from now on" is a reasonable first thing to say -- and the settings
        # row has a foreign key to the record.
        await self._users.ensure(identity.account_id, now=now)
        return await self._settings.update(
            identity.account_id,
            now=now,
            default_grace_days=self._config.default_grace_days,
            erasure_mode=erasure_mode,
            grace_days=grace_days,
            log_values=log_values,
        )

    # -- shared --------------------------------------------------------------------

    async def _journal(self, account_id: str) -> Journal:
        """How this account has asked its changes to be recorded.

        Read per write rather than cached: turning ``log_values`` off has to take effect on
        the very next write, not whenever a cache happens to expire, because the person
        turning it off is doing so for a reason.
        """
        settings = await self._settings.get(
            account_id, default_grace_days=self._config.default_grace_days
        )
        return Journal(cap=self._config.max_events, log_values=settings.log_values)

    def _check_scopes(self, scopes: tuple[str, ...], identity: Identity) -> None:
        """Are these real scope names, and does this token carry them.

        Known-first, so a caller that misspelled the scope it *does* hold is told it
        misspelled it rather than told it lacks permission and sent looking for a
        permission problem that does not exist.
        """
        check_known(scopes, allowed=self._config.allowed_scopes)
        check_writable(scopes, granted=identity.granted_scope)

    def _limit(self, requested: int | None) -> int:
        """Clamp a requested page size. Clamped rather than refused: a caller asking for a
        thousand wants as many as it can have, and a 422 teaches it nothing it can act on.
        """
        if requested is None:
            return self._config.search_default_limit
        return max(1, min(requested, self._config.search_max_limit))


def _normalise_key_filters(filters: Filters) -> Filters:
    """Fold any key or key prefix in a filter the same way a write folds one.

    An unfoldable key raises, which is the same answer ``get_field`` gives for one -- a
    caller that asked for a key that cannot exist has made a mistake worth hearing about,
    and silently returning nothing would teach it that the key is simply unset.
    """
    if filters.keys is None and filters.key_prefix is None:
        return filters
    return replace(
        filters,
        keys=tuple(normalize_key(key) for key in filters.keys) if filters.keys else filters.keys,
        key_prefix=(
            normalize_key_prefix(filters.key_prefix) if filters.key_prefix is not None else None
        ),
    )


def _found(entry: Entry | None) -> Entry:
    """Turn an absent, forgotten, foreign or out-of-scope entry into the one error."""
    if entry is None:
        msg = "no such entry"
        raise EntryNotFoundError(msg)
    return entry


def _refuse_credentials(value: object) -> None:
    """Refuse anything that looks like a secret, naming where it belongs instead.

    Applied to the rendered text of a value rather than to strings only, so a credential
    hidden inside a list or an object is caught as readily as one written plainly. keyring
    is named in the message because the caller is a model that will otherwise try again
    with the same value phrased differently.
    """
    reason = looks_like_a_credential(value if isinstance(value, str) else searchable_text(value))
    if reason is not None:
        msg = f"{reason}; {KEYRING_ADVICE}"
        raise CredentialRefusedError(msg)


def _cursor(raw: str | None, *, expected: Ordering) -> Cursor | None:
    """Decode a cursor, checking it belongs to the ordering being walked."""
    return None if raw is None else decode_cursor(raw, expected=expected)
