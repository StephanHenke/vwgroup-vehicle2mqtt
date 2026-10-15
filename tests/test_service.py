from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vw_euda_mqtt import __version__  # noqa: E402
from vw_euda_mqtt.api import ApiError, DatasetDownload, DatasetFile  # noqa: E402
from vw_euda_mqtt.data import Dataset  # noqa: E402
from vw_euda_mqtt.service import (  # noqa: E402
    HOMEASSISTANT_BINARY_SENSOR_ENTITIES,
    HOMEASSISTANT_SENSOR_ENTITIES,
    MqttConfig,
    ServiceConfig,
    _created_on,
    _dataset_listing_retry_delay,
    _is_dataset_listing_pending,
    _list_datasets_with_retries,
    _load_state,
    _mqtt_payload,
    _next_sleep,
    _original_data_dir_path,
    _retain_for_topic,
    _safe_original_data_filename,
    _save_state,
    _select_vehicle_and_identifier,
    _state_path,
    healthcheck,
    publish_carcompat,
    publish_dataset,
    publish_homeassistant_discovery,
    publish_status,
    run_service,
    run_diagnose,
    save_original_dataset,
)


class ConfigAndStateTests(unittest.TestCase):
    def test_service_config_from_file_applies_defaults_and_mqtt_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "email": "user@example.com",
                        "password": "example-password",
                        "brand": "AUDI",
                        "vin": "",
                        "state_file": "data/state.json",
                        "mqtt": {
                            "host": "mqtt.example.local",
                            "base_topic": "/cars/euda/",
                            "publish_raw": True,
                            "publish_history": True,
                            "publish_carcompat": True,
                            "carcompat_base_topic": "/garage/",
                            "publish_homeassistant_discovery": True,
                            "homeassistant_discovery_prefix": "/ha/",
                        },
                        "save_original_data": True,
                        "original_data_dir": "archive/original",
                    }
                ),
                encoding="utf-8",
            )

            config = ServiceConfig.from_file(config_path)

        self.assertEqual(config.email, "user@example.com")
        self.assertIsNone(config.vin)
        self.assertEqual(config.portal.oidc_state, "de__de__AUDI")
        self.assertTrue(config.save_original_data)
        self.assertEqual(config.original_data_dir, "archive/original")
        self.assertEqual(config.mqtt.host, "mqtt.example.local")
        self.assertEqual(config.mqtt.base_topic, "cars/euda")
        self.assertFalse(config.mqtt.retain)
        self.assertTrue(config.mqtt.publish_raw)
        self.assertTrue(config.mqtt.publish_history)
        self.assertTrue(config.mqtt.publish_carcompat)
        self.assertEqual(config.mqtt.carcompat_base_topic, "garage")
        self.assertTrue(config.mqtt.publish_homeassistant_discovery)
        self.assertEqual(config.mqtt.homeassistant_discovery_prefix, "ha")
        self.assertTrue(config.mqtt.homeassistant_discovery_retain)

    def test_example_config_disables_retain_by_default(self) -> None:
        example = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))

        self.assertIs(example["mqtt"]["retain"], False)
        self.assertIs(example["mqtt"]["publish_history"], False)
        self.assertIs(example["mqtt"]["homeassistant_discovery_retain"], True)
        self.assertIs(example["save_original_data"], False)
        self.assertEqual(example["original_data_dir"], "data/original")

    def test_service_config_from_file_accepts_environment_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "email": "file@example.com",
                        "password": "file-password",
                        "vin": "FILEVIN1234567890",
                        "identifier": "file-identifier",
                        "mqtt": {
                            "host": "file-mqtt.local",
                            "username": "file-user",
                            "password": "file-mqtt-password",
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "VW_EUDA_EMAIL": "env@example.com",
                    "VW_EUDA_PASSWORD": "env-password",
                    "VW_EUDA_VIN": "ENVVIN12345678901",
                    "VW_EUDA_IDENTIFIER": "env-identifier",
                    "VW_EUDA_MQTT_HOST": "env-mqtt.local",
                    "VW_EUDA_MQTT_USERNAME": "env-user",
                    "VW_EUDA_MQTT_PASSWORD": "env-mqtt-password",
                },
            ):
                config = ServiceConfig.from_file(config_path)

        self.assertEqual(config.email, "env@example.com")
        self.assertEqual(config.password, "env-password")
        self.assertEqual(config.vin, "ENVVIN12345678901")
        self.assertEqual(config.identifier, "env-identifier")
        self.assertEqual(config.mqtt.host, "env-mqtt.local")
        self.assertEqual(config.mqtt.username, "env-user")
        self.assertEqual(config.mqtt.password, "env-mqtt-password")

    def test_state_path_resolves_relative_to_config_file_and_roundtrips_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            state_path = _state_path(config_path, "data/state.json")
            self.assertEqual(state_path, Path(tmp) / "data" / "state.json")

            _save_state(state_path, {"last_dataset": "dataset.zip"})
            self.assertEqual(_load_state(state_path), {"last_dataset": "dataset.zip"})
            self.assertEqual(_load_state(Path(tmp) / "missing.json"), {})

    def test_state_path_keeps_absolute_paths(self) -> None:
        absolute = Path.cwd() / "state.json"
        self.assertEqual(_state_path(Path("config.json"), str(absolute)), absolute)

    def test_original_data_dir_resolves_relative_to_config_file(self) -> None:
        self.assertEqual(
            _original_data_dir_path(Path("configs/config.json"), "data/original"),
            Path("configs/data/original"),
        )

    def test_safe_original_data_filename_removes_path_segments_and_unsafe_chars(self) -> None:
        self.assertEqual(_safe_original_data_filename("../portal dataset.zip"), "portal_dataset.zip")
        self.assertEqual(_safe_original_data_filename("nested\\dataset:1.zip"), "dataset_1.zip")
        self.assertEqual(_safe_original_data_filename(""), "dataset.zip")

    def test_save_original_dataset_writes_raw_zip_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config = ServiceConfig(
                email="user@example.com",
                password="example-password",
                save_original_data=True,
                original_data_dir="archive",
                mqtt=MqttConfig(host="mqtt.example.local"),
            )
            download = DatasetDownload(
                name="../portal dataset.zip",
                payload={"ok": True},
                files=[],
                raw=b"original zip bytes",
            )

            saved = save_original_dataset(config, config_path, download)

            self.assertEqual(saved, Path(tmp) / "archive" / "portal_dataset.zip")
            self.assertEqual(saved.read_bytes(), b"original zip bytes")

    def test_save_original_dataset_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config = ServiceConfig(
                email="user@example.com",
                password="example-password",
                mqtt=MqttConfig(host="mqtt.example.local"),
            )
            download = DatasetDownload(name="dataset.zip", payload={}, files=[], raw=b"raw")

            self.assertIsNone(save_original_dataset(config, config_path, download))
            self.assertFalse((Path(tmp) / "data").exists())

    def test_healthcheck_accepts_recent_successful_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config = ServiceConfig(
                email="user@example.com",
                password="example-password",
                poll_interval_seconds=900,
                state_file="data/state.json",
                mqtt=MqttConfig(host="mqtt.example.local"),
            )
            state_path = _state_path(config_path, config.state_file)
            _save_state(state_path, {"last_success_at": datetime.now(timezone.utc).isoformat()})

            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(healthcheck(config, config_path), 0)

    def test_healthcheck_rejects_missing_or_stale_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config = ServiceConfig(
                email="user@example.com",
                password="example-password",
                poll_interval_seconds=900,
                state_file="data/state.json",
                mqtt=MqttConfig(host="mqtt.example.local"),
            )
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(healthcheck(config, config_path), 1)

            state_path = _state_path(config_path, config.state_file)
            old = datetime.now(timezone.utc) - timedelta(hours=3)
            _save_state(state_path, {"last_success_at": old.isoformat()})
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(healthcheck(config, config_path), 1)


