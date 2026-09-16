# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Breaking:** `GET /healthy` is liveness only -- the process is running, no I/O, and it
  never fails. The database and keyring checks moved to a new `GET /ready`
  (`check_readiness`), which answers 503 when keyring's keys cannot be read. Point container
  healthchecks at `/healthy` and load balancers at `/ready`.

### Added

- Optional settings-api wiring, off unless both `USER_API_SETTINGS_API_BASE_URL` and
  `USER_API_SETTINGS_API_TOKEN` are set. When on, each request that needs a pin ceiling
  or a default search page reads that caller's `user.max_pinned` and
  `user.search_default_limit` and holds them to the deployment's ceilings -- a person may
  narrow a cap and never raise it. Unset, behaviour is unchanged. `erasure_mode`,
  `grace_days` and `log_values` stay on `SqlSettingsStore` because the erasure sweeper
  has no user token to present, and `log_values` is written by the public PUT and read by
  the event log on the same row.
