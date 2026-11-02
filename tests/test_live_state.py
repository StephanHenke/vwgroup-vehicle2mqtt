from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from vw_euda_mqtt.api import ApiError, DatasetDownload, DatasetFile
from vw_euda_mqtt.config import MqttConfig, ServiceConfig
from vw_euda_mqtt.dataset import Dataset
from vw_euda_mqtt.dataset_values import curated_values
from vw_euda_mqtt.live_state import LiveState
from vw_euda_mqtt.polling import poll_once
from vw_euda_mqtt.state import _load_state, _save_state

VIN = "TESTVIN1234567890"
SOC = "506cb83e-f99f-3af3-bbeb-0429b69a78d9"
HV = "ac1108b1-b8cc-3db9-a663-03d387e42223"
PACKAGES = json.loads((Path(__file__).parent / "fixtures/continuous_partial_sequence.json").read_text())["packages"]


def merge(live, package):
    live.merge(Dataset.from_json(package["payload"]), package["name"],
               datetime.fromisoformat(package["createdOn"].replace("Z", "+00:00")))


class LiveStateTests(unittest.TestCase):
    def test_alternate_soc_wins_by_delivery_and_omission_keeps_original_source(self):
        live = LiveState({}, VIN, "test-stream")
        for package in PACKAGES:
            merge(live, package)
        values = live.values(structured=True)
        self.assertEqual(values["battery/soc"], 53)
        self.assertEqual(values["battery/soc/source_key"], HV)
        self.assertEqual(values["battery/soc/source_dataset"], "sample_hv_level.zip")
        self.assertEqual(values["battery/soc/source_delivered_at"], "2026-01-01T00:15:00+00:00")
        self.assertEqual(values["battery/soc/car_captured_at"], "")
        self.assertEqual(values[f"structured/by_key/{SOC}/value"], 39)
        self.assertEqual(values[f"structured/by_key/{HV}/value"], 53)
        self.assertEqual(values["structured/by_name/soc/key"], SOC)
        self.assertEqual(values["structured/by_name/battery_level_HV.value/key"], HV)
        self.assertEqual(values["battery/charge_power_kw"], 0)
        self.assertEqual(values["odometer/km"], 1005)
        self.assertTrue(values["doors/locked"])

    def test_restart_and_missing_soc_do_not_refresh_soc_message_age(self):
        live = LiveState({}, VIN, "test-stream")
        merge(live, PACKAGES[1])
        state = {}
        live.save(state)
        restored = LiveState(json.loads(json.dumps(state)), VIN, "test-stream")
        merge(restored, PACKAGES[2])
        self.assertEqual(restored.values()["battery/soc"], 53)
        updates = restored.values(structured=True, only_updated=True)
        self.assertNotIn("battery/soc", updates)
        self.assertNotIn(f"structured/by_key/{HV}/value", updates)
        self.assertEqual(updates["battery/target_soc"], 80)

    def test_invalid_soc_and_hv_status_keep_last_valid_value(self):
        for raw, status in (("", "VALID"), ("-1", "VALID"), ("101", "VALID"), ("NaN", "VALID"),
                            ("true", "VALID"), ("50", "INVALID"), ("50", None)):
            with self.subTest(raw=raw, status=status):
                live = LiveState({}, VIN, "test-stream")
                merge(live, PACKAGES[1])
                updated = deepcopy(PACKAGES[1])
                updated["name"] = "invalid.zip"
                updated["createdOn"] = "2026-01-01T00:40:00Z"
                updated["payload"]["Data"][0]["value"] = raw
                if status is None:
                    updated["payload"]["Data"].pop(1)
                else:
                    updated["payload"]["Data"][1]["value"] = status
                merge(live, updated)
                self.assertEqual(live.values()["battery/soc"], 53)
                self.assertEqual(live.values()["battery/soc/source_dataset"], "sample_hv_level.zip")

    def test_zero_is_a_valid_soc(self):
        package = deepcopy(PACKAGES[1])
        package["payload"]["Data"][0]["value"] = "0"
        live = LiveState({}, VIN, "test-stream")
        merge(live, package)
        self.assertEqual(live.values()["battery/soc"], 0)
        self.assertEqual(curated_values(Dataset.from_json(package["payload"]))["battery/soc"], 0)

    def test_equally_dated_sources_preserve_existing_soc_source_priority(self):
        package = deepcopy(PACKAGES[1])
        package["payload"]["Data"].append({"key": SOC, "dataFieldName": "battery_state_report.soc", "value": "42"})
        for reverse in (False, True):
            live = LiveState({}, VIN, "test-stream")
            if reverse:
                package["payload"]["Data"].reverse()
            merge(live, package)
            self.assertEqual(live.values()["battery/soc"], 42)
            self.assertEqual(curated_values(Dataset.from_json(package["payload"]))["battery/soc"], 42)

    def test_direct_extraction_recognizes_valid_hv_but_not_invalid_hv(self):
        package = deepcopy(PACKAGES[1])
        self.assertEqual(curated_values(Dataset.from_json(package["payload"]))["battery/soc"], 53)
        package["payload"]["Data"][1]["value"] = "INVALID"
        self.assertNotIn("battery/soc", curated_values(Dataset.from_json(package["payload"])))

    def test_old_delivery_cannot_overwrite_newer_untimed_value(self):
        live = LiveState({}, VIN, "test-stream")
        merge(live, PACKAGES[1])
        merge(live, PACKAGES[0])
        self.assertEqual(live.values()["battery/soc"], 53)

    def test_older_explicit_measurement_cannot_replace_newer_measurement(self):
        live = LiveState({}, VIN, "test-stream")
        merge(live, PACKAGES[0])
        package = deepcopy(PACKAGES[0])
        package["name"] = "late.zip"
        package["createdOn"] = "2026-01-01T01:00:00Z"
        package["payload"]["Data"][0].update(value="10", timestampUtc="2025-12-31T23:00:00Z")
        merge(live, package)
        self.assertEqual(live.values()["battery/soc"], 39)

    def test_cache_is_scoped_to_vehicle_and_identifier(self):
        live = LiveState({}, VIN, "test-stream")
        merge(live, PACKAGES[0])
        state = {}
        live.save(state)
        self.assertFalse(LiveState(state, "OTHERTESTVIN00000", "test-stream").values())
        self.assertFalse(LiveState(state, VIN, "different-stream").values())

    def test_shared_structured_name_selects_newest_source_without_changing_keys(self):
        live = LiveState({}, VIN, "test-stream")
        for key, value, created in (("z-old", "10", "00:00"), ("a-new", "11", "00:15")):
            package = {"name": key + ".zip", "createdOn": f"2026-01-01T{created}:00Z", "payload": {"vin": VIN, "Data": [
                {"key": key, "dataFieldName": "min_temperature", "value": value}]}}
            merge(live, package)
        values = live.values(structured=True)
        self.assertEqual(values["battery/min_temperature_c"], 11)
        self.assertEqual(values["structured/by_name/min_temperature/value"], 11)
        self.assertEqual(values["structured/by_name/min_temperature/key"], "a-new")
        self.assertEqual(values["structured/by_key/z-old/value"], 10)
        self.assertEqual(values["structured/by_key/a-new/value"], 11)

    def test_same_key_field_label_change_preserves_original_name_path(self):
        live = LiveState({}, VIN, "test-stream")
        merge(live, PACKAGES[1])
        renamed = deepcopy(PACKAGES[1])
        renamed["name"] = "renamed.zip"
        renamed["createdOn"] = "2026-01-01T00:30:00Z"
        renamed["payload"]["Data"][0].update(dataFieldName="renamed_hv_value", value="56")
        merge(live, renamed)
        values = live.values(structured=True)
        self.assertEqual(values["structured/by_name/battery_level_HV.value/value"], 56)
        self.assertNotIn("structured/by_name/renamed_hv_value/value", values)

    def test_wrong_vehicle_and_historical_sources_are_rejected(self):
        live = LiveState({}, VIN, "test-stream")
        with self.assertRaises(ValueError):
            live.merge(Dataset.from_json({"vin": "OTHERTESTVIN00000", "Data": []}), "wrong.zip", None)
        with self.assertRaises(ValueError):
            live.merge(Dataset.from_json(PACKAGES[0]["payload"]), "history.zip", None, delivery_type="historical")
        self.assertFalse(live.values())

    def test_odometer_does_not_decrease_or_reset_to_zero(self):
        live = LiveState({}, VIN, "test-stream")
        merge(live, PACKAGES[1])
        for raw in ("0", "999", "-1", "NaN"):
            package = {"name": "bad-km.zip", "createdOn": "2026-01-01T01:00:00Z", "payload": {"vin": VIN, "Data": [
                {"key": "example-odometer", "dataFieldName": "mileage.value", "value": raw}]}}
            merge(live, package)
            self.assertEqual(live.values()["odometer/km"], 1005)
            self.assertEqual(live.dataset().points["example-odometer"].value, 1005)


