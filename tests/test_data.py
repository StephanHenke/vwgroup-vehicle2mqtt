from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vw_euda_mqtt.api import DatasetDownload, DatasetFile  # noqa: E402
from vw_euda_mqtt.data import (  # noqa: E402
    Dataset,
    curated_capture_values,
    curated_values,
    history_batch,
    parse_timestamp,
    parse_value,
    raw_file_values,
    structured_values,
    topic_safe,
)


class ValueParsingTests(unittest.TestCase):
    def test_parse_value_converts_common_scalar_types(self) -> None:
        cases = {
            None: None,
            "": None,
            "  ": None,
            "true": True,
            "FALSE": False,
            "42": 42,
            "-7": -7,
            "12.5": 12.5,
            "-3.25": -3.25,
            "90 s": 90.0,
            "1.5S": 1.5,
            "ready": "ready",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_value(raw), expected)

    def test_parse_timestamp_accepts_epoch_milliseconds_and_iso_strings(self) -> None:
        self.assertEqual(
            parse_timestamp("1700000000000"),
            datetime.fromtimestamp(1700000000, tz=timezone.utc),
        )
        self.assertEqual(
            parse_timestamp("2026-01-02T03:04:05Z"),
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        self.assertIsNone(parse_timestamp(""))
        self.assertIsNone(parse_timestamp("not-a-timestamp"))

    def test_topic_safe_removes_mqtt_topic_unfriendly_characters(self) -> None:
        self.assertEqual(topic_safe(" range / km "), "range_km")
        self.assertEqual(topic_safe("battery.state"), "battery.state")
        self.assertEqual(topic_safe("###"), "unknown")


class DatasetTests(unittest.TestCase):
    def test_dataset_from_json_parses_points_groups_and_latest_capture_time(self) -> None:
        dataset = Dataset.from_json(
            {
                "vin": "TESTVIN1234567890",
                "user_id": "user-1",
                "Data": [
                    {"key": "soc_old", "dataFieldName": "battery_state_report.soc", "value": "77", "timestampUtc": "2026-01-02T03:04:05Z"},
                    {"key": "captured_old", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:04:05Z"},
                    {"dataFieldName": "ignored-no-key", "value": "123"},
                    {"key": "soc_new", "dataFieldName": "battery_state_report.soc", "value": "78", "timestampUtc": "2026-01-02T03:05:05Z"},
                    {"key": "captured_new", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:05:05Z"},
                ],
            }
        )

        old_capture = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        new_capture = datetime(2026, 1, 2, 3, 5, 5, tzinfo=timezone.utc)
        self.assertEqual(dataset.vin, "TESTVIN1234567890")
        self.assertEqual(dataset.user_id, "user-1")
        self.assertEqual(dataset.captured_at, new_capture)
        self.assertEqual(len(dataset.groups), 2)
        self.assertEqual([point.key for point in dataset.groups[0].points], ["captured_old", "soc_old"])
        self.assertEqual([point.key for point in dataset.groups[1].points], ["captured_new", "soc_new"])
        self.assertEqual(dataset.points["soc_old"].group_index, 0)
        self.assertEqual(dataset.points["soc_old"].car_captured_at, old_capture)
        self.assertEqual(dataset.points["captured_new"].car_captured_at, new_capture)
        self.assertEqual(dataset.by_field("battery_state_report.soc").value, 78)
        self.assertIsNone(dataset.by_field("missing"))

    def test_by_field_returns_stable_lowest_key_match(self) -> None:
        dataset = Dataset.from_json(
            {
                "Data": [
                    {"key": "z", "dataFieldName": "range", "value": "300"},
                    {"key": "a", "dataFieldName": "range", "value": "301"},
                ]
            }
        )

        self.assertEqual(dataset.by_field("range").key, "a")
        self.assertEqual(dataset.by_field("range").value, 301)

    def test_by_field_prefers_latest_explicit_measurement_time(self) -> None:
        dataset = Dataset.from_json(
            {
                "Data": [
                    {"key": "newer_lexically", "dataFieldName": "range", "value": "300", "timestampUtc": "2026-01-02T03:04:05Z"},
                    {"key": "captured_old", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:04:05Z"},
                    {"key": "a_older_lexically", "dataFieldName": "range", "value": "301", "timestampUtc": "2026-01-02T03:05:05Z"},
                    {"key": "captured_new", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:05:05Z"},
                ]
            }
        )

        self.assertEqual(dataset.by_field("range").key, "a_older_lexically")
        self.assertEqual(dataset.by_field("range").value, 301)

    def test_neither_preceding_nor_trailing_points_inherit_capture_time(self) -> None:
        dataset = Dataset.from_json(
            {
                "Data": [
                    {"key": "range", "dataFieldName": "range", "value": "300"},
                    {"key": "captured", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:04:05Z"},
                    {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "80"},
                ]
            }
        )

        captured_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(len(dataset.groups), 2)
        self.assertEqual([point.key for point in dataset.groups[0].points], ["captured"])
        self.assertEqual([point.key for point in dataset.groups[1].points], ["range", "soc"])
        self.assertEqual(dataset.points["captured"].car_captured_at, captured_at)
        self.assertIsNone(dataset.points["soc"].car_captured_at)
        self.assertIsNone(dataset.points["range"].car_captured_at)
        self.assertEqual(curated_capture_values(dataset), {})
        self.assertEqual(history_batch(dataset, "flat.zip")["events"], [])

    def test_curated_and_structured_values_use_expected_topics(self) -> None:
        dataset = Dataset.from_json(
            {
                "Data": [
                    {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "80", "timestampUtc": "2026-01-02T03:04:05Z"},
                    {"key": "range", "dataFieldName": "range", "value": "321", "timestampUtc": "2026-01-02T03:04:05Z"},
                    {"key": "door", "dataFieldName": "locked", "value": "true", "timestampUtc": "2026-01-02T03:04:05Z"},
                    {"key": "odd", "dataFieldName": "bad field/name", "value": "text", "timestampUtc": "2026-01-02T03:04:05Z"},
                    {"key": "captured", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:04:05Z"},
                ]
            }
        )

        self.assertEqual(
            curated_values(dataset),
            {
                "battery/soc": 80,
                "range/km": 321,
                "doors/locked": True,
            },
        )
        self.assertEqual(
            curated_capture_values(dataset),
            {
                "battery/soc/car_captured_at": "2026-01-02T03:04:05+00:00",
                "range/km/car_captured_at": "2026-01-02T03:04:05+00:00",
                "doors/locked/car_captured_at": "2026-01-02T03:04:05+00:00",
            },
        )
        structured = structured_values(dataset)
        self.assertEqual(structured["structured/by_key/odd/value"], "text")
        self.assertEqual(structured["structured/by_key/odd/name"], "bad field/name")
        self.assertEqual(structured["structured/by_key/odd/unit"], "")
        self.assertEqual(structured["structured/by_key/odd/description"], "")
        self.assertEqual(structured["structured/by_key/odd/car_captured_at"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(structured["structured/by_key/odd/field_name"], "bad field/name")
        self.assertEqual(structured["structured/by_key/odd/group_index"], 0)
        self.assertEqual(structured["structured/by_key/odd/key"], "odd")
        self.assertEqual(structured["structured/by_key/odd/json"]["value"], "text")
        self.assertEqual(structured["structured/by_key/odd/json"]["name"], "bad field/name")
        self.assertEqual(structured["structured/by_key/odd/json"]["raw_value"], "text")
        self.assertEqual(structured["structured/by_key/odd/json"]["car_captured_at"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(structured["structured/by_key/odd/json"]["topics"]["by_key"], "structured/by_key/odd")
        self.assertEqual(structured["structured/by_name/bad_field_name/value"], "text")
        self.assertEqual(structured["structured/by_name/bad_field_name/key"], "odd")
        self.assertEqual(structured["structured/by_name/bad_field_name/car_captured_at"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(structured["structured/by_name/range/unit"], "km")
        self.assertEqual(structured["structured/by_name/range/description"], "Reported electric range.")
        self.assertEqual(
            structured["structured/by_name/bad_field_name/keys"],
            [{"key": "odd", "car_captured_at": "2026-01-02T03:04:05+00:00"}],
        )
        self.assertFalse(any(value is None for value in structured.values()))
        self.assertFalse(any(topic.startswith("raw/") for topic in structured))
        self.assertFalse(any(topic.startswith("structured/groups/") for topic in structured))

    def test_structured_values_keep_duplicate_names_without_overwriting(self) -> None:
        dataset = Dataset.from_json(
            {
                "Data": [
                    {"key": "timestamp_a", "dataFieldName": "timestamp", "value": "1"},
                    {"key": "timestamp_b", "dataFieldName": "timestamp", "value": "2"},
                ]
            }
        )

        structured = structured_values(dataset)

        self.assertEqual(structured["structured/by_key/timestamp_a/value"], 1)
        self.assertEqual(structured["structured/by_key/timestamp_b/value"], 2)
        self.assertEqual(structured["structured/by_name/timestamp/value"], 2)
        self.assertEqual(structured["structured/by_name/timestamp/key"], "timestamp_b")
        self.assertEqual(structured["structured/by_key/timestamp_a/name"], "timestamp")
        self.assertEqual(structured["structured/by_key/timestamp_b/name"], "timestamp")
        self.assertEqual(structured["structured/by_key/timestamp_a/car_captured_at"], "")
        self.assertEqual(structured["structured/by_key/timestamp_b/car_captured_at"], "")
        self.assertEqual(
            structured["structured/by_name/timestamp/keys"],
            [
                {"key": "timestamp_b", "car_captured_at": ""},
                {"key": "timestamp_a", "car_captured_at": ""},
            ],
        )
        self.assertNotIn("structured/by_name/timestamp/timestamp_a/value", structured)
        self.assertNotIn("raw/_index", structured)

    def test_history_batch_keeps_duplicate_keys_with_original_capture_times(self) -> None:
        dataset = Dataset.from_json(
            {
                "vin": "TESTVIN1234567890",
                "Data": [
                    {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "77", "timestampUtc": "2026-01-02T03:04:05Z"},
                    {"key": "captured_old", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:04:05Z"},
                    {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "78", "timestampUtc": "2026-01-02T03:05:05Z"},
                    {"key": "captured_new", "dataFieldName": "car_captured_time", "value": "2026-01-02T03:05:05Z"},
                    {"key": "orphan", "dataFieldName": "range", "value": "300"},
                ],
            }
        )

        batch = history_batch(dataset, "dataset.zip")

        self.assertEqual(batch["vin"], "TESTVIN1234567890")
        self.assertEqual(batch["dataset"], "dataset.zip")
        self.assertEqual(batch["event_count"], 2)
        values = [event["value"] for event in batch["events"]]
        self.assertEqual(values, [77, 78])
        self.assertEqual(
            [event["car_captured_at"] for event in batch["events"]],
            [
                "2026-01-02T03:04:05+00:00",
                "2026-01-02T03:05:05+00:00",
            ],
        )
        self.assertEqual(batch["events"][0]["curated_topic"], "battery/soc")
        self.assertEqual(batch["events"][0]["structured_by_key_topic"], "structured/by_key/soc")
        self.assertEqual(batch["events"][0]["structured_by_name_topic"], "structured/by_name/soc")
        self.assertEqual(batch["events"][0]["unit"], "%")
        self.assertTrue(all(event["field_name"] != "car_captured_time" for event in batch["events"]))
        self.assertEqual(len({event["event_id"] for event in batch["events"]}), 2)
        self.assertEqual(dataset.by_field("battery_state_report.soc").value, 78)

    def test_history_batch_excludes_points_without_capture_time(self) -> None:
        dataset = Dataset.from_json(
            {
                "Data": [
                    {"key": "range", "dataFieldName": "range", "value": "300"},
                ]
            }
        )

        batch = history_batch(dataset, "dataset.zip")

        self.assertEqual(batch["event_count"], 0)
        self.assertEqual(batch["events"], [])

    def test_redacted_audi_fixture_exposes_expected_normalized_topics(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "audi_dataset_redacted.json"
        dataset = Dataset.from_json(json.loads(fixture.read_text(encoding="utf-8")))

        values = curated_values(dataset)
        self.assertEqual(values["battery/soc"], 52)
        self.assertEqual(values["battery/target_soc"], 100)
        self.assertEqual(values["battery/charge_power_kw"], 0)
        self.assertEqual(values["odometer/km"], 63151)
        self.assertEqual(values["charging/state"], "not_charging")
        self.assertEqual(values["charging/mode"], "manual")
        self.assertEqual(values["charging/scenario"], "default")
        self.assertEqual(values["doors/locked"], True)
        self.assertEqual(values["parking_brake"], True)
        self.assertEqual(values["battery/min_temperature_c"], 17.5)
        self.assertEqual(values["battery/max_temperature_c"], 19.0)
        self.assertEqual(values["climate/remaining_time_s"], 0.0)

        structured = structured_values(dataset)
        soc_key = "506cb83e-f99f-3af3-bbeb-0429b69a78d9"
        self.assertEqual(structured["structured/by_key/captured_main/value"], "2026-05-31T15:35:00Z")
        self.assertEqual(structured["structured/by_key/captured_secondary/value"], "2026-05-31T15:36:00Z")
        self.assertEqual(structured["structured/by_key/captured_main/name"], "car_captured_time")
        self.assertEqual(
            structured["structured/by_name/car_captured_time/description"],
            "Vehicle-side report capture timestamp; flat payloads do not identify its associated measurement fields.",
        )
        self.assertEqual(structured[f"structured/by_key/{soc_key}/value"], 52)
        self.assertEqual(structured[f"structured/by_key/{soc_key}/unit"], "%")
        self.assertEqual(structured[f"structured/by_key/{soc_key}/car_captured_at"], "")
        self.assertEqual(structured["structured/by_name/soc/value"], 52)
        self.assertEqual(structured["structured/by_name/soc/key"], soc_key)
        self.assertEqual(structured[f"structured/by_key/{soc_key}/name"], "soc")
        self.assertEqual(structured[f"structured/by_key/{soc_key}/json"]["description"], "Battery state of charge.")
        self.assertEqual(structured["structured/by_key/odometer/value"], 63151)
        self.assertEqual(structured["structured/by_key/odometer/unit"], "km")
        self.assertEqual(structured["structured/by_key/odometer/car_captured_at"], "")
        self.assertEqual(values["battery/soc"], 52)
        self.assertEqual(curated_capture_values(dataset), {})
        self.assertEqual(history_batch(dataset, "fixture.zip")["events"], [])
        self.assertFalse(any(topic.startswith("raw/") for topic in structured))
        self.assertFalse(any(value is None for value in structured.values()))

    def test_raw_file_values_publish_zip_members_only(self) -> None:
        download = DatasetDownload(
            name="dataset.zip",
            payload={},
            files=[
                DatasetFile(
                    name="data/vehicle.xml",
                    media_type="application/xml",
                    content="<vehicle><soc>52</soc></vehicle>",
                    sha256="abc123",
                    xml_json={"tag": "vehicle", "children": [{"tag": "soc", "text": "52"}]},
                ),
                DatasetFile(
                    name="data.json",
                    media_type="application/json",
                    content='{"ok":true}',
                    sha256="def456",
                ),
            ],
        )

        values = raw_file_values(download)

        self.assertEqual(values["raw/file/0/filename"], "data/vehicle.xml")
        self.assertEqual(values["raw/file/0/content"], "<vehicle><soc>52</soc></vehicle>")
        self.assertEqual(values["raw/file/1/filename"], "data.json")
        self.assertEqual(values["raw/file/1/content"], '{"ok":true}')
        self.assertNotIn("raw/file/0/xml_json", values)
        self.assertNotIn("raw/file/0/sha256", values)
        self.assertFalse(any(topic.startswith("raw/by_") for topic in values))
        self.assertNotIn("raw/_index", values)


if __name__ == "__main__":
    unittest.main()
