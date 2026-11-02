"""Publishing helpers."""

from __future__ import annotations

from datetime import datetime,timezone
from typing import Any
import logging
from . import __version__
from .api import DatasetDownload
from .config import ServiceConfig,MqttConfig
from .data import Dataset,curated_values,curated_capture_values,structured_values,raw_file_values,history_batch
from .mqtt import MqttPublisher,_retain_for_topic
from .homeassistant import publish_homeassistant_discovery
from .state import _stabilize_odometer
from .live_state import LiveState
LOG=logging.getLogger(__name__)


def _status_vin(config: ServiceConfig, vin: str | None = None) -> str:
    return vin or config.vin or "_service"


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
    live_state: LiveState | None = None,
    publish_live: bool = True,
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
    if not publish_live:
        values.clear()
    elif live_state is not None:
        values.update(live_state.values(structured=config.mqtt.publish_raw, only_updated=not config.publish_unchanged))
    else:
        values.update(curated_values(dataset))
        values.update(curated_capture_values(dataset))
    if state is not None and publish_live:
        _stabilize_odometer(values, state)
    if config.mqtt.publish_raw:
        if publish_live and live_state is None:
            values.update(structured_values(dataset))
        if download is not None:
            values.update(raw_file_values(download))
    if config.mqtt.publish_history:
        values["history/batch/json"] = history_batch(dataset, dataset_name)

    if not values:
        return

    with MqttPublisher(config.mqtt, dry_run=dry_run) as publisher:
        if config.mqtt.publish_homeassistant_discovery and publish_live:
            publish_homeassistant_discovery(publisher, config.mqtt, vin)
        for topic, value in sorted(values.items()):
            publisher.publish(f"{base}/{topic}", value, retain=_retain_for_topic(config.mqtt, topic, value))
        if config.mqtt.publish_carcompat and publish_live:
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