class _Client:
    def __init__(self, packages):
        self.packages = packages
        self.downloads = []
        self.fail = None

    async def async_list_datasets(self, vin, identifier, delivery_type="partial"):
        return [{"name": package["name"], "createdOn": package["createdOn"]} for package in reversed(self.packages)]

    async def async_download_dataset(self, vin, identifier, name):
        self.downloads.append(name)
        if name == self.fail:
            raise ApiError("Synthetic download failure")
        package = next(package for package in self.packages if package["name"] == name)
        content = json.dumps(package["payload"])
        return DatasetDownload(name=name, payload=package["payload"], files=[
            DatasetFile(name="sample.json", media_type="application/json", content=content, sha256="test")])


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class PollingLiveStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "config.json"
        self.config = ServiceConfig(email="user@example.com", password="example-password", vin=VIN,
                                    identifier="test-stream", mqtt=MqttConfig(host="mqtt.example.local", publish_raw=True,
                                    publish_history=True, publish_homeassistant_discovery=True))
        self.client = _Client(deepcopy(PACKAGES))
        self.messages = []
        messages = self.messages

        class Recorder:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def publish(self, topic, value, *, retain=None):
                messages.append((topic, value, retain))

        for target, replacement in (("vw_euda_mqtt.polling.aiohttp.ClientSession", lambda **kwargs: _Session()),
                                    ("vw_euda_mqtt.polling.EudaApiClient", lambda *args: self.client),
                                    ("vw_euda_mqtt.polling.asyncio.sleep", AsyncMock()),
                                    ("vw_euda_mqtt.publishing.MqttPublisher", Recorder)):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_poll(self):
        return asyncio.run(poll_once(self.config, self.path))

    def test_bootstrap_reads_every_package_and_publishes_final_soc_once(self):
        self.run_poll()
        self.assertEqual(self.client.downloads, [package["name"] for package in PACKAGES])
        self.assertEqual([value for topic, value, retain in self.messages if topic.endswith("/battery/soc")], [53])
        state = _load_state(self.path.parent / "state.json")
        self.assertEqual(state["live_state"]["processed"], [package["name"] for package in PACKAGES])
        histories = [value for topic, value, retain in self.messages if topic.endswith("/history/batch/json")]
        self.assertEqual([batch["event_count"] for batch in histories], [1, 0, 0])
        self.assertEqual([batch["dataset"] for batch in histories], [package["name"] for package in PACKAGES])
        self.assertTrue(all(not retain for topic, value, retain in self.messages if not topic.endswith("/config")))
        self.assertFalse(any("raw/_index" in topic or "raw/by_" in topic for topic, value, retain in self.messages))
        self.assertEqual(len([topic for topic, value, retain in self.messages if topic.endswith("raw/file/0/content")]), 3)

    def test_restart_does_not_redownload_or_refresh_omitted_soc(self):
        self.run_poll()
        self.messages.clear()
        self.client.downloads.clear()
        self.run_poll()
        self.assertFalse(self.client.downloads)
        self.assertFalse(any(topic.endswith("/battery/soc") for topic, value, retain in self.messages))
        missing = deepcopy(PACKAGES[2])
        missing["name"] = "later_without_soc.zip"
        missing["createdOn"] = "2026-01-01T00:45:00Z"
        self.client.packages.append(missing)
        self.run_poll()
        self.assertEqual(self.client.downloads, [missing["name"]])
        self.assertFalse(any(topic.endswith("/battery/soc") for topic, value, retain in self.messages))
        restored = LiveState(_load_state(self.path.parent / "state.json"), VIN, "test-stream")
        self.assertEqual(restored.values()["battery/soc"], 53)

    def test_failed_download_does_not_advance_checkpoint_or_publish_partial_live_state(self):
        self.client.fail = PACKAGES[1]["name"]
        with self.assertRaises(ApiError):
            self.run_poll()
        self.assertFalse((self.path.parent / "state.json").exists())
        self.assertFalse(self.messages)
        self.client.fail = None
        self.run_poll()
        self.assertEqual(LiveState(_load_state(self.path.parent / "state.json"), VIN, "test-stream").values()["battery/soc"], 53)

    def test_failed_publish_does_not_advance_checkpoint(self):
        with patch("vw_euda_mqtt.polling.publish_dataset", side_effect=RuntimeError("Synthetic MQTT failure")):
            with self.assertRaises(RuntimeError):
                self.run_poll()
        self.assertFalse((self.path.parent / "state.json").exists())

    def test_legacy_last_dataset_alone_does_not_skip_bootstrap(self):
        _save_state(self.path.parent / "state.json", {"vin": VIN, "identifier": "test-stream", "last_dataset": PACKAGES[-1]["name"]})
        self.run_poll()
        self.assertEqual(self.client.downloads, [package["name"] for package in PACKAGES])

    def test_atomic_state_failure_preserves_previous_checkpoint(self):
        path = self.path.parent / "state.json"
        original = {"vin": VIN, "last_dataset": "last-good.zip"}
        _save_state(path, original)
        with patch("vw_euda_mqtt.state.os.replace", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaises(OSError):
                _save_state(path, {"last_dataset": "not-persisted.zip"})
        self.assertEqual(_load_state(path), original)
        self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
