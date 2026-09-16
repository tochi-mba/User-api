# How this is tested

`make check` is the gate: format, lint, strict types over `src` **and** `tests`, the five
architectural contracts, and the suite at 100% branch coverage. `make matrix` runs the
suite on every Python CI does, because a green run on one interpreter is one interpreter's
opinion.

```
make check     # the gate. Run before every commit.
make matrix    # 3.11 and 3.12. Run before pushing.
make test      # the suite alone
make cov       # an HTML report in htmlcov/
```

Run one group with a path: `uv run pytest tests/unit/auth -q`. Run one behaviour by name:
`uv run pytest -k "forgotten" -q`. Never pipe any of these to `head` or `tail` in a shell
chain -- it masks the exit code, which is how a broken commit slips through.

## How it is laid out

```
tests/
  conftest.py        the fixtures every test builds on
  fakes/             hand-written doubles: a clock, and a keyring
  support/           the file-mode assertion: exact on POSIX, owner bits on Windows
  unit/<layer>/      mirrors src/user_api/<layer>/
  integration/       over real HTTP, through the real app
```

`tests/unit/` mirrors `src/` exactly, so the test for a module is where you would look for
it. `tests/integration/` is organised by *property* rather than by module -- isolation,
erasure, retrieval, the contract, logging, restart -- because those are the things that
cross every module and would otherwise be nobody's to test.

## Conventions

**Tests are named after the behaviour, in a full sentence.** Not
`test_forget_404`;
`test_a_forgotten_entry_is_invisible_from_every_read_path`. The name is the
specification, and a failing test should tell you what stopped being true without opening
the file.

**Comment why a property matters when it is not obvious.** Especially where a test encodes
a security boundary or a past bug. `test_a_hyphenated_english_word_is_not_an_api_key` has a
comment saying that a bare substring match refused "risk-averse" and that there is no
override, so a false positive is a memory somebody cannot write down. That comment is worth
more than the assertion.

**Fakes are hand-written and satisfy the real Protocol.** There is no `unittest.mock` in
this suite and no patching of internals. `tests/fakes/keyring.py` is a real RSA key, a real
JWKS document and a real `httpx.MockTransport`; `tests/fakes/clock.py` is a clock that only
moves when a test moves it. A fake that satisfies a Protocol fails to type-check when the
Protocol changes. A patched attribute does not.

The one exception is `test_entrypoint.py`, which swaps `uvicorn.run` inside a
`try`/`finally` because there is no other way to assert that the entry point passes the
configured host and port. It says so in a comment.

**Never sleep.** Three rules in this service are arithmetic on a date -- token expiry, the
JWKS cache, and the grace period before erasure -- and the third is measured in days. Every
one of them is tested by moving `FakeClock`.

**Do not assert an object is merely truthy.** `assert await service.get_user(...)` always
passes. Strict mypy's `truthy-bool` catches most of these; assert the actual value.

**Separate the threshold a test is about from the ones it is not.** A test about the pinned
cap builds settings with a low pinned cap and leaves everything else generous, so it cannot
pass for the wrong reason.

## The groups that earn their keep

Most of the suite is ordinary. These are the parts worth reading before changing anything
near them.

**`tests/unit/auth/`** -- the security boundary. `TestUnknownKidRateLimit` is the one to
read first: a key id is read from a token's header *before anything has been verified*, so
it is the one value an unauthenticated caller puts in front of the verifier, and without a
floor between fetches a stream of invented ids is one outbound request to keyring per
inbound request. `TestOneRefusalForEverything` collects nineteen ways a token can be
rejected and asserts the set of error messages has exactly one element -- every distinction
a caller can tell apart is an oracle. Two of those tokens are assembled by hand because
PyJWT refuses to *mint* them; that refusal protects a signer and does nothing for a
verifier.

**`tests/integration/test_isolation.py`** -- one test per verb. What each asserts is not
that a check exists but that a caller who tries gets *exactly* what they would get for data
that never existed. `TestSearchIsolation` has its own class because the FTS index is shared
across every account: the `MATCH` alone finds other people's rows, and the account filter
lives in the outer `WHERE` of the join.

**Erasure, in `tests/unit/users/test_erasure.py` and `tests/integration/test_erasure.py`**  -- 
one test reads the raw bytes of the database file *and its `-wal`* and asserts a sentinel is
in neither. It cannot be replaced by anything else here, because it is about the file rather
than the code: `DELETE` takes the row out of the b-tree and leaves what it held in the write-ahead log. See [ADR-0005](adr/0005-erasure-is-a-setting.md).

