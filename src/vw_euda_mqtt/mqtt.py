"""MQTT publishing primitives and retain policy."""

from __future__ import annotations

import json
from typing import Any

import paho.mqtt.client as mqtt

from .config import MqttConfig


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
        if result.rc != mqtt.MQTT_ERR_SUCCESS or not result.is_published():
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


def check_mqtt_connection(config: MqttConfig, dry_run: bool = False) -> None:
    if dry_run:
        return
    with MqttPublisher(config):
        return
