# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Breaking:** the floor is now **Python 3.12** (CI runs 3.12 and 3.13).
  `.python-version`, `requires-python`, ruff's `target-version`, mypy's `python_version`,
  the Docker base image and the pre-commit interpreter all moved together, and `uv.lock`
  was regenerated. The family-wide reason is in the meta-repo's
  [ADR-0008](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0008-python-3-12-floor.md):
  `weftai`, which the assistant hub depends on, requires 3.12 and uses PEP 695 type
  parameters that do not parse on 3.11. Generics here moved to PEP 695 syntax with it.
- CI mints a short-lived family token through the family's OIDC token broker
  (`id-token: write`) rather than holding a long-lived secret; image builds accept a
  BuildKit `github_token` secret so tagged client packages can be fetched from private
  family repositories.
  `make docker` uses the signed-in GitHub account without saving its token in an image.
- **Breaking:** `GET /healthy` is liveness only -- the process is running, no I/O, and it
  never fails. The database and keyring checks moved to a new `GET /ready`
  (`check_readiness`), which answers 503 when keyring's keys cannot be read. Point container
  healthchecks at `/healthy` and load balancers at `/ready`.
- Tokens are verified by the family's shared `keyring_client`, installed from its tagged
  git source, in place of this service's own JWKS client and verifier. The rules are the
  same; one is new: while keyring cannot be reached, keys already held are served for up to
  24 hours past the one-hour cache, so a keyring outage no longer fails every request at
  once. See [docs/operations.md](docs/operations.md#when-keyring-is-down).

### Added

- A GitHub Pages site at <https://tochi-mba.github.io/User-api/>, in the REX ink/signal style: what User-api is,
  its API, how to run it and what it will not do. `site/` is plain static HTML;
  `.github/workflows/pages.yml` publishes it after `scripts/check_site.py` has checked every
  page for a broken anchor, a missing asset, an image without alt text or draft text.
- The repository is attributed to REX Technologies: the LICENSE copyright holder, the package
  author and the README.
- Optional settings-api wiring, off unless both `USER_API_SETTINGS_API_BASE_URL` and
  `USER_API_SETTINGS_API_TOKEN` are set. When on, each request that needs a pin ceiling
  or a default search page reads that caller's `user.max_pinned` and
  `user.search_default_limit` and holds them to the deployment's ceilings -- a person may
  narrow a cap and never raise it. Unset, behaviour is unchanged. `erasure_mode`,
  `grace_days` and `log_values` stay on `SqlSettingsStore` because the erasure sweeper
  has no user token to present, and `log_values` is written by the public PUT and read by
  the event log on the same row.

### Fixed

- The first write after an erasure could fail with `cannot commit transaction - SQL
  statements in progress`, depending on garbage-collector timing. The truncating checkpoint
  handed its unread cursor back out of the database worker, which kept the checkpoint
  running until something freed the cursor. It is closed on the worker now, and work that
  returns a cursor is refused with a `TypeError` (rolled back inside a transaction), so the
  pattern cannot come back as a flake.
- `order=relevance` without `q` on `search_user` is a 422 that says relevance comes from
  passing a query. It used to reach the SQL layer and answer 500.