**The hostile search table** -- the same list of punctuation in
`tests/unit/domain/test_search.py` and `tests/integration/test_retrieval.py`. Eight of the
strings raise `OperationalError` if handed to FTS5 unchanged. In the domain tests the
produced expression is run against a real in-memory FTS5 table; in the integration tests
the assertion is simply that the status is 200 or 422 and never 500.

**Index integrity, in `TestIndexIntegrity`** -- its own class, because the explicit
`_index()` helper is what creates this bug class. A plain FTS5 table is maintained by hand,
so "the index and the table disagree" is a state the schema permits and only a test
forbids.

**`tests/integration/test_no_content_in_logs.py`** -- a sentinel driven through six request
paths, captured through a processor rather than read from stdout so it is seen *after* the
redactor and before anything renders. The failure paths matter more than the success path:
an exception handler that logs its whole context, and a validation error that echoes its
input, are the two usual ways this goes wrong.

**`tests/unit/storage/test_schema.py`** -- compares the migrations against the checked-in
snapshot. The snapshot is not authoritative; the point is that a schema change shows up as
a diff in a review, because three things in that file are load-bearing in a way SQL does not
announce.

## The coverage gate

100% **branch** coverage of `src/`, enforced in CI, with no `# pragma: no cover` anywhere.
The only exclusions are non-executable lines, listed in `pyproject.toml`: `if TYPE_CHECKING:`,
bare `...` protocol bodies, `@overload`, `raise NotImplementedError`, and the `__main__`
guard.

**A line that is hard to cover is usually the code telling you it is shaped wrong.** That is
not a slogan here; it found three things during this build:

* The credential detector had an explicit URL exemption that could never run, because every
  character a URL needs is one the credential alphabet refuses a line earlier. It was
  removed. A branch no input can take is a branch that stops being true without anybody
  noticing.
* `_read_one_in` carried an optional account and an `include_forgotten` flag that each had
  exactly one value at every call site. Both are gone.
* `validate_value`'s `except` arm for un-serialisable JSON was unreachable, because
  `json.dumps` renders `NaN` happily by default. Passing `allow_nan=False` made the arm
  reachable *and* fixed a real defect.

When a line will not cover, work out which of those three it is before reaching for
anything else.

### Reading a coverage miss

`make test` prints missing lines per file. A bare number is a statement nothing reached. A
number like `134->140` is a **branch**: line 134 can jump to 140 and never does in the
suite, which usually means a condition is only ever true or only ever false. Those are the
interesting ones, and they are where the three findings above came from.

Per-module, while you work:

```
uv run pytest tests/unit/entries --cov=user_api.entries --cov-report=term-missing
```

## Environment gotchas

* `asyncio_mode = "auto"`, so `async def test_*` needs no marker and no decorator.
* `filterwarnings = ["error"]` -- a new `DeprecationWarning` fails the suite. Fix it rather
  than filtering it.
* The `database` fixture is on a **real file**, not `:memory:`. Durability is a property
  this storage exists for, and the erasure tests read the file's bytes.
* The `app` fixture builds the container itself and parks it on the app, which is the one
  seam this suite needs: `create_app` deliberately builds its own, and a test that could not
  substitute the clock could not test a grace period without waiting a month.
* A test that advances the clock past a token's lifetime will get a 401 -- the clock that
  ages a grace period is the clock the verifier checks `exp` against. `test_erasure.py`
  mints tokens that outlive the windows it tests, and says so.
* Two entries written in the same tick share a `created_at`, so an ordering assertion
  between them falls through to the random `entry_id` tiebreak. Compare as a set, or move
  the clock.

## Preferences

settings-api is its shared `FakeSettingsClient`, including with `unavailable = True`,
because the outage is the case most services forget. A person may narrow a pin or search
default and never raise it; a 401/403 from settings-api is a 503 with fixed text that names
neither the grant nor the URL; a value of the wrong type leaves the configuration and
logs the key, never the value. Two accounts on one store are held to different pin
ceilings because the cap is resolved per request, not frozen into the store at startup.

`erasure_mode`, `grace_days` and `log_values` stay on `SqlSettingsStore` on purpose: the
sweeper has no user token, and faking a dual-write would be a setting that stores a value
and changes nothing for the one path that needs it.