class SchedulingTests(unittest.TestCase):
    def test_created_on_prefers_explicit_timestamp_and_falls_back_to_filename(self) -> None:
        self.assertEqual(
            _created_on({"createdOn": "2026-01-02T03:04:05Z"}),
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _created_on({"name": "partial_20260102030405.zip"}),
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        self.assertIsNone(_created_on({"createdOn": "invalid", "name": "no_timestamp.zip"}))

    def test_next_sleep_uses_retry_when_no_timestamp_exists(self) -> None:
        self.assertEqual(_next_sleep([{"name": "no_timestamp.zip"}], 900, 60), 60)

    def test_dataset_listing_pending_only_matches_transient_delivery_errors(self) -> None:
        self.assertTrue(_is_dataset_listing_pending(ApiError("GET /datadelivery/vehicles/abc/list -> HTTP 404")))
        self.assertTrue(_is_dataset_listing_pending(ApiError("GET /datadelivery/vehicles/abc/list -> HTTP 503")))
        self.assertFalse(_is_dataset_listing_pending(ApiError("GET /vehicles -> HTTP 404")))
        self.assertFalse(_is_dataset_listing_pending(ApiError("GET /datadelivery/vehicles/abc/list -> HTTP 401")))

    def test_dataset_listing_retry_delay_uses_exponential_backoff_cap(self) -> None:
        self.assertEqual(_dataset_listing_retry_delay(1), 5)
        self.assertEqual(_dataset_listing_retry_delay(2), 10)
        self.assertEqual(_dataset_listing_retry_delay(10), 30)

    def test_dataset_listing_retries_transient_errors_and_returns_listing(self) -> None:
        client = _DatasetListingClient(
            [
                ApiError("GET /datadelivery/vehicles/abc/list -> HTTP 500"),
                ApiError("GET /datadelivery/vehicles/abc/list -> HTTP 503"),
                [{"name": "dataset.zip"}],
            ]
        )
        sleeps: list[int] = []

        async def fake_sleep(delay: int) -> None:
            sleeps.append(delay)

        listing = asyncio.run(_list_datasets_with_retries(client, "TESTVIN1234567890", "identifier123", sleep=fake_sleep))

        self.assertEqual(listing, [{"name": "dataset.zip"}])
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleeps, [5, 10])

    def test_dataset_listing_retry_does_not_swallow_non_transient_errors(self) -> None:
        client = _DatasetListingClient([ApiError("GET /datadelivery/vehicles/abc/list -> HTTP 401")])

        with self.assertRaises(ApiError):
            asyncio.run(_list_datasets_with_retries(client, "TESTVIN1234567890", "identifier123"))

        self.assertEqual(client.calls, 1)

    def test_dataset_listing_retry_exhausts_transient_errors(self) -> None:
        client = _DatasetListingClient(
            [
                ApiError("GET /datadelivery/vehicles/abc/list -> HTTP 500"),
                ApiError("GET /datadelivery/vehicles/abc/list -> HTTP 502"),
                ApiError("GET /datadelivery/vehicles/abc/list -> HTTP 504"),
            ]
        )
        sleeps: list[int] = []

        async def fake_sleep(delay: int) -> None:
            sleeps.append(delay)

        with self.assertRaises(ApiError) as ctx:
            asyncio.run(_list_datasets_with_retries(client, "TESTVIN1234567890", "identifier123", sleep=fake_sleep))

        self.assertIn("HTTP 504", str(ctx.exception))
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleeps, [5, 10])

    def test_select_vehicle_and_identifier_reuses_matching_state_values(self) -> None:
        client = _VehicleIdentifierClient()
        config = ServiceConfig(email="user@example.com", password="example-password")
        state = {"vin": "TESTVIN1234567890", "identifier": "cached-identifier"}

        vin, identifier = asyncio.run(_select_vehicle_and_identifier(client, config, state))

        self.assertEqual(vin, "TESTVIN1234567890")
        self.assertEqual(identifier, "cached-identifier")
        self.assertEqual(client.vehicle_calls, 0)
        self.assertEqual(client.metadata_calls, 0)

    def test_select_vehicle_and_identifier_does_not_reuse_identifier_for_different_configured_vin(self) -> None:
        client = _VehicleIdentifierClient(metadata={"Identifier": "fresh-identifier"})
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            vin="TESTVIN0000000001",
        )
        state = {"vin": "TESTVIN1234567890", "identifier": "cached-identifier"}

        vin, identifier = asyncio.run(_select_vehicle_and_identifier(client, config, state))

        self.assertEqual(vin, "TESTVIN0000000001")
        self.assertEqual(identifier, "fresh-identifier")
        self.assertEqual(client.vehicle_calls, 0)
        self.assertEqual(client.metadata_calls, 1)

    def test_select_vehicle_and_identifier_prefers_config_identifier(self) -> None:
        client = _VehicleIdentifierClient()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            vin="TESTVIN0000000001",
            identifier="configured-identifier",
        )
        state = {"vin": "TESTVIN1234567890", "identifier": "cached-identifier"}

        vin, identifier = asyncio.run(_select_vehicle_and_identifier(client, config, state))

        self.assertEqual(vin, "TESTVIN0000000001")
        self.assertEqual(identifier, "configured-identifier")
        self.assertEqual(client.vehicle_calls, 0)
        self.assertEqual(client.metadata_calls, 0)


