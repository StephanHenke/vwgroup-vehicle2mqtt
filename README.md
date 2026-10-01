# VW Group Vehicle2MQTT

[![Tests](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/actions/workflows/tests.yml/badge.svg)](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/actions/workflows/tests.yml)
[![Docker Image](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/actions/workflows/docker-image.yml/badge.svg)](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/actions/workflows/docker-image.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/stephanhenke/vwgroup-vehicle2mqtt)](https://hub.docker.com/r/stephanhenke/vwgroup-vehicle2mqtt)

[Deutsch](README.de.md)

This project retrieves vehicle data made available through the Volkswagen Group
EU Data Act portal and forwards it in a structured form to an MQTT broker. The
goal is to make these values available to smart home systems, energy management
systems, and similar local automation platforms.

The former project name was `vw-euda-mqtt`. The new name
`vwgroup-vehicle2mqtt` describes the scope more broadly: Volkswagen Group
vehicle data forwarded to MQTT.

The service logs in to `eu-data-act.drivesomethinggreater.com`, follows the
portal's brand-specific login redirect, downloads the latest continuous-data ZIP
for a VIN, extracts useful data points, and publishes them to MQTT. MQTT retain
is disabled by default and must be enabled explicitly with `mqtt.retain=true`.

The login and dataset approach is based on
<https://github.com/mikrohard/hass-vw-eu-data-act> (MIT).

## What It Does

- Logs in with the VW Group account configured in `config.json`.
- Selects the configured brand through the portal redirect, for example
  `brand=AUDI&method=login`.
- Handles required VW/Audi Identity onboarding steps.
- Reads continuous-data metadata and dataset ZIP files.
- Skips `*_no_content_found.zip` files because they do not contain vehicle
  payload.
- Publishes normalized vehicle values and optional raw data to MQTT.
- Makes the data available for smart home and energy management use cases.

This service does not create the EU Data Act data request. It only consumes ZIP
datasets that the portal has already generated.

## Current Limitation

The service is implemented against the data transfer that Volkswagen Group
exposes through the EU Data Act portal: it logs in, reads the active data
request metadata, lists generated ZIP datasets, skips empty placeholder ZIPs,
downloads real ZIP payloads, and publishes their contents to MQTT.

In practice, Audi/VW portal tests initially only produced
`*_no_content_found.zip` placeholder files. Real vehicle datasets have since
been observed and successfully published to MQTT. The datadelivery list endpoint
can still intermittently return `HTTP 500`, so the bridge can retain the last
successfully published values in MQTT only when `mqtt.retain=true`; otherwise it
publishes live values and reports transient `PendingData` status during later
polls.

## Portal Setup

1. Open <https://eu-data-act.drivesomethinggreater.com/> in a browser.
2. Select the correct brand, for example `Audi`.
3. Log in with the same myAudi/VW account used in `config.json`.
4. Complete any terms, registration, country/language, or consent prompts.
5. Make sure the vehicle is connected and the VIN is correct.
6. Enable a continuous/customised data request with a 15-minute frequency.
7. Wait until ZIP datasets are visible for the vehicle.

Right after setup this state is normal:

```text
No content datasets available yet
```

It means login, brand selection, and metadata access work, but the portal has
only generated `*_no_content_found.zip` files so far. The service keeps polling
and will process the first ZIP with real content automatically.

## VW/Audi Identity Onboarding

Even when `brand` is set to `AUDI`, the login pages can still say
`Volkswagen ID`. Audi uses the VW Group Identity backend.

For login, the service first calls the portal's official brand redirect
endpoint, for example:

```text
/services/redirect/authentication?brand=AUDI&method=login
```

It then follows the returned OIDC authorize URL. This avoids using the wrong
Identity client for Audi vehicles.

Depending on account state, the first non-browser login can hit these pages:

- `terms-and-conditions`: required IdentityKit terms and privacy documents are
  confirmed automatically.
- `consent/users`: required OAuth scopes for basic profile and vehicle access
  are granted automatically.
- `consent/marketing`: optional marketing or personalised communication consent
  is skipped automatically.
- `verification/email-sent`: Volkswagen sent a verification email. Open the link
  in that email, then restart the service.

## Configuration

Copy the example configuration:

```bash
cp config.example.json config.json
```

Important fields:

- `email` / `password`: myAudi/VW account credentials.
- `brand`: use `AUDI` for Audi vehicles.
- `country` / `language`: locale used by the portal, for example `de`.
- `vin`: vehicle VIN. If empty, the service tries to auto-select a single
  vehicle.
- `identifier`: can stay empty. The service reads the continuous-data
  identifier from portal metadata.
- `poll_interval_seconds`: default `900`, matching 15-minute datasets.
- `save_original_data`: default `false`. When set to `true`, every downloaded
  original portal ZIP is saved locally before MQTT publishing.
- `original_data_dir`: default `data/original`. Relative paths are resolved next
  to `config.json`; in Docker this ends up below the mounted `/config/data`
  folder.
- `mqtt.host`, `mqtt.port`, `mqtt.username`, `mqtt.password`: MQTT broker
  settings.
- `mqtt.base_topic`: default `vw/euda`. This topic intentionally stays stable
  for compatibility, even though the service is now named
  `vwgroup-vehicle2mqtt`.
- `mqtt.retain`: default `false`. Set to `true` only when MQTT clients should
  receive the latest small state topics after reconnecting.
- `mqtt.publish_raw`: also publish structured datapoints under `structured/...`
  and ZIP file contents under `raw/file/<index>/...`.
- `mqtt.publish_history`: default `false`. Publishes a live-only backfill batch
  under `history/batch/json` so systems such as openHAB can persist values with
  their original `car_captured_at`.
- `mqtt.publish_carcompat`: optionally mirror selected values under
  `car/garage/<vin>/...`.
- `mqtt.publish_homeassistant_discovery`: publish Home Assistant MQTT
  autodiscovery configs.
- `mqtt.homeassistant_discovery_prefix`: discovery prefix, default
  `homeassistant`.
- `mqtt.homeassistant_discovery_retain`: default `true`. Retains Home Assistant
  discovery config topics independently from vehicle state retention.

`config.json` contains secrets and is intentionally ignored by Git.

The following environment variables override `config.json` and are useful for
Docker, Proxmox, NAS systems, and secret managers:

```text
VW_EUDA_EMAIL
VW_EUDA_PASSWORD
VW_EUDA_VIN
VW_EUDA_IDENTIFIER
VW_EUDA_MQTT_HOST
VW_EUDA_MQTT_USERNAME
VW_EUDA_MQTT_PASSWORD
```

## Local Usage

Run a setup diagnosis without printing secrets:

```bash
uv run vwgroup-vehicle2mqtt --config config.json --diagnose
```

Debug once without publishing to MQTT:

```bash
uv run vwgroup-vehicle2mqtt --config config.json --once --dry-run --debug
```

Run once without MQTT publishing:

```bash
uv run vwgroup-vehicle2mqtt --config config.json --once --dry-run
```

Run once and publish to MQTT:

```bash
uv run vwgroup-vehicle2mqtt --config config.json --once
```

Run continuously:

```bash
uv run vwgroup-vehicle2mqtt --config config.json
```

Check whether the last successful dataset publish is still recent enough for
container health monitoring:

```bash
uv run vwgroup-vehicle2mqtt --config config.json --healthcheck
```

## Docker

Prebuilt images are published to Docker Hub and GitHub Container Registry:

```bash
docker pull stephanhenke/vwgroup-vehicle2mqtt:latest
```

The same image is also available from GitHub Container Registry:

```bash
docker pull ghcr.io/stephanhenke/vwgroup-vehicle2mqtt:latest
```

The GitHub Actions workflow publishes to Docker Hub when the repository secrets
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` are set.

Use the published image with Docker Compose:

```bash
cp config.example.json config.json
cp docker-compose.example.yml docker-compose.yml
mkdir -p data
docker compose up -d
docker logs -f vwgroup-vehicle2mqtt
```

The published image includes a Docker `HEALTHCHECK`. It reads the configured
`state_file` and reports unhealthy when no successful dataset publish was
recorded or the last one is older than four polling intervals, with a minimum
threshold of one hour.

For local development, build the image from this checkout:

```bash
cp config.example.json config.json
mkdir -p data
docker compose up -d --build
docker logs -f vwgroup-vehicle2mqtt
```

Rebuild after code changes:

```bash
docker compose up -d --build
```

## MQTT Topics

Default base topic:

```text
vw/euda/<vin>/
```

Selected topics:

- `status/online`
- `status/connected`
- `status/error`
- `status/error_type`
- `status/last_status_at`
- `status/last_poll_at`
- `status/last_success_at`
- `status/last_error_at`
- `status/last_dataset`
- `status/captured_at`
- `status/car_captured_at`
- `status/data_age_seconds`
- `status/stale`
- `status/service_version`
- `battery/soc`
- `battery/target_soc`
- `battery/charge_bulk_threshold`
- `battery/charge_power_kw`
- `odometer/km`
- `range/km`
- `charging/state`
- `charging/mode`
- `charging/scenario`
- `charging/action_state`
- `charging/mode_selection`
- `charging/max_charge_current_ac`
- `doors/locked`
- `parking_brake`
- `battery/min_temperature_c`
- `battery/max_temperature_c`
- `climate/remaining_time_s`
- `<normalized-topic>/car_captured_at`

`status/car_captured_at` is derived from the payload's `car_captured_time`
entries. According to the VW/Audi data dictionary, this is a UTC timestamp for
when the report was created or sent on the vehicle-side path from ICAS1/OCU to
the backend. Real payloads contain multiple `car_captured_time` entries; each
one closes the preceding datapoint group. The service therefore publishes the
latest such value as the dataset timestamp and also exposes datapoint-specific
capture metadata under `<normalized-topic>/car_captured_at`. If a payload ends
with datapoints after the last `car_captured_time`, those trailing datapoints
are assigned to the last known capture timestamp.
`status/captured_at` remains as a compatibility alias.
`status/last_poll_at` and `status/last_success_at` are service-side timestamps.
`status/data_age_seconds` is calculated from `car_captured_at`; `status/stale`
turns `true` when the dataset is older than two configured polling intervals.

Normalized vehicle topics:

| MQTT topic | Source field | Meaning |
| --- | --- | --- |
| `battery/soc` | `battery_state_report.soc` | Battery state of charge in percent |
| `battery/target_soc` | `settings.target_soc` | Configured charging target in percent |
| `battery/charge_bulk_threshold` | `battery_state_report.charge_bulk_threshold` | Bulk charging threshold in percent |
| `battery/charge_power_kw` | `battery_state_report.charge_power` | Reported charging power |
| `odometer/km` | `mileage.value` | Odometer in kilometers, protected against `0` and decreasing values |
| `range/km` | `range` | Reported electric range if present in the dataset |
| `charging/state` | `charging_state_report.current_charge_state` | Current charging state |
| `charging/mode` | `charging_state_report.charge_mode` | Charging mode |
| `charging/scenario` | `charging_state_report.charging_scenario` | Charging scenario |
| `charging/action_state` | `charging_state_report.immediate_action_state` | Immediate charging action state |
| `charging/mode_selection` | `settings.charge_mode_selection` | Selected charge mode setting |
| `charging/max_charge_current_ac` | `settings.max_charge_current_ac` | Maximum AC charge current setting |
| `doors/locked` | `locked` | Vehicle lock state |
| `parking_brake` | `parking_brake` | Parking brake state |
| `battery/min_temperature_c` | `min_temperature` | Minimum reported battery temperature |
| `battery/max_temperature_c` | `max_temperature` | Maximum reported battery temperature |
| `climate/remaining_time_s` | `remaining_climate_time` | Remaining climate runtime in seconds |

The EU Data Act delivery endpoint returns ZIP files. If `mqtt.publish_raw` is
enabled, the service keeps two MQTT views separate:

- `structured/by_name/<datapoint_name>/...`: catalog-based datapoint view. The
  direct leaves always point to the newest value for that name.
- `structured/by_name/<datapoint_name>/keys`: JSON list of matching technical
  keys, newest first, each with `key` and `car_captured_at`.
- `structured/by_key/<datapoint_key>/...`: technical-key view of the same
  datapoint, using the raw ZIP `key` value.
- `raw/file/<index>/filename`: original ZIP member filename.
- `raw/file/<index>/content`: original ZIP member content.

Example:

```text
vw/euda/<vin>/structured/by_name/soc/key
vw/euda/<vin>/structured/by_name/soc/value
vw/euda/<vin>/structured/by_name/soc/car_captured_at
vw/euda/<vin>/structured/by_key/<datapoint_key>/name
vw/euda/<vin>/raw/file/0/filename
vw/euda/<vin>/raw/file/0/content
```

MQTT retain for vehicle, status, structured, and raw values is opt-in. By
default those topics are not retained. When `mqtt.retain=true`, small state and
structured leaves may be retained, but `.../json` and raw file `content` are
always live-only. Home Assistant discovery config topics use the separate
`mqtt.homeassistant_discovery_retain` setting.
The service does not publish delete/tombstone messages for old broker layouts.

If login or polling fails, the service publishes error state under:

```text
vw/euda/<vin>/status/...
```

If no VIN is configured, it uses:

```text
vw/euda/_service/status/...
```

## evcc Integration

A dedicated evcc integration guide is available in [evcc.md](evcc.md).
An MQTT-based evcc vehicle example is available at
[evcc/mqtt-vehicle.example.yaml](evcc/mqtt-vehicle.example.yaml).

## Home Assistant

Home Assistant MQTT autodiscovery can be enabled with
`mqtt.publish_homeassistant_discovery`. A dedicated guide is available in
[homeassistant.md](homeassistant.md). A minimal config snippet is available at
[homeassistant/mqtt-autodiscovery.example.json](homeassistant/mqtt-autodiscovery.example.json).
MQTT state updates cannot backdate Recorder history; see [history.md](history.md)
for the history backfill approach.

## openHAB

openHAB can consume the MQTT topics through the MQTT Binding as a Generic MQTT
Thing. Enable `mqtt.retain=true` if openHAB should receive the last state after
reconnect. A dedicated guide is available in [openhab.md](openhab.md).
Example Things, Items, and history backfill files are available in
[openhab/](openhab/). The original-`car_captured_at` backfill path is described
in [history.md](history.md).

## Troubleshooting

Run `--diagnose` first when setting up a new account, vehicle, MQTT broker, or
container. It checks configuration, MQTT connectivity, portal login, vehicle
selection, continuous-data identifier lookup, and the dataset list while
redacting configured secrets.

`Authentication failed: terms-and-conditions`

VW Identity still requested terms or registration. The service now handles this
automatically. If it appears again, run with `--debug` and check the landing URL.

`verification/email-sent`

Volkswagen sent a verification email. Open the link in that email, then restart
the service.

`Authentication failed: Login rejected with HTTP 404`

[Issue #4](https://github.com/StephanHenke/vwgroup-vehicle2mqtt/issues/4)
reports a missing localized landing page (`/se/en/user.html`) after the portal
login callback. This redirect trace does not indicate missing user consent.
The service accepts this specific 404 only after a portal callback and a
successful authenticated vehicle API check. Other HTTP failures remain errors.
If the session check fails, complete browser login with the same brand account,
review any required consent or email verification, and run `--diagnose` again.
Do not share cookies, authorization codes, tokens, or unredacted redirect URLs.

`No continuous-data Identifier returned`

The VIN does not have an active continuous/customised data request in the EU
Data Act portal.

`No content datasets available yet`

Login, brand selection, and identifier lookup work. The portal did not provide a
ZIP with real content for this poll. Wait until a ZIP with real content is
available; the service will process it automatically once the portal provides a
non-empty dataset.

`HTTP 401`

Usually means the wrong brand/Identity client was used or the portal login
expired. Run with `--debug` and check whether login lands on `/de/de/user.html`.

`HTTP 500` while listing datasets

Can be a transient portal backend error. The service retries the datadelivery
list endpoint several times within the same poll using short bounded
exponential backoff. Only after those attempts fail does it publish
`PendingData` and retry the full poll after `retry_interval_seconds`.

## Security Notes

See [SECURITY.md](SECURITY.md) for responsible disclosure and data redaction
guidance.

Using account email and password is a pragmatic workaround for the current
browser-oriented portal flow. It is not the preferred long-term design. In this
project's view, a cleaner solution would be a dedicated API key or token issued
through the user's VW/Audi account, so third-party services can be authorized
without storing the account password.

- `config.json` is ignored because it contains account and MQTT credentials.
- `access.txt` is ignored because it can contain deployment host credentials.
- `data/` is ignored and only stores runtime state.
- Do not commit real passwords, personal VINs, or tokens if the repository is
  public.
- Real datasets used for tests or issues must be redacted like
  `tests/fixtures/audi_dataset_redacted.json`.
