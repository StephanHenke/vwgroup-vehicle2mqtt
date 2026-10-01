# Changelog

All notable changes to this project are documented in this file.

The format follows the spirit of Keep a Changelog, and this project uses
semantic versioning.

## [Unreleased]

### Fixed

- Handle a missing localized user landing page after the portal login callback
  (issue #4). Accept HTTP 404 only when the same-origin callback was followed
  and a separate authenticated vehicle API request confirms the session.
  Apply the same check after terms and consent steps; preserve other errors.

### Changed

- Raise the minimum aiohttp version from 3.14.0 to 3.14.3 (Dependabot #3).

## [0.3.0] - 2026-06-05

### Added

- Publish datapoint-specific capture metadata under
  `<normalized-topic>/car_captured_at` when the matching `car_captured_time` is
  known.
- Add a canonical `structured/by_name/<name>/...` and
  `structured/by_key/<key>/...` MQTT tree for cataloged datapoints.
- Publish ZIP members under `raw/file/<index>/filename` and
  `raw/file/<index>/content`.
- Add optional `mqtt.publish_history` backfill publishing under
  `history/batch/json` for systems that can persist values with their original
  `car_captured_at`.
- Add openHAB history backfill examples using JavaScript Scripting and
  TimescaleDB point injection with original `car_captured_at` timestamps.
- Add optional `save_original_data` / `original_data_dir` local archiving for
  unchanged portal ZIP downloads.

### Changed

- Prefer the newest `car_captured_time` group when a normalized source field
  appears more than once in one dataset.
- Disable MQTT retain by default; `mqtt.retain=true` is now an explicit opt-in.
- Keep heavy helper payloads such as `.../json` and raw file contents live-only
  even when `mqtt.retain=true`.
- Retain Home Assistant discovery config topics independently through
  `mqtt.homeassistant_discovery_retain`, defaulting to `true` when discovery is
  enabled.
- Stop publishing delete/tombstone messages for previous raw MQTT layouts.

### Fixed

- Treat `car_captured_time` as the terminal timestamp of the preceding datapoint
  group instead of flattening it only into a global dataset timestamp.
- Reuse a matching VIN/identifier pair from the local state file when
  `config.identifier` is empty, avoiding unnecessary metadata lookups on later
  polls.

## [0.2.0] - 2026-06-05

### Changed

- Rename the public project, Docker image, repository links, and primary CLI
  command to `vwgroup-vehicle2mqtt`.
- Keep the old `vw-euda-mqtt` CLI command as a backward-compatible alias.
- Keep the Python package name `vw_euda_mqtt` and the default MQTT base topic
  `vw/euda` for compatibility with existing installations and examples.
- Redact VINs and continuous-data identifiers from API error URLs and
  `PendingData` status messages.
- Retry transient datadelivery listing failures (`HTTP 404/500/502/503/504`)
  within the same poll using bounded exponential backoff before publishing
  `PendingData`.

## [0.1.3] - 2026-06-04

### Added

- Add Home Assistant MQTT autodiscovery publishing with configurable discovery
  prefix.
- Add German/English Home Assistant MQTT autodiscovery documentation and a
  minimal config example.
- Add German/English openHAB integration documentation plus example Things and
  Items files.

## [0.1.2] - 2026-06-04

### Added

- Add German/English evcc integration documentation and an MQTT vehicle example.
- Add `--diagnose` to check configuration, MQTT connectivity, portal login,
  vehicle selection, continuous-data identifier lookup, and dataset listing
  without printing configured secrets.
- Add `--healthcheck` and a Docker `HEALTHCHECK` for recent successful dataset
  processing.
- Add environment variable overrides for account, VIN, identifier, and MQTT
  secrets.
- Add MQTT status topics for `status/last_poll_at`,
  `status/data_age_seconds`, `status/stale`, and `status/service_version`.
- Add a redacted Audi dataset fixture and GitHub issue templates for bugs and
  new data-point reports.
- Add `SECURITY.md` with secret handling and dataset redaction guidance.

### Changed

- Keep the last plausible normalized `odometer/km` value when a newer dataset
  reports `0`, a non-numeric value, or a decreasing odometer.
- Catch unexpected runtime errors in the polling loop, publish retained error
  status, and continue after the retry interval.
- Document the normalized MQTT topic table in the German and English READMEs.

### Security

- Document that real datasets, VINs, account details, and deployment secrets
  must be redacted before being shared in public issues or fixtures.

## [0.1.1] - 2026-06-04

### Changed

- Publish raw datapoints losslessly under `raw/by_key/...` and
  `raw/by_field/...` when `mqtt.publish_raw` is enabled, so repeated field names
  in real VW/Audi datasets no longer overwrite each other.
- Document the full raw MQTT topic layout and topic index.
- Publish `status/car_captured_at` as an explicit alias for the latest
  `car_captured_time` value from the vehicle dataset.

## [0.1.0] - 2026-06-01

### Added

- Initial VW/Audi EU Data Act to MQTT bridge.
- Login flow for the EU Data Act portal, including VW Identity onboarding,
  terms confirmation, client consent, optional marketing consent skip, and
  email-verification guidance.
- Configurable Audi/VW portal settings, VIN/identifier selection, polling
  intervals, retry handling, and local state tracking.
- MQTT publishing for service status, curated vehicle datapoints, optional raw
  datapoints, and optional car-compatible topics.
- Dockerfile and Docker Compose examples.
- GitHub Actions workflow for multi-architecture Docker image publishing to
  GitHub Container Registry and Docker Hub.
- Docker Hub project description and GitHub linkback.
- German and English README files.
- GitHub social preview image.
- Unit tests for API parsing helpers, dataset extraction, configuration,
  scheduling, MQTT payload formatting, and publishing behavior.

### Changed

- Removed the local MQTT host from public examples.
- Documented the current limitation that VW/Audi appears to provide the data
  delivery structure, but no confirmed path is known yet for clean, timely data
  population in the portal.
- Documented that username/password authentication is only a pragmatic current
  workaround and that an account-issued API key or token would be a cleaner
  long-term solution.

### Security

- Added `.dockerignore` so local configuration, access notes, state data,
  virtual environments, and other private files are excluded from Docker build
  contexts.
