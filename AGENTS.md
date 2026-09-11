# AGENTS.md

Working notes for anyone -- human or agent -- changing this codebase. Read this before your
first edit. It is the single source of truth for how work is done here; `CLAUDE.md` just
points at it.

## What this service is

**user-api** is one HTTP service holding structured knowledge about the **person** an
assistant is talking to: fields (named facts, one live one per key) and notes (episodes,
observations, lessons). An assistant loads the record at the start of a conversation and
writes to it as it learns.

It is the second service in this family. [keyring](../keyring-api) holds the accounts and
the credentials; this service has none of its own. The only identity it ever learns is the
`sub` of a token keyring signed, verified locally against keyring's JWKS document. It never
calls keyring at request time, and it cannot ask keyring anything about a person.

The subject being a person rather than a system is what makes the invariants below
non-negotiable. Everything in the database is somebody's own data, in plaintext, and most
of what can go wrong here goes wrong quietly.

The HTTP surface is designed to be fronted by an **MCP server** later, so an assistant can
call it as tools. That is why route `operation_id`s and descriptions are treated as
contract rather than decoration -- see [Invariants](#invariants) and
[docs/mcp.md](docs/mcp.md).

## Commands

| Command | What it does |
| --- | --- |
| `make install` | Create the venv and install everything. |
| `make check` | **The gate.** Format check, lint, strict types, layering contracts, tests at 100% branch coverage. Run before every commit. |
| `make matrix` | The tests on every Python CI runs. Coverage genuinely differs between versions; a green `check` is one interpreter's opinion. |
| `make test` | Tests only, with coverage enforced. |
| `make cov` | HTML coverage report in `htmlcov/`. |
| `make fmt` | Format and auto-fix. |
| `make lint` / `make type` / `make imports` | The three halves of `check` that are not tests. |
| `make schema` | Regenerate `storage/schema.sql` after changing a migration. Commit the diff with it. |
| `make run` | Serve on :8002 with reload. Docs at `/docs`. |
| `make smoke` | End-to-end against a running user-api **and** a running keyring. See `scripts/smoke.py`. |
| `make docker` | Build the image. |

Always run `make check` rather than a bare `pytest` -- piping any of these to `head`/`tail`
in a shell chain masks the exit code, which is how a broken commit slips through.

## The map

```
src/user_api/
  core/      config, clock, logging, request context, version, and the composition root
  domain/    pure types and rules: Entry, ErasureMode, cursors, field keys, scopes,
             search-query building, value limits, the credential detector. Imports
             nothing internal.
  storage/   the SQLite connection on its one thread, the migrations, the schema
             snapshot, and how a datetime becomes a column. Knows rows and transactions,
             and nothing else.
  auth/      JwksClient and TokenVerifier. The only package that imports `jwt` or
             `httpx`, and the only one that decides who a request is for.
  events/    EventLog port + SQL adapter. The person's own history of their own record.
  entries/   EntryStore port + SQL adapter. Fields, notes, their scopes, and the FTS
             index, which is maintained by hand.
  users/     the record row, the settings port and its adapter, the erasure path, and
             UserService -- where a request becomes a change.
  api/       FastAPI app, routers, wire schemas, problem+json errors, middleware.
```

Dependencies point inward:
`api → users → entries → events → auth → storage → domain`. `core` is a shared kernel
everything may use, except `domain`.

`domain/` is the bottom because it is where the rules are written down in a form a person
can read: `Entry.visible_to` is the scope boundary in Python beside the SQL that enforces
it, and a test asserts the two agree. A boundary that exists only as a `WHERE` clause is a
boundary nobody can read.

`auth/` sits below `events`/`entries`/`users` and above `storage` because it needs nothing
from the database: a token is verified against a cached public key and nothing else. That
position is also what lets one import-linter contract keep `jwt` and `httpx` inside it.

## Invariants

These are enforced mechanically. If you want to break one, change the enforcement
deliberately and say why in the commit message -- do not work around it.

1. **The domain imports nothing from the rest of the package.** The import-linter contract
   "Domain is independent" in `pyproject.toml`, run by `make imports`.
2. **Layers point inward.** The contract "Layers point inward", listing the seven packages
   in order. `exhaustive = false`, so `core` is outside it by design.
3. **`jwt` and `httpx` are imported only by `user_api.auth`.** The contract "Keyring is
   spoken to from one package only", which forbids both to every other package,
   indirect imports included. Everything this service asks of keyring, and every rule by
   which it believes an answer, lives in one package you can read in a sitting. It is also
   why `JwksClient.key_for` is annotated `-> Any` rather than `-> jwt.PyJWK`: a signature
   naming a library's type is how that library leaks out of the package meant to hold it.
4. **SQL stays behind the stores.** The contract "SQL stays behind the stores" forbids
   `api`, `auth` and `domain` from importing `user_api.storage` or `sqlite3`. A router that
   *could* write a query is a router that will eventually contain one.
5. **Nothing reads the wall clock.** Every component that behaves differently over time
   takes a `Clock` in its constructor, and `SystemClock` in `core/clock.py` is the only
   caller of `datetime.now` or `time.monotonic` in `src/`. This includes JWT expiry: PyJWT's
   `verify_exp` **and** `verify_iat` are switched off and expiry is re-checked against the
   injected clock, because PyJWT refuses a token whose `iat` is in the future by the *wall*
   clock, which would refuse every good token in a test that pinned the clock to next
   Tuesday. What enforces it: ruff's `DTZ` rules refuse a naive `datetime.now()`,
   `storage/times.py` raises on any attempt to store a naive datetime, and a suite that
   never sleeps cannot test a grace period any other way. The one deliberate exception is
   `time.perf_counter()` in `api/middleware.py`, which measures a request's duration for a
   log field and decides nothing.
6. **No account id appears in any path, and no endpoint accepts one.** Every route is under
   `/v1/user`, and the account comes from the verified `sub` via `IdentityDep`. Every store
   method takes an `account_id` and it is not optional on any of them, so a cross-account
   read is not forbidden -- it is inexpressible. The corollary: another account's entry is
   **404, never 403**, identical to one that never existed. `EntryNotFoundError` covers
   never-existed, another account's, forgotten and out-of-scope alike; adding a
   distinguishable error here is a security change.
7. **Scope comes from the token's `aud`, and a query parameter can only narrow.**
   `granted_scope()` in `domain/scopes.py` is the only thing that turns an audience into a
   grant, and it is called from the verified claims in `auth/tokens.py` and nowhere else.
   `check_filterable` refuses a `?scope=` that is not the one the token holds -- refused
   rather than quietly empty, because a caller that gets an empty page caches the emptiness
   and stops asking. `check_writable` refuses writing *up*. Enforcement below the service is
   `_VISIBLE` in `entries/sql_store.py`, one predicate binding one parameter, applied to
   writes addressed by id as well as to reads.
8. **No entry content ever reaches a log record.** Two mechanisms, and both are needed: no
   call site passes content to a logger (log `entry_id`, `key`, `entry_type`, counts), and
   `redact_secrets` in `core/logging.py` replaces anything whose field name is on
   `_CONTENT_FIELDS` or matches a sensitive substring, at every depth, before rendering.
   The rule is what keeps content out; the redactor is what catches the call site that
   forgot. A test drives a request whose body carries a sentinel and asserts it appears in
   no log record, on the success path and on every failure path. Note what else this covers:
   `RequestValidationError` is reshaped by hand in `api/errors.py` because FastAPI's own
   handler echoes the offending **input**, and an unhandled exception is rendered by type
   name only.
9. **A credential is refused rather than stored.** `looks_like_a_credential` runs on
   `set_field`, `write_note` and both halves of `revise_entry`, after shape validation and
   **before** the scope check, so a caller pasting an API key is told what is actually
   wrong. The refusal names keyring and never echoes the matched text. There is no override
   and there is no setting that turns it off. The specification is the pair of corpora in
   `tests/unit/domain/test_secrets.py`: `MUST_ACCEPT` is the one allowed to grow, and a
   change that shrinks it is a regression even if it catches more secrets.
10. **Erasure means `DELETE` plus a truncating checkpoint.** `PRAGMA wal_checkpoint(FULL)`
    is not enough and `DELETE` alone is not close: the forgotten value sits in the `-wal`
    file, findable with `grep`. `users/erasure.py` does the deletes and the event-value
    stripping in one transaction and calls `Database.checkpoint_truncate` afterwards, once
    per sweep, because a checkpoint cannot run inside a transaction. `DELETE /v1/user` does
    the same pair. A test scans the bytes of the database **and** its `-wal` for a sentinel;
    it is the one test here that a unit test cannot replace, because it is about the file
    rather than about the code. `PRAGMA secure_delete` is on and is not what makes this
    work -- see [ADR-0005](docs/adr/0005-erasure-is-a-setting.md).
11. **Route `operation_id`s are public API.** They become MCP tool names, so renaming one
    breaks every client with a tool bound to it. There are sixteen; every route sets one
    explicitly, snake_case `verb_noun`, along with a `summary` and a real `description`
    written for a model rather than for a browser. Keep the OpenAPI contract test that pins
    the exact set in step when you add an endpoint.
12. **Coverage is 100% branch coverage, and the exclusions are only non-executable
    lines** -- `if TYPE_CHECKING:`, bare `...` protocol bodies, `@overload`,
    `raise NotImplementedError`, the `__main__` guard. There is no `# pragma: no cover` in `src/`, and `fail_under = 100`
    is what makes that stick. A line that is hard to cover is usually the code saying it is
    shaped wrong: the URL exemption in `domain/secrets.py` was removed because the gate
    showed no input could reach it, and `Database.count` indexes into its result rather than
    testing for a missing row precisely so there is no branch nothing can take.
13. **The FTS index is maintained by hand, and the table and the index must agree.**
    `entry_search` is a *plain* FTS5 table, not an external-content one, so every write path
    goes through `_index` or `_unindex` in `entries/sql_store.py` and nothing in the
    database enforces that. `index_agrees` exists on the `EntryStore` port for exactly this,
    and a test class asserts the two still match after create, revise, forget, purge and
    cascade. The explicit helper is what creates this bug class; the test class is the price
    of the trade.
14. **Every cap is counted inside the transaction that writes.** Entries, fields, pins and
    the event log: the count and the write are one submitted callable on one thread, so
    "count, then write" cannot go stale. A cap checked by the caller before the call is a
    cap two concurrent writes both pass.

## How we work: TDD

Every change follows red → green → refactor, and each commit leaves `make check` passing.

1. Write the test first. It should fail for the reason you expect -- check that it does.
2. Write the smallest implementation that passes.
3. Refactor with the test as a safety net.

Notes earned during this build:

- **Name tests after the behaviour.**
  `test_a_scoped_entry_is_absent_rather_than_forbidden` beats `test_search_404`.
- **Fakes are hand-written and satisfy the real `Protocol`** (`tests/fakes/`). If a port
  changes they fail to type-check, which is how you find out. There is no `unittest.mock` in
  this suite: the fake keyring is a real RSA key, a real JWKS document and a real transport,
  and its forged tokens are assembled by hand because PyJWT refuses to sign with a public
  key -- a guard on the signing side that says nothing about the verifying side.
- **Never sleep.** Three rules here are arithmetic on a date -- a token's expiry, the JWKS
  cache's age, and how long a forgotten entry survives -- and the third is measured in days.
  Move the `FakeClock`.
- **Write the "why" when it is not obvious.** The comment explaining that the `?scope=`
  filter refuses rather than returning an empty page is worth more than the assertion under
  it.
- **Assert something that could fail.** `assert await service.get_user(...)` always passes;
  strict mypy's `truthy-bool` catches it mechanically.
- **Test the outcome, not the mechanism.** The erasure test scans the file's bytes rather
  than asserting that a checkpoint was called. The first version would need rewriting for
  any change of mechanism; this one would survive a move to `VACUUM`.
- **Two situations that must look identical need a test that they do.** Cross-account,
  out-of-scope, forgotten and never-existed all answer 404 with the same body.

## Recipe: add a field key convention

The well-known keys are a **documented convention, not a schema**. Nothing enforces them,
nothing requires them, and an account that uses none of them is not malformed. What they buy
is that a model needing to know what to call somebody finds `preferred_name` instead of
inventing `name_they_go_by`.

1. Add it to `WELL_KNOWN_KEYS` in `domain/keys.py`, in the normalised form
   (`^[a-z][a-z0-9_]*$`) -- run it through `normalize_key` in your head first.
2. Update the `describe_schema` route description in `api/routers/user.py`, which lists the
   keys by name. That paragraph is what a model reads before inventing a key, so a
   convention missing from it is a convention that does not exist in practice.
3. Check it is worth it. Every well-known key is returned by `describe_schema` whether or
   not it is set, so each one costs bytes on the call an assistant is told to make before
   every write. Five prevent fifty invented keys; fifty would be a schema, which this
   deliberately is not.
4. Tests: that it appears in `describe_schema` unset, and that setting it reports
   `well_known: true` and `set: true`.

Do **not** add fuzzy matching to `normalize_key` to catch near-synonyms.
`preferred_name` and `name_preferred` stay two keys, because collapsing them needs a
judgement that module has no business making, and a store that silently merged two fields
would be much worse than one that kept two.

## Recipe: add a scope

A scope is a compartment. Adding one is configuration on both sides of the keyring
boundary, and it is only worth doing if somebody would genuinely mint a token for it.

1. Add it to `allowed_scopes` in `core/config.py`, or set `USER_API_ALLOWED_SCOPES` in the
   deployment. The startup validator refuses anything that is not lowercase alphanumeric
   with underscores -- a dot in particular, because an audience is `{prefix}.{scope}` and a
   dotted scope would make one audience parse as another.
2. Update `.env.example`, which lists the shipped set.
3. Make sure keyring will mint `user.<scope>` for it. An audience naming a scope this
   deployment does not recognise is refused outright as a 401, not treated as granting
   nothing -- so a typo on either side is loud rather than quiet, which is the point.
4. Nothing else changes. Entries carry scopes as rows in `entry_scopes`, `_VISIBLE` binds
   the one scope a token grants, and neither knows what the names mean.
5. Tests: a token granting it sees entries tagged with it and its unscoped ones; a token
   granting a different one gets 404 for the same entry and 403 for `?scope=<the new one>`;
   writing *up* to it from another scope is 403.

One scope per token, not a set. That is a property of the audience format, and changing it
would be a change to what keyring mints before it was a change here.

## Recipe: add an endpoint group

1. Create `src/user_api/api/routers/<name>.py` with
   `router = APIRouter(prefix="/v1/user", tags=["<name>"])`.
2. Add wire models in `src/user_api/api/schemas/<name>.py`. Set
   `model_config = ConfigDict(extra="forbid")` on every request body, so a field this API
   does not know about is rejected rather than ignored -- the caller is often a model, and a
   silently dropped field is a model that believes it wrote something it did not. Give every
   field a `description` and every model an `examples` entry: they are the tool
   documentation, not decoration.
3. On every route set `operation_id` (snake_case `verb_noun`, stable forever), `summary`, a
   real `description`, and `responses` for every failure a caller can provoke.
4. Register it in `ROUTERS` in `api/routers/__init__.py`. **Order matters**: everything is
   mounted at `/v1/user` and Starlette matches in order, so a literal path like
   `/v1/user/schema` must be registered before anything that could shadow it. That is the
   only wiring step; problem+json, the request id, the account binding and access logging
   are inherited.
5. Take `IdentityDep` and address every store through `identity.account_id`. Never accept an
   account id, and never accept a scope -- both come from the token.
6. Raise domain errors. Map any new one in `_DOMAIN_STATUS` in `api/errors.py`; never build
   an error response in a handler.
7. Tests: one per behaviour, an isolation test per verb proving another account gets a 404,
   a scope test proving an out-of-scope token gets the same 404, and the OpenAPI contract
   test extended with the new `operation_id`.

## Recipe: add an erasure mode

`ErasureMode` is what `DELETE` on one entry means, and it is the person's decision rather
than the caller's. Three exist; a fourth needs to be a thing somebody would actually choose.

1. Add the value to `ErasureMode` in `domain/settings.py`, with a docstring saying what a
   person choosing it is asking for.
2. Decide what it makes the two properties on `UserSettings` say. `purges_on_forget` is read
   by `UserService.forget_entry` to decide whether to destroy inside the request;
   `ever_purges` is read by the sweeper to decide whether to touch the account at all.
   Every mode is one of four combinations of those two, and a new one that is none of them
   is a new property rather than a new value.
3. Add a migration under `storage/migrations/` widening the `CHECK (erasure_mode IN (...))`
   on `user_settings` -- the database refuses the value otherwise -- then `make schema` and
   commit the snapshot diff with it. The diff *is* the review of what the migration did.
4. Update the description on `SettingsResponse.erasure_mode` in `api/schemas/settings.py`
   and the `forget_entry` route description, which both enumerate the modes for a model.
5. Keep it non-retroactive. A settings change must not destroy anything that is already
   waiting out a grace period, and must not reprieve anything already due. A settings change
   that silently destroyed data would be the worst surprise this service could produce.
6. Tests: the mode's effect on `forget_entry` in the request, its effect on a sweep with the
   clock moved past the grace period, and that switching to and from it changes nothing that
   already exists.

## Environment gotchas

- `asyncio_mode = "auto"`, so `async def test_*` needs no marker.
- `filterwarnings = ["error"]`: a new deprecation warning fails the suite. Fix it rather
  than filtering it.
- Every setting is an environment variable prefixed `USER_API_`; nested ones would use a
  double underscore. A `USER_API_`-prefixed variable that matches **no** setting is a
  **startup error**, not a warning -- `check_for_unknown_env_vars` raises before `Settings`
  is built. pydantic-settings would otherwise ignore it, and `USER_API_ALOWED_SCOPES` would
  leave the scope list on its default with nothing in the logs to say so, in a deployment
  that believed it had compartmentalised an assistant.
- Tests must never sleep. `FakeClock` is injected through the container the `app` fixture
  builds, which is the one seam the suite needs: `create_app` deliberately builds its own
  container, and `start()` honours a prebuilt one.
- Test settings are built through the `Settings` constructor rather than
  `model_copy(update=...)`, which skips validators and would accept a scope list the
  validator rejects, failing somewhere far away instead.
- `database_path` is resolved at load, so a relative path cannot mean two places after a
  `chdir`. The test suite uses a real file rather than `:memory:`, because the erasure tests
  read the file's bytes and an in-memory database has none.
- `make matrix` runs 3.11 and 3.12 because coverage differs between them: until 3.12,
  `isinstance()` against a runtime-checkable Protocol executed property getters, so a
  property with no test of its own looked covered on 3.11 and does not on 3.12.

## Commit conventions

Conventional-commit subject (`feat(scope):`, `fix(scope):`, `chore:`, `docs:`), imperative
mood, no trailing period. The body explains **why** -- the tradeoff, the failure mode being
prevented, the thing that surprised you. A reader six months from now has the diff already;
what they lack is your reasoning.

## Definition of done

- [ ] Tests were written first, and failed first.
- [ ] `make check` passes: format, lint, strict types, the four contracts, 100% coverage.
- [ ] New behaviour is covered by a test named after the behaviour.
- [ ] Anything that reads or writes an entry has an **isolation test** proving another
      account gets a 404, and a **scope test** proving an out-of-scope token gets the same
      404 rather than a 403.
- [ ] Anything that accepts text from a caller passes it through the credential refusal, or
      there is a written reason in this file why it does not.
- [ ] Nothing new can appear in a log record or a response that should not -- check
      `_CONTENT_FIELDS` in `core/logging.py`, and remember that a 422 body is logged by the
      caller.
- [ ] Anything that destroys data leaves nothing in the `-wal`: `DELETE` plus a truncating
      checkpoint, with a byte scan asserting it.
- [ ] Anything touching the schema has a migration, a regenerated `schema.sql`, and the
      snapshot diff in the same commit.
- [ ] Public HTTP changes: `operation_id`s stable, descriptions written for a model to read,
      contract test updated.
- [ ] Docs updated -- this file for workflow, `docs/` for design, an ADR for a decision that
      future-you would otherwise re-litigate.
