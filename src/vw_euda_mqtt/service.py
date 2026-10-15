"""Command-line VW Group vehicle data to MQTT bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt

from . import __version__
from .api import ApiError, AuthError, DatasetDownload, EudaApiClient, NO_CONTENT_SUFFIX, PortalConfig
from .data import (
    Dataset,
    curated_capture_values,
    curated_values,
    history_batch,
    raw_file_values,
    structured_values,
    topic_safe,
)

LOG = logging.getLogger(__name__)
HEALTHCHECK_MISSED_POLLS = 4
DATASET_LIST_RETRY_ATTEMPTS = 3
DATASET_LIST_RETRY_BASE_SECONDS = 5
DATASET_LIST_RETRY_MAX_SECONDS = 30


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "vwgroup-vehicle2mqtt"
    base_topic: str = "vw/euda"
    retain: bool = False
    qos: int = 0
    publish_raw: bool = False
    publish_history: bool = False
    publish_carcompat: bool = False
    carcompat_base_topic: str = "car"
    publish_homeassistant_discovery: bool = False
    homeassistant_discovery_prefix: str = "homeassistant"
    homeassistant_discovery_retain: bool = True


@dataclass(frozen=True)
class ServiceConfig:
    email: str
    password: str
    brand: str = "AUDI"
    country: str = "de"
    language: str = "de"
    vin: str | None = None
    identifier: str | None = None
    poll_interval_seconds: int = 900
    retry_interval_seconds: int = 60
    publish_unchanged: bool = False
    state_file: str = "state.json"
    save_original_data: bool = False
    original_data_dir: str = "data/original"
    mqtt: MqttConfig | None = None

    @classmethod
    def from_file(cls, path: Path) -> "ServiceConfig":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        mqtt_raw = raw.get("mqtt") or {}
        email = _env_value("VW_EUDA_EMAIL") or raw.get("email")
        password = _env_value("VW_EUDA_PASSWORD") or raw.get("password")
        if not email:
            raise ValueError("Missing email in config or VW_EUDA_EMAIL")
        if not password:
            raise ValueError("Missing password in config or VW_EUDA_PASSWORD")
        mqtt_config = MqttConfig(
            host=_env_value("VW_EUDA_MQTT_HOST") or mqtt_raw.get("host", "localhost"),
            port=int(mqtt_raw.get("port", 1883)),
            username=_env_value("VW_EUDA_MQTT_USERNAME") or mqtt_raw.get("username") or None,
            password=_env_value("VW_EUDA_MQTT_PASSWORD") or mqtt_raw.get("password") or None,
            client_id=mqtt_raw.get("client_id") or "vwgroup-vehicle2mqtt",
            base_topic=(mqtt_raw.get("base_topic") or "vw/euda").strip("/"),
            retain=bool(mqtt_raw.get("retain", False)),
            qos=int(mqtt_raw.get("qos", 0)),
            publish_raw=bool(mqtt_raw.get("publish_raw", False)),
            publish_history=bool(mqtt_raw.get("publish_history", False)),
            publish_carcompat=bool(mqtt_raw.get("publish_carcompat", False)),
            carcompat_base_topic=(mqtt_raw.get("carcompat_base_topic") or "car").strip("/"),
            publish_homeassistant_discovery=bool(mqtt_raw.get("publish_homeassistant_discovery", False)),
            homeassistant_discovery_prefix=(mqtt_raw.get("homeassistant_discovery_prefix") or "homeassistant").strip("/"),
            homeassistant_discovery_retain=bool(mqtt_raw.get("homeassistant_discovery_retain", True)),
        )
        return cls(
            email=email,
            password=password,
            brand=raw.get("brand") or "AUDI",
            country=raw.get("country") or "de",
            language=raw.get("language") or "de",
            vin=_env_value("VW_EUDA_VIN") or raw.get("vin") or None,
            identifier=_env_value("VW_EUDA_IDENTIFIER") or raw.get("identifier") or None,
            poll_interval_seconds=int(raw.get("poll_interval_seconds", 900)),
            retry_interval_seconds=int(raw.get("retry_interval_seconds", 60)),
            publish_unchanged=bool(raw.get("publish_unchanged", False)),
            state_file=raw.get("state_file") or "state.json",
            save_original_data=bool(raw.get("save_original_data", False)),
            original_data_dir=raw.get("original_data_dir") or "data/original",
            mqtt=mqtt_config,
        )

    @property
    def portal(self) -> PortalConfig:
        return PortalConfig(
            email=self.email,
            password=self.password,
            brand=self.brand,
            country=self.country,
            language=self.language,
        )


class MqttPublisher:
    def __init__(self, config: MqttConfig, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.client: mqtt.Client | None = None

    def __enter__(self):
        if self.dry_run:
            return self
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.config.client_id)
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)
        rc = client.connect(self.config.host, self.config.port, keepalive=60)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT connect failed: rc={rc}")
        client.loop_start()
        self.client = client
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()

    def publish(self, topic: str, value: Any, *, retain: bool | None = None) -> None:
        payload = _mqtt_payload(value)
        retain_message = self.config.retain if retain is None else retain
        if self.dry_run:
            print(f"{topic} {payload}")
            return
        assert self.client is not None
        result = self.client.publish(
            topic,
            payload,
            qos=self.config.qos,
            retain=retain_message,
        )
        result.wait_for_publish(timeout=10)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish failed for {topic}: rc={result.rc}")


def _mqtt_payload(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _force_live_topic(topic: str) -> bool:
    return (
        topic.endswith("/json")
        or topic.startswith("raw/file/") and topic.endswith("/content")
        or topic == "history/batch/json"
    )


def _retain_for_topic(config: MqttConfig, topic: str, value: Any) -> bool:
    if not config.retain:
        return False
    if _force_live_topic(topic):
        return False
    return True


def _env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value


def _state_path(config_path: Path, state_file: str) -> Path:
    path = Path(state_file)
    return path if path.is_absolute() else config_path.parent / path


def _original_data_dir_path(config_path: Path, original_data_dir: str) -> Path:
    path = Path(original_data_dir)
    return path if path.is_absolute() else config_path.parent / path


def _safe_original_data_filename(name: str) -> str:
    basename = Path(str(name).replace("\\", "/")).name
    safe = "".join(char if char.isascii() and (char.isalnum() or char in "._-") else "_" for char in basename)
    safe = safe.strip("._")
    return safe or "dataset.zip"


def save_original_dataset(config: ServiceConfig, config_path: Path, download: DatasetDownload) -> Path | None:
    if not config.save_original_data:
        return None
    if not download.raw:
        LOG.warning("Original dataset ZIP %s was not saved because the raw download bytes are empty", download.name)
        return None

    target_dir = _original_data_dir_path(config_path, config.original_data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _safe_original_data_filename(download.name)
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_bytes(download.raw)
    temp.replace(target)
    LOG.info("Saved original dataset ZIP to %s", target)
    return target


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _status_vin(config: ServiceConfig, vin: str | None = None) -> str:
    return vin or config.vin or "_service"


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _state_car_captured_at(state: dict) -> datetime | None:
    return _parse_datetime(state.get("last_car_captured_at"))


def _status_timing_values(
    config: ServiceConfig,
    *,
    now: datetime,
    car_captured_at: datetime | None,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "last_poll_at": now.isoformat(),
        "service_version": __version__,
        "data_age_seconds": "",
        "stale": "",
    }
    if car_captured_at is None:
        return values

    age = max(0, int((now - car_captured_at).total_seconds()))
    values["data_age_seconds"] = age
    values["stale"] = age > config.poll_interval_seconds * 2
    return values


def _created_on(entry: dict) -> datetime | None:
    raw = entry.get("createdOn")
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    name = entry.get("name", "").rsplit(".", 1)[0]
    for part in reversed(name.split("_")):
        try:
            return datetime.strptime(part, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _next_sleep(listing: list[dict], poll_interval_seconds: int, retry_interval_seconds: int) -> int:
    timestamps = [timestamp for entry in listing if (timestamp := _created_on(entry))]
    if not timestamps:
        return retry_interval_seconds
    newest = max(timestamps)
    target = newest + timedelta(seconds=poll_interval_seconds + 45)
    delta = target - datetime.now(timezone.utc)
    if delta.total_seconds() > 30:
        return max(30, int(delta.total_seconds()))
    return retry_interval_seconds


def _is_dataset_listing_pending(err: ApiError) -> bool:
    text = str(err)
    return (
        "/datadelivery/" in text
        and "/list" in text
        and any(f"HTTP {status}" in text for status in (404, 500, 502, 503, 504))
    )


def _dataset_listing_retry_delay(attempt_index: int) -> int:
    delay = DATASET_LIST_RETRY_BASE_SECONDS * (2 ** max(0, attempt_index - 1))
    return min(delay, DATASET_LIST_RETRY_MAX_SECONDS)


async def _list_datasets_with_retries(
    client: EudaApiClient,
    vin: str,
    identifier: str,
    *,
    sleep=asyncio.sleep,
) -> list[dict]:
    last_err: ApiError | None = None
    for attempt in range(1, DATASET_LIST_RETRY_ATTEMPTS + 1):
        try:
            return await client.async_list_datasets(vin, identifier)
        except ApiError as err:
            if not _is_dataset_listing_pending(err):
                raise
            last_err = err
            if attempt >= DATASET_LIST_RETRY_ATTEMPTS:
                break
            delay = _dataset_listing_retry_delay(attempt)
            LOG.warning(
                "Dataset listing failed transiently for identifier %s (attempt %s/%s), retrying in %ss: %s",
                _mask_identifier(identifier),
                attempt,
                DATASET_LIST_RETRY_ATTEMPTS,
                delay,
                err,
            )
            await sleep(delay)
    assert last_err is not None
    raise last_err


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _stabilize_odometer(values: dict[str, Any], state: dict) -> None:
    if "odometer/km" not in values:
        return

    current = _numeric_value(values["odometer/km"])
    previous = _numeric_value(state.get("last_odometer_km"))
    current_captured_at = values.get("odometer/km/car_captured_at")
    previous_captured_at = state.get("last_odometer_car_captured_at")

    if current is None or current <= 0:
        if previous is not None:
            LOG.warning("Ignoring implausible odometer value %r, keeping %s km", values["odometer/km"], previous)
            values["odometer/km"] = int(previous) if previous.is_integer() else previous
            if previous_captured_at:
                values["odometer/km/car_captured_at"] = previous_captured_at
            else:
                values.pop("odometer/km/car_captured_at", None)
        else:
            LOG.warning("Ignoring implausible odometer value %r without previous fallback", values["odometer/km"])
            values.pop("odometer/km", None)
            values.pop("odometer/km/car_captured_at", None)
        return

    if previous is not None and current < previous:
        LOG.warning("Ignoring decreasing odometer value %s km, keeping %s km", current, previous)
        values["odometer/km"] = int(previous) if previous.is_integer() else previous
        if previous_captured_at:
            values["odometer/km/car_captured_at"] = previous_captured_at
        else:
            values.pop("odometer/km/car_captured_at", None)
        return

    state["last_odometer_km"] = int(current) if current.is_integer() else current
    if isinstance(current_captured_at, str) and current_captured_at:
        state["last_odometer_car_captured_at"] = current_captured_at


async def _select_vehicle_and_identifier(
    client: EudaApiClient,
    config: ServiceConfig,
    state: dict | None = None,
) -> tuple[str, str]:
    state = state or {}
    state_vin = state.get("vin") if isinstance(state.get("vin"), str) else None
    state_identifier = state.get("identifier") if isinstance(state.get("identifier"), str) else None

    vin = config.vin or state_vin
    if not vin:
        vehicles = await client.async_list_vehicles()
        if not vehicles:
            raise ApiError("No vehicles returned by EU Data Act portal")
        if len(vehicles) > 1:
            names = ", ".join(f"{item.get('vin')} ({item.get('nickname') or 'no name'})" for item in vehicles)
            raise ApiError(f"Multiple vehicles found, set vin in config: {names}")
        vin = vehicles[0]["vin"]

    identifier = config.identifier
    if not identifier and state_vin == vin:
        identifier = state_identifier
    if not identifier:
        metadata = await client.async_get_metadata(vin)
        identifier = metadata.get("Identifier")
        if not identifier:
            raise ApiError(
                "No continuous-data Identifier returned. Enable customised continuous data "
                "for this VIN in the EU Data Act portal first."
            )
    return vin, identifier


async def poll_once(config: ServiceConfig, config_path: Path, dry_run: bool = False) -> int:
    state_path = _state_path(config_path, config.state_file)
    state = _load_state(state_path)
    state_car_captured_at = _state_car_captured_at(state)

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = EudaApiClient(session, config.portal)
        vin, identifier = await _select_vehicle_and_identifier(client, config, state)
        try:
            listing = await _list_datasets_with_retries(client, vin, identifier)
        except ApiError as err:
            if not _is_dataset_listing_pending(err):
                raise
            message = f"Dataset listing not available yet for identifier {_mask_identifier(identifier)}: {err}"
            LOG.warning(message)
            publish_status(
                config,
                vin,
                connected=True,
                error=message,
                error_type="PendingData",
                car_captured_at=state_car_captured_at,
                dry_run=dry_run,
            )
            return config.retry_interval_seconds
        content = sorted(
            (entry for entry in listing if entry.get("name") and not entry["name"].endswith(NO_CONTENT_SUFFIX)),
            key=lambda entry: _created_on(entry) or datetime.min.replace(tzinfo=timezone.utc),
        )
        if not content:
            message = "No content datasets available yet"
            LOG.info(message)
            publish_status(
                config,
                vin,
                connected=True,
                error=message,
                error_type="PendingData",
                car_captured_at=state_car_captured_at,
                dry_run=dry_run,
            )
            return _next_sleep(listing, config.poll_interval_seconds, config.retry_interval_seconds)

        newest = content[-1]
        name = newest["name"]
        if not config.publish_unchanged and state.get("last_dataset") == name:
            LOG.info("No new dataset: %s", name)
            publish_status(config, vin, connected=True, car_captured_at=state_car_captured_at, dry_run=dry_run)
            return _next_sleep(listing, config.poll_interval_seconds, config.retry_interval_seconds)

        download = await client.async_download_dataset(vin, identifier, name)
        if config.save_original_data and not dry_run:
            save_original_dataset(config, config_path, download)
        elif config.save_original_data:
            LOG.info("Dry-run enabled; not saving original dataset ZIP %s", name)
        dataset = Dataset.from_json(download.payload)
        publish_dataset(config, vin, name, dataset, download.payload, download=download, state=state, dry_run=dry_run)

        now = datetime.now(timezone.utc)
        state.update(
            {
                "vin": vin,
                "identifier": identifier,
                "last_dataset": name,
                "last_car_captured_at": dataset.captured_at.isoformat() if dataset.captured_at else "",
                "last_publish": now.isoformat(),
                "last_success_at": now.isoformat(),
            }
        )
        if not dry_run:
            _save_state(state_path, state)
        return _next_sleep(listing, config.poll_interval_seconds, config.retry_interval_seconds)


def publish_dataset(
    config: ServiceConfig,
    vin: str,
    dataset_name: str,
    dataset: Dataset,
    raw_payload: dict,
    *,
    download: DatasetDownload | None = None,
    state: dict | None = None,
    dry_run: bool = False,
) -> None:
    assert config.mqtt is not None
    base = f"{config.mqtt.base_topic}/{vin}"
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "status/online": True,
        "status/connected": True,
        "status/error": "",
        "status/error_type": "",
        "status/last_dataset": dataset_name,
        "status/captured_at": dataset.captured_at.isoformat() if dataset.captured_at else "",
        "status/car_captured_at": dataset.captured_at.isoformat() if dataset.captured_at else "",
        "status/last_success_at": now.isoformat(),
    }
    values.update(
        {
            f"status/{topic}": value
            for topic, value in _status_timing_values(config, now=now, car_captured_at=dataset.captured_at).items()
        }
    )
    values.update(curated_values(dataset))
    values.update(curated_capture_values(dataset))
    if state is not None:
        _stabilize_odometer(values, state)
    if config.mqtt.publish_raw:
        values.update(structured_values(dataset))
        if download is not None:
            values.update(raw_file_values(download))
    if config.mqtt.publish_history:
        values["history/batch/json"] = history_batch(dataset, dataset_name)

    with MqttPublisher(config.mqtt, dry_run=dry_run) as publisher:
        if config.mqtt.publish_homeassistant_discovery:
            publish_homeassistant_discovery(publisher, config.mqtt, vin)
        for topic, value in sorted(values.items()):
            publisher.publish(f"{base}/{topic}", value, retain=_retain_for_topic(config.mqtt, topic, value))
        if config.mqtt.publish_carcompat:
            publish_carcompat(publisher, config.mqtt, vin, values)


def publish_status(
    config: ServiceConfig,
    vin: str | None = None,
    *,
    connected: bool,
    error: str = "",
    error_type: str = "",
    car_captured_at: datetime | None = None,
    dry_run: bool = False,
) -> None:
    assert config.mqtt is not None
    base = f"{config.mqtt.base_topic}/{_status_vin(config, vin)}/status"
    now = datetime.now(timezone.utc).isoformat()
    values: dict[str, Any] = {
        "online": connected,
        "connected": connected,
        "error": error,
        "error_type": error_type,
        "last_error_at": "",
        "last_status_at": now,
    }
    values.update(_status_timing_values(config, now=datetime.fromisoformat(now), car_captured_at=car_captured_at))
    if connected:
        values["last_success_at"] = now
    else:
        values["last_error_at"] = now

    with MqttPublisher(config.mqtt, dry_run=dry_run) as publisher:
        for topic, value in sorted(values.items()):
            publisher.publish(f"{base}/{topic}", value)


def publish_error_status(
    config: ServiceConfig,
    err: Exception,
    *,
    dry_run: bool = False,
) -> None:
    try:
        publish_status(
            config,
            connected=False,
            error=str(err),
            error_type=type(err).__name__,
            dry_run=dry_run,
        )
    except Exception as publish_err:  # pragma: no cover - logging fallback only
        LOG.error("Could not publish MQTT error status: %s", publish_err)


def publish_carcompat(
    publisher: MqttPublisher,
    mqtt_config: MqttConfig,
    vin: str,
    values: dict[str, Any],
) -> None:
    base = f"{mqtt_config.carcompat_base_topic}/garage/{vin}"
    mapping = {
        "battery/soc": f"{base}/drives/primary/level",
        "range/km": f"{base}/drives/primary/range",
        "odometer/km": f"{base}/odometer",
        "charging/state": f"{base}/charging/state",
    }
    for source, target in mapping.items():
        if source in values:
            publisher.publish(target, values[source])


HOMEASSISTANT_SENSOR_ENTITIES: tuple[dict[str, Any], ...] = (
    {
        "key": "battery_soc",
        "name": "Battery SOC",
        "topic": "battery/soc",
        "device_class": "battery",
        "unit_of_measurement": "%",
        "state_class": "measurement",
    },
    {
        "key": "battery_target_soc",
        "name": "Battery Target SOC",
        "topic": "battery/target_soc",
        "unit_of_measurement": "%",
        "state_class": "measurement",
    },
    {
        "key": "battery_charge_bulk_threshold",
        "name": "Battery Charge Bulk Threshold",
        "topic": "battery/charge_bulk_threshold",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "entity_category": "diagnostic",
    },
    {
        "key": "charge_power",
        "name": "Charge Power",
        "topic": "battery/charge_power_kw",
        "device_class": "power",
        "unit_of_measurement": "kW",
        "state_class": "measurement",
    },
    {
        "key": "odometer",
        "name": "Odometer",
        "topic": "odometer/km",
        "device_class": "distance",
        "unit_of_measurement": "km",
        "state_class": "total_increasing",
    },
    {
        "key": "range",
        "name": "Range",
        "topic": "range/km",
        "device_class": "distance",
        "unit_of_measurement": "km",
        "state_class": "measurement",
    },
    {
        "key": "charging_state",
        "name": "Charging State",
        "topic": "charging/state",
    },
    {
        "key": "charging_mode",
        "name": "Charging Mode",
        "topic": "charging/mode",
    },
    {
        "key": "charging_scenario",
        "name": "Charging Scenario",
        "topic": "charging/scenario",
        "entity_category": "diagnostic",
    },
    {
        "key": "charging_action_state",
        "name": "Charging Action State",
        "topic": "charging/action_state",
        "entity_category": "diagnostic",
    },
    {
        "key": "charging_mode_selection",
        "name": "Charging Mode Selection",
        "topic": "charging/mode_selection",
        "entity_category": "diagnostic",
    },
    {
        "key": "charging_max_charge_current_ac",
        "name": "Max AC Charge Current",
        "topic": "charging/max_charge_current_ac",
        "entity_category": "diagnostic",
    },
    {
        "key": "battery_min_temperature",
        "name": "Battery Min Temperature",
        "topic": "battery/min_temperature_c",
        "device_class": "temperature",
        "unit_of_measurement": "\u00b0C",
        "state_class": "measurement",
    },
    {
        "key": "battery_max_temperature",
        "name": "Battery Max Temperature",
        "topic": "battery/max_temperature_c",
        "device_class": "temperature",
        "unit_of_measurement": "\u00b0C",
        "state_class": "measurement",
    },
    {
        "key": "climate_remaining_time",
        "name": "Climate Remaining Time",
        "topic": "climate/remaining_time_s",
        "device_class": "duration",
        "unit_of_measurement": "s",
        "state_class": "measurement",
    },
    {
        "key": "last_poll_at",
        "name": "Last Poll",
        "topic": "status/last_poll_at",
        "device_class": "timestamp",
        "entity_category": "diagnostic",
    },
    {
        "key": "last_success_at",
        "name": "Last Success",
        "topic": "status/last_success_at",
        "device_class": "timestamp",
        "entity_category": "diagnostic",
    },
    {
        "key": "car_captured_at",
        "name": "Car Captured At",
        "topic": "status/car_captured_at",
        "device_class": "timestamp",
        "entity_category": "diagnostic",
    },
    {
        "key": "data_age",
        "name": "Data Age",
        "topic": "status/data_age_seconds",
        "device_class": "duration",
        "unit_of_measurement": "s",
        "state_class": "measurement",
        "entity_category": "diagnostic",
    },
    {
        "key": "service_version",
        "name": "Service Version",
        "topic": "status/service_version",
        "entity_category": "diagnostic",
    },
)

HOMEASSISTANT_BINARY_SENSOR_ENTITIES: tuple[dict[str, Any], ...] = (
    {
        "key": "connected",
        "name": "Connected",
        "topic": "status/connected",
        "device_class": "connectivity",
        "entity_category": "diagnostic",
    },
    {
        "key": "stale",
        "name": "Data Stale",
        "topic": "status/stale",
        "device_class": "problem",
        "entity_category": "diagnostic",
    },
    {
        "key": "doors_locked",
        "name": "Doors Locked",
        "topic": "doors/locked",
        "device_class": "lock",
    },
    {
        "key": "parking_brake",
        "name": "Parking Brake",
        "topic": "parking_brake",
    },
)


def _homeassistant_device(vin: str) -> dict[str, Any]:
    return {
        "identifiers": [f"vw_euda_{vin}"],
        "name": f"VW Group Vehicle2MQTT {vin[-6:]}",
        "manufacturer": "Volkswagen Group",
        "model": "Vehicle2MQTT",
        "sw_version": __version__,
    }


def _homeassistant_base_payload(mqtt_config: MqttConfig, vin: str, entity: dict[str, Any]) -> dict[str, Any]:
    base = f"{mqtt_config.base_topic}/{vin}"
    payload: dict[str, Any] = {
        "name": entity["name"],
        "unique_id": f"vw_euda_{topic_safe(vin).lower()}_{entity['key']}",
        "state_topic": f"{base}/{entity['topic']}",
        "availability": [
            {
                "topic": f"{base}/status/online",
                "payload_available": "true",
                "payload_not_available": "false",
            }
        ],
        "device": _homeassistant_device(vin),
    }
    for key in (
        "device_class",
        "unit_of_measurement",
        "state_class",
        "entity_category",
    ):
        if entity.get(key) is not None:
            payload[key] = entity[key]
    return payload


def publish_homeassistant_discovery(
    publisher: MqttPublisher,
    mqtt_config: MqttConfig,
    vin: str,
) -> None:
    prefix = mqtt_config.homeassistant_discovery_prefix.strip("/") or "homeassistant"
    node_id = f"vw_euda_{topic_safe(vin).lower()}"

    for entity in HOMEASSISTANT_SENSOR_ENTITIES:
        payload = _homeassistant_base_payload(mqtt_config, vin, entity)
        topic = f"{prefix}/sensor/{node_id}/{entity['key']}/config"
        publisher.publish(topic, payload, retain=mqtt_config.homeassistant_discovery_retain)

    for entity in HOMEASSISTANT_BINARY_SENSOR_ENTITIES:
        payload = _homeassistant_base_payload(mqtt_config, vin, entity)
        payload["payload_on"] = "true"
        payload["payload_off"] = "false"
        topic = f"{prefix}/binary_sensor/{node_id}/{entity['key']}/config"
        publisher.publish(topic, payload, retain=mqtt_config.homeassistant_discovery_retain)


async def run_service(config: ServiceConfig, config_path: Path, once: bool, dry_run: bool) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    while not stop.is_set():
        try:
            sleep_seconds = await poll_once(config, config_path, dry_run=dry_run)
            LOG.info("Poll completed, next check in %ss", sleep_seconds)
        except AuthError as err:
            LOG.error("Authentication failed: %s", err)
            publish_error_status(config, err, dry_run=dry_run)
            sleep_seconds = config.retry_interval_seconds
        except ApiError as err:
            LOG.error("Poll failed: %s", err)
            publish_error_status(config, err, dry_run=dry_run)
            sleep_seconds = config.retry_interval_seconds
        except Exception as err:
            LOG.exception("Unexpected poll failure")
            publish_error_status(config, err, dry_run=dry_run)
            sleep_seconds = config.retry_interval_seconds
        if once:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=sleep_seconds)
        except asyncio.TimeoutError:
            pass


def healthcheck(config: ServiceConfig, config_path: Path) -> int:
    state = _load_state(_state_path(config_path, config.state_file))
    last_success = _parse_datetime(state.get("last_success_at") or state.get("last_publish"))
    if last_success is None:
        print("unhealthy: no successful dataset publish recorded", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    age = max(0, int((now - last_success).total_seconds()))
    max_age = max(config.poll_interval_seconds * HEALTHCHECK_MISSED_POLLS, 3600)
    if age <= max_age:
        print(f"healthy: last successful dataset publish {age}s ago")
        return 0

    print(f"unhealthy: last successful dataset publish {age}s ago exceeds {max_age}s", file=sys.stderr)
    return 1


def _mask_identifier(value: str | None, visible: int = 4) -> str:
    if not value:
        return "not configured"
    if len(value) <= visible * 2:
        return "<redacted>"
    return f"{value[:visible]}...{value[-visible:]}"


def _redact_text(text: str, *values: str | None) -> str:
    redacted = text
    for value in values:
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def check_mqtt_connection(config: MqttConfig, dry_run: bool = False) -> None:
    if dry_run:
        return
    with MqttPublisher(config):
        return


async def run_diagnose(config: ServiceConfig, config_path: Path, dry_run: bool = False) -> int:
    assert config.mqtt is not None
    selected_vin: str | None = None
    selected_identifier: str | None = None
    state = _load_state(_state_path(config_path, config.state_file))
    print("Configuration: ok")
    print(f"Brand: {config.brand.upper()} / locale {config.country}-{config.language}")
    print(f"Configured VIN: {_mask_identifier(config.vin)}")
    print(f"State file: {_state_path(config_path, config.state_file)}")

    try:
        check_mqtt_connection(config.mqtt, dry_run=dry_run)
        print("MQTT connection: ok" if not dry_run else "MQTT connection: skipped in dry-run")

        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
            client = EudaApiClient(session, config.portal)
            await client.async_login()
            print("Portal login: ok")
            selected_vin, selected_identifier = await _select_vehicle_and_identifier(client, config, state)
            print(f"Vehicle: {_mask_identifier(selected_vin)}")
            print(f"Continuous-data identifier: {_mask_identifier(selected_identifier)}")
            listing = await _list_datasets_with_retries(client, selected_vin, selected_identifier)
            content = [entry for entry in listing if entry.get("name") and not entry["name"].endswith(NO_CONTENT_SUFFIX)]
            print(f"Dataset listing: ok ({len(listing)} files, {len(content)} content files)")
            if content:
                newest = max(content, key=lambda entry: _created_on(entry) or datetime.min.replace(tzinfo=timezone.utc))
                download = await client.async_download_dataset(selected_vin, selected_identifier, newest["name"])
                print(f"Dataset download: ok ({len(download.raw)} bytes, {len(download.files)} extracted files)")
            else:
                print("Dataset download: not tested (no content dataset available yet)")
    except Exception as err:
        message = _redact_text(
            str(err),
            config.email,
            config.password,
            config.vin,
            config.identifier,
            selected_vin,
            selected_identifier,
        )
        print(f"Diagnosis failed: {type(err).__name__}: {message}", file=sys.stderr)
        return 1
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VW Group vehicle data to MQTT bridge")
    parser.add_argument("--config", required=True, type=Path, help="Path to config JSON")
    parser.add_argument("--once", action="store_true", help="Run one poll and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print MQTT publishes instead of sending")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--diagnose", action="store_true", help="Check portal, vehicle, dataset download and MQTT access")
    parser.add_argument("--healthcheck", action="store_true", help="Check recent successful dataset processing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = ServiceConfig.from_file(args.config)
    if args.healthcheck:
        return healthcheck(config, args.config)
    if args.diagnose:
        return asyncio.run(run_diagnose(config, args.config, dry_run=args.dry_run))
    try:
        asyncio.run(run_service(config, args.config, once=args.once, dry_run=args.dry_run))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