class PublishTests(unittest.TestCase):
    def test_mqtt_payload_serializes_supported_values(self) -> None:
        self.assertEqual(_mqtt_payload(True), "true")
        self.assertEqual(_mqtt_payload(False), "false")
        self.assertEqual(_mqtt_payload(None), "")
        self.assertEqual(_mqtt_payload({"b": 2, "a": 1}), '{"b":2,"a":1}')
        self.assertEqual(_mqtt_payload(12.5), "12.5")

    def test_retain_for_topic_skips_heavy_payload_helpers(self) -> None:
        config = MqttConfig(host="mqtt.example.local", retain=True)

        self.assertTrue(_retain_for_topic(config, "battery/soc", 80))
        self.assertTrue(_retain_for_topic(config, "structured/by_key/soc/value", 80))
        self.assertTrue(_retain_for_topic(config, "structured/by_name/soc/description", "x"))
        self.assertTrue(_retain_for_topic(config, "raw/file/0/filename", "data.xml"))
        self.assertFalse(_retain_for_topic(config, "structured/by_key/soc/json", {}))
        self.assertFalse(_retain_for_topic(config, "raw/file/0/content", "<xml/>"))
        self.assertFalse(_retain_for_topic(config, "history/batch/json", {"events": []}))
        self.assertFalse(_retain_for_topic(MqttConfig(host="mqtt.example.local", retain=False), "battery/soc", 80))

    def test_publish_dataset_emits_status_curated_structured_raw_files_and_carcompat_topics(self) -> None:
        fake_class = _recording_publisher_class()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(
                host="mqtt.example.local",
                base_topic="vw/euda",
                retain=True,
                publish_raw=True,
                publish_carcompat=True,
                carcompat_base_topic="car",
            ),
        )
        raw_payload = {
            "Data": [
                {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "80"},
                {"key": "range", "dataFieldName": "range", "value": "321"},
                {"key": "captured", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:04:05Z"},
            ]
        }
        dataset = Dataset.from_json(raw_payload)
        download = DatasetDownload(
            name="dataset.zip",
            payload=raw_payload,
            files=[
                DatasetFile(
                    name="data.json",
                    media_type="application/json",
                    content=json.dumps(raw_payload),
                    sha256="a" * 64,
                ),
                DatasetFile(
                    name="vehicle.xml",
                    media_type="application/xml",
                    content="<vehicle><soc>80</soc></vehicle>",
                    sha256="b" * 64,
                    xml_json={"tag": "vehicle", "children": [{"tag": "soc", "text": "80"}]},
                ),
            ],
        )

        with patch("vw_euda_mqtt.service.MqttPublisher", fake_class):
            publish_dataset(config, "TESTVIN1234567890", "dataset.zip", dataset, raw_payload, download=download)

        published = dict(fake_class.instances[-1].published)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/status/online"], True)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/status/last_dataset"], "dataset.zip")
        self.assertEqual(published["vw/euda/TESTVIN1234567890/status/captured_at"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(published["vw/euda/TESTVIN1234567890/status/car_captured_at"], "2026-01-02T03:04:05+00:00")
        self.assertNotEqual(published["vw/euda/TESTVIN1234567890/status/last_poll_at"], "")
        self.assertEqual(published["vw/euda/TESTVIN1234567890/status/service_version"], __version__)
        self.assertIsInstance(published["vw/euda/TESTVIN1234567890/status/data_age_seconds"], int)
        self.assertIsInstance(published["vw/euda/TESTVIN1234567890/status/stale"], bool)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/battery/soc"], 80)
        self.assertEqual(
            published["vw/euda/TESTVIN1234567890/battery/soc/car_captured_at"],
            "2026-01-02T03:04:05+00:00",
        )
        self.assertEqual(published["vw/euda/TESTVIN1234567890/range/km"], 321)
        self.assertEqual(
            published["vw/euda/TESTVIN1234567890/range/km/car_captured_at"],
            "2026-01-02T03:04:05+00:00",
        )
        self.assertNotIn("vw/euda/TESTVIN1234567890/json", published)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/structured/by_key/range/value"], 321)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/structured/by_key/range/unit"], "km")
        self.assertEqual(published["vw/euda/TESTVIN1234567890/structured/by_name/range/value"], 321)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/structured/by_key/soc/value"], 80)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/structured/by_key/soc/unit"], "%")
        self.assertEqual(published["vw/euda/TESTVIN1234567890/structured/by_name/soc/value"], 80)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/structured/by_name/soc/key"], "soc")
        self.assertEqual(published["vw/euda/TESTVIN1234567890/structured/by_key/soc/name"], "soc")
        self.assertEqual(
            published["vw/euda/TESTVIN1234567890/structured/by_key/soc/car_captured_at"],
            "2026-01-02T03:04:05+00:00",
        )
        self.assertEqual(
            published["vw/euda/TESTVIN1234567890/structured/by_key/soc/json"]["description"],
            "Battery state of charge.",
        )
        self.assertEqual(
            published["vw/euda/TESTVIN1234567890/structured/by_name/soc/keys"],
            [{"key": "soc", "car_captured_at": "2026-01-02T03:04:05+00:00"}],
        )
        self.assertEqual(published["vw/euda/TESTVIN1234567890/raw/file/0/filename"], "data.json")
        self.assertEqual(published["vw/euda/TESTVIN1234567890/raw/file/0/content"], json.dumps(raw_payload))
        self.assertEqual(published["vw/euda/TESTVIN1234567890/raw/file/1/filename"], "vehicle.xml")
        self.assertEqual(published["vw/euda/TESTVIN1234567890/raw/file/1/content"], "<vehicle><soc>80</soc></vehicle>")
        self.assertNotIn("vw/euda/TESTVIN1234567890/raw/file/1/xml_json", published)
        self.assertNotIn("vw/euda/TESTVIN1234567890/raw/file/1/sha256", published)
        self.assertFalse(any("/raw/by_" in topic for topic in published))
        self.assertFalse(any("/raw/groups/" in topic for topic in published))
        self.assertFalse(any("/raw/files/" in topic for topic in published))
        self.assertFalse(any(value is None for value in published.values()))
        retained = fake_class.instances[-1].retained
        self.assertTrue(retained["vw/euda/TESTVIN1234567890/structured/by_key/soc/value"])
        self.assertTrue(retained["vw/euda/TESTVIN1234567890/structured/by_name/soc/value"])
        self.assertFalse(retained["vw/euda/TESTVIN1234567890/structured/by_key/soc/json"])
        self.assertTrue(retained["vw/euda/TESTVIN1234567890/raw/file/1/filename"])
        self.assertFalse(retained["vw/euda/TESTVIN1234567890/raw/file/1/content"])
        self.assertEqual(published["car/garage/TESTVIN1234567890/drives/primary/level"], 80)
        self.assertEqual(published["car/garage/TESTVIN1234567890/drives/primary/range"], 321)

    def test_publish_dataset_does_not_retain_by_default(self) -> None:
        fake_class = _recording_publisher_class()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(host="mqtt.example.local", base_topic="vw/euda", publish_raw=True),
        )
        raw_payload = {
            "Data": [
                {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "80"},
            ]
        }
        dataset = Dataset.from_json(raw_payload)

        with patch("vw_euda_mqtt.service.MqttPublisher", fake_class):
            publish_dataset(config, "TESTVIN1234567890", "dataset.zip", dataset, raw_payload)

        self.assertFalse(any(fake_class.instances[-1].retained.values()))

    def test_publish_dataset_can_emit_live_only_history_batch(self) -> None:
        fake_class = _recording_publisher_class()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(
                host="mqtt.example.local",
                base_topic="vw/euda",
                retain=True,
                publish_history=True,
            ),
        )
        raw_payload = {
            "vin": "TESTVIN1234567890",
            "Data": [
                {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "77"},
                {"key": "captured_old", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:04:05Z"},
                {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "78"},
                {"key": "captured_new", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:05:05Z"},
            ],
        }
        dataset = Dataset.from_json(raw_payload)

        with patch("vw_euda_mqtt.service.MqttPublisher", fake_class):
            publish_dataset(config, "TESTVIN1234567890", "dataset.zip", dataset, raw_payload)

        topic = "vw/euda/TESTVIN1234567890/history/batch/json"
        published = dict(fake_class.instances[-1].published)
        batch = published[topic]
        self.assertEqual(batch["event_count"], 2)
        self.assertEqual([event["value"] for event in batch["events"]], [77, 78])
        self.assertEqual(
            [event["car_captured_at"] for event in batch["events"]],
            ["2026-01-02T03:04:05+00:00", "2026-01-02T03:05:05+00:00"],
        )
        self.assertEqual(batch["events"][0]["curated_topic"], "battery/soc")
        self.assertFalse(any(event["field_name"] == "car_captured_time" for event in batch["events"]))
        self.assertFalse(fake_class.instances[-1].retained[topic])

    def test_publish_dataset_skips_history_batch_by_default(self) -> None:
        fake_class = _recording_publisher_class()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(host="mqtt.example.local", base_topic="vw/euda"),
        )
        raw_payload = {
            "Data": [
                {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "80"},
                {"key": "captured", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:04:05Z"},
            ]
        }
        dataset = Dataset.from_json(raw_payload)

        with patch("vw_euda_mqtt.service.MqttPublisher", fake_class):
            publish_dataset(config, "TESTVIN1234567890", "dataset.zip", dataset, raw_payload)

        published = dict(fake_class.instances[-1].published)
        self.assertNotIn("vw/euda/TESTVIN1234567890/history/batch/json", published)

    def test_publish_dataset_stores_last_valid_odometer(self) -> None:
        fake_class = _recording_publisher_class()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(host="mqtt.example.local", base_topic="vw/euda"),
        )
        raw_payload = {
            "Data": [
                {"key": "odometer", "dataFieldName": "mileage.value", "value": "63151"},
                {"key": "captured", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:04:05Z"},
            ]
        }
        dataset = Dataset.from_json(raw_payload)
        state: dict[str, object] = {}

        with patch("vw_euda_mqtt.service.MqttPublisher", fake_class):
            publish_dataset(config, "TESTVIN1234567890", "dataset.zip", dataset, raw_payload, state=state)

        published = dict(fake_class.instances[-1].published)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/odometer/km"], 63151)
        self.assertEqual(
            published["vw/euda/TESTVIN1234567890/odometer/km/car_captured_at"],
            "2026-01-02T03:04:05+00:00",
        )
        self.assertEqual(state["last_odometer_km"], 63151)
        self.assertEqual(state["last_odometer_car_captured_at"], "2026-01-02T03:04:05+00:00")

    def test_publish_dataset_keeps_previous_odometer_when_new_value_is_zero(self) -> None:
        fake_class = _recording_publisher_class()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(host="mqtt.example.local", base_topic="vw/euda", publish_carcompat=True),
        )
        raw_payload = {
            "Data": [
                {"key": "odometer", "dataFieldName": "mileage.value", "value": "0"},
                {"key": "captured", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:05:05Z"},
            ]
        }
        dataset = Dataset.from_json(raw_payload)
        state: dict[str, object] = {
            "last_odometer_km": 63151,
            "last_odometer_car_captured_at": "2026-01-02T03:04:05+00:00",
        }

        with patch("vw_euda_mqtt.service.MqttPublisher", fake_class):
            publish_dataset(config, "TESTVIN1234567890", "dataset.zip", dataset, raw_payload, state=state)

        published = dict(fake_class.instances[-1].published)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/odometer/km"], 63151)
        self.assertEqual(
            published["vw/euda/TESTVIN1234567890/odometer/km/car_captured_at"],
            "2026-01-02T03:04:05+00:00",
        )
        self.assertEqual(published["car/garage/TESTVIN1234567890/odometer"], 63151)
        self.assertEqual(state["last_odometer_km"], 63151)

    def test_publish_dataset_keeps_previous_odometer_when_new_value_decreases(self) -> None:
        fake_class = _recording_publisher_class()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(host="mqtt.example.local", base_topic="vw/euda"),
        )
        raw_payload = {
            "Data": [
                {"key": "odometer", "dataFieldName": "mileage.value", "value": "60000"},
            ]
        }
        dataset = Dataset.from_json(raw_payload)
        state: dict[str, object] = {"last_odometer_km": 63151}

        with patch("vw_euda_mqtt.service.MqttPublisher", fake_class):
            publish_dataset(config, "TESTVIN1234567890", "dataset.zip", dataset, raw_payload, state=state)

        published = dict(fake_class.instances[-1].published)
        self.assertEqual(published["vw/euda/TESTVIN1234567890/odometer/km"], 63151)
        self.assertEqual(state["last_odometer_km"], 63151)

    def test_publish_status_marks_errors_and_service_fallback_vin(self) -> None:
        fake_class = _recording_publisher_class()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(host="mqtt.example.local", base_topic="vw/euda"),
        )

        with patch("vw_euda_mqtt.service.MqttPublisher", fake_class):
            publish_status(config, connected=False, error="boom", error_type="ApiError")

        published = dict(fake_class.instances[-1].published)
        self.assertEqual(published["vw/euda/_service/status/online"], False)
        self.assertEqual(published["vw/euda/_service/status/connected"], False)
        self.assertEqual(published["vw/euda/_service/status/error"], "boom")
        self.assertEqual(published["vw/euda/_service/status/error_type"], "ApiError")
        self.assertNotEqual(published["vw/euda/_service/status/last_error_at"], "")
        self.assertNotEqual(published["vw/euda/_service/status/last_poll_at"], "")
        self.assertEqual(published["vw/euda/_service/status/service_version"], __version__)
        self.assertNotIn("vw/euda/_service/status/last_success_at", published)

    def test_publish_carcompat_only_emits_available_mapped_values(self) -> None:
        publisher = _StandaloneRecorder()
        publish_carcompat(
            publisher,
            MqttConfig(host="mqtt.example.local", carcompat_base_topic="car"),
            "TESTVIN1234567890",
            {"battery/soc": 80, "odometer/km": 12345, "ignored": "value"},
        )

        self.assertEqual(
            publisher.published,
            [
                ("car/garage/TESTVIN1234567890/drives/primary/level", 80),
                ("car/garage/TESTVIN1234567890/odometer", 12345),
            ],
        )

    def test_publish_homeassistant_discovery_emits_sensor_and_binary_sensor_configs(self) -> None:
        publisher = _StandaloneRecorder()
        mqtt_config = MqttConfig(
            host="mqtt.example.local",
            base_topic="vw/euda",
            homeassistant_discovery_prefix="ha",
        )

        publish_homeassistant_discovery(publisher, mqtt_config, "TESTVIN1234567890")

        published = dict(publisher.published)
        soc = published["ha/sensor/vw_euda_testvin1234567890/battery_soc/config"]
        self.assertEqual(soc["name"], "Battery SOC")
        self.assertEqual(soc["unique_id"], "vw_euda_testvin1234567890_battery_soc")
        self.assertEqual(soc["state_topic"], "vw/euda/TESTVIN1234567890/battery/soc")
        self.assertEqual(soc["device_class"], "battery")
        self.assertEqual(soc["unit_of_measurement"], "%")
        self.assertEqual(soc["state_class"], "measurement")
        self.assertEqual(soc["availability"][0]["topic"], "vw/euda/TESTVIN1234567890/status/online")
        self.assertEqual(soc["availability"][0]["payload_available"], "true")
        self.assertEqual(soc["device"]["identifiers"], ["vw_euda_TESTVIN1234567890"])
        self.assertEqual(soc["device"]["sw_version"], __version__)

        doors = published["ha/binary_sensor/vw_euda_testvin1234567890/doors_locked/config"]
        self.assertEqual(doors["state_topic"], "vw/euda/TESTVIN1234567890/doors/locked")
        self.assertEqual(doors["payload_on"], "true")
        self.assertEqual(doors["payload_off"], "false")
        self.assertEqual(doors["device_class"], "lock")
        self.assertTrue(publisher.retained["ha/sensor/vw_euda_testvin1234567890/battery_soc/config"])
        self.assertTrue(publisher.retained["ha/binary_sensor/vw_euda_testvin1234567890/doors_locked/config"])

    def test_publish_homeassistant_discovery_can_disable_discovery_retain(self) -> None:
        publisher = _StandaloneRecorder()
        mqtt_config = MqttConfig(
            host="mqtt.example.local",
            base_topic="vw/euda",
            homeassistant_discovery_prefix="ha",
            homeassistant_discovery_retain=False,
        )

        publish_homeassistant_discovery(publisher, mqtt_config, "TESTVIN1234567890")

        self.assertTrue(publisher.published)
        self.assertFalse(any(publisher.retained.values()))

    def test_publish_homeassistant_discovery_covers_all_expected_topics(self) -> None:
        publisher = _StandaloneRecorder()
        mqtt_config = MqttConfig(
            host="mqtt.example.local",
            base_topic="vw/euda-dev",
            homeassistant_discovery_prefix="/ha/",
        )
        vin = "TESTVIN1234567890"

        publish_homeassistant_discovery(publisher, mqtt_config, vin)

        published = dict(publisher.published)
        node_id = "vw_euda_testvin1234567890"
        expected_sensor_topics = {
            f"ha/sensor/{node_id}/{entity['key']}/config"
            for entity in HOMEASSISTANT_SENSOR_ENTITIES
        }
        expected_binary_topics = {
            f"ha/binary_sensor/{node_id}/{entity['key']}/config"
            for entity in HOMEASSISTANT_BINARY_SENSOR_ENTITIES
        }
        self.assertEqual(set(published), expected_sensor_topics | expected_binary_topics)

        service_version = published[f"ha/sensor/{node_id}/service_version/config"]
        self.assertEqual(service_version["state_topic"], f"vw/euda-dev/{vin}/status/service_version")
        self.assertEqual(service_version["entity_category"], "diagnostic")
        self.assertEqual(service_version["availability"][0]["topic"], f"vw/euda-dev/{vin}/status/online")
        self.assertEqual(service_version["device"]["name"], "VW Group Vehicle2MQTT 567890")
        self.assertEqual(service_version["device"]["manufacturer"], "Volkswagen Group")
        self.assertEqual(service_version["device"]["model"], "Vehicle2MQTT")
        self.assertEqual(service_version["device"]["sw_version"], __version__)

        data_age = published[f"ha/sensor/{node_id}/data_age/config"]
        self.assertEqual(data_age["state_topic"], f"vw/euda-dev/{vin}/status/data_age_seconds")
        self.assertEqual(data_age["device_class"], "duration")
        self.assertEqual(data_age["unit_of_measurement"], "s")
        self.assertEqual(data_age["state_class"], "measurement")
        self.assertEqual(data_age["entity_category"], "diagnostic")

        stale = published[f"ha/binary_sensor/{node_id}/stale/config"]
        self.assertEqual(stale["state_topic"], f"vw/euda-dev/{vin}/status/stale")
        self.assertEqual(stale["device_class"], "problem")
        self.assertEqual(stale["entity_category"], "diagnostic")
        self.assertEqual(stale["payload_on"], "true")
        self.assertEqual(stale["payload_off"], "false")

    def test_publish_dataset_can_include_homeassistant_discovery(self) -> None:
        fake_class = _recording_publisher_class()
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(
                host="mqtt.example.local",
                base_topic="vw/euda",
                publish_homeassistant_discovery=True,
            ),
        )
        raw_payload = {
            "Data": [
                {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "80"},
            ]
        }
        dataset = Dataset.from_json(raw_payload)

        with patch("vw_euda_mqtt.service.MqttPublisher", fake_class):
            publish_dataset(config, "TESTVIN1234567890", "dataset.zip", dataset, raw_payload)

        published = dict(fake_class.instances[-1].published)
        self.assertEqual(
            published["homeassistant/sensor/vw_euda_testvin1234567890/battery_soc/config"]["state_topic"],
            "vw/euda/TESTVIN1234567890/battery/soc",
        )


