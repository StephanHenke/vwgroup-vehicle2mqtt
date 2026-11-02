"""Config helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
from .api import PortalConfig


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


def _env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value
