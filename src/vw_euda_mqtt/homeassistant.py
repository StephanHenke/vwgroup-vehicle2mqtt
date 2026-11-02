"""Homeassistant helpers."""

from __future__ import annotations

from typing import Any
from . import __version__
from .config import MqttConfig
from .mqtt import MqttPublisher
from .data import topic_safe


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