class RunServiceTests(unittest.TestCase):
    def test_run_service_catches_unexpected_errors_in_once_mode(self) -> None:
        config = ServiceConfig(
            email="user@example.com",
            password="example-password",
            mqtt=MqttConfig(host="mqtt.example.local"),
        )

        with (
            patch("vw_euda_mqtt.service.poll_once", side_effect=RuntimeError("boom")),
            patch("vw_euda_mqtt.service.publish_error_status") as publish_error,
            patch("vw_euda_mqtt.service.LOG.exception"),
        ):
            asyncio.run(run_service(config, Path("config.json"), once=True, dry_run=True))

        publish_error.assert_called_once()


class _StandaloneRecorder:
    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []
        self.retained: dict[str, bool] = {}

    def publish(self, topic: str, value: object, *, retain: bool | None = None) -> None:
        self.published.append((topic, value))
        self.retained[topic] = bool(retain)


class _DatasetListingClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls = 0

    async def async_list_datasets(self, vin: str, identifier: str) -> list[dict]:
        response = self.responses[self.calls]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


class _VehicleIdentifierClient:
    def __init__(self, *, vehicles: list[dict] | None = None, metadata: dict | None = None) -> None:
        self.vehicles = vehicles or [{"vin": "TESTVIN1234567890"}]
        self.metadata = metadata or {"Identifier": "metadata-identifier"}
        self.vehicle_calls = 0
        self.metadata_calls = 0

    async def async_list_vehicles(self) -> list[dict]:
        self.vehicle_calls += 1
        return self.vehicles

    async def async_get_metadata(self, vin: str) -> dict:
        self.metadata_calls += 1
        return self.metadata


def _recording_publisher_class():
    class RecordingPublisher:
        instances: list[RecordingPublisher] = []

        def __init__(self, config: MqttConfig, dry_run: bool = False) -> None:
            self.config = config
            self.dry_run = dry_run
            self.published: list[tuple[str, object]] = []
            self.retained: dict[str, bool] = {}
            self.retained_events: list[tuple[str, object, bool]] = []
            self.__class__.instances.append(self)

        def __enter__(self) -> RecordingPublisher:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def publish(self, topic: str, value: object, *, retain: bool | None = None) -> None:
            self.published.append((topic, value))
            retain_value = self.config.retain if retain is None else retain
            self.retained[topic] = retain_value
            self.retained_events.append((topic, value, retain_value))

    return RecordingPublisher


class DiagnoseDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnosis_downloads_newest_dataset_without_writing_or_publishing(self):
        config = ServiceConfig("user@example.com", "password", mqtt=MqttConfig("mqtt.example"))
        listing = [
            {"name": "new.zip", "createdOn": "2026-10-15T10:00:00Z"},
            {"name": "old.zip", "createdOn": "2026-10-14T10:00:00Z"},
            {"name": "newest_no_content_found.zip", "createdOn": "2026-10-16T10:00:00Z"},
        ]
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                client = Mock()
                client.async_login = AsyncMock()
                client.async_download_dataset = AsyncMock(
                    return_value=DatasetDownload("new.zip", {}, [], b"zip"),
                    side_effect=ApiError("download denied") if failure else None,
                )
                output, errors = io.StringIO(), io.StringIO()
                with patch("vw_euda_mqtt.service.EudaApiClient", return_value=client), \
                     patch("vw_euda_mqtt.service._select_vehicle_and_identifier", new=AsyncMock(return_value=("vin", "id"))), \
                     patch("vw_euda_mqtt.service._list_datasets_with_retries", new=AsyncMock(return_value=listing)), \
                     patch("vw_euda_mqtt.service.check_mqtt_connection"), \
                     patch("vw_euda_mqtt.service.publish_dataset") as publish, \
                     patch("vw_euda_mqtt.service._save_state") as save, \
                     patch("sys.stdout", output), patch("sys.stderr", errors):
                    result = await run_diagnose(config, Path(tmp) / "config.json", dry_run=True)
                client.async_download_dataset.assert_awaited_once_with("vin", "id", "new.zip")
                publish.assert_not_called()
                save.assert_not_called()
                self.assertEqual(result, 1 if failure else 0)
                if failure:
                    self.assertNotIn("Dataset download: ok", output.getvalue())
                    self.assertIn("download denied", errors.getvalue())
                else:
                    self.assertIn("Dataset download: ok", output.getvalue())

    async def test_diagnosis_does_not_claim_download_success_for_empty_listing(self):
        config = ServiceConfig("user@example.com", "password", mqtt=MqttConfig("mqtt.example"))
        client = Mock()
        client.async_login = AsyncMock()
        client.async_download_dataset = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with patch("vw_euda_mqtt.service.EudaApiClient", return_value=client), \
                 patch("vw_euda_mqtt.service._select_vehicle_and_identifier", new=AsyncMock(return_value=("vin", "id"))), \
                 patch("vw_euda_mqtt.service._list_datasets_with_retries", new=AsyncMock(return_value=[])), \
                 patch("vw_euda_mqtt.service.check_mqtt_connection"), patch("sys.stdout", output):
                result = await run_diagnose(config, Path(tmp) / "config.json", dry_run=True)
        self.assertEqual(result, 0)
        client.async_download_dataset.assert_not_awaited()
        self.assertIn("Dataset download: not tested", output.getvalue())


if __name__ == "__main__":
    unittest.main()
