from __future__ import annotations

import copy
import json
import random
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vw_euda_mqtt.dataset import Dataset, parse_timestamp  # noqa: E402
from vw_euda_mqtt.dataset_values import (  # noqa: E402
    curated_capture_values,
    curated_values,
    history_batch,
    structured_values,
)


class CaptureTimeTests(unittest.TestCase):
    def test_flat_audi_fixture_is_invariant_under_shuffling(self) -> None:
        path = Path(__file__).parent / "fixtures" / "audi_dataset_redacted.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(any("timestampUtc" in row for row in payload["Data"]))
        expected = Dataset.from_json(payload)
        rng = random.Random(17)
        for _ in range(30):
            shuffled = copy.deepcopy(payload)
            rng.shuffle(shuffled["Data"])
            dataset = Dataset.from_json(shuffled)
            self.assertEqual(dataset, expected)
            self.assertEqual(structured_values(dataset), structured_values(expected))
            self.assertEqual(curated_values(dataset), curated_values(expected))
            self.assertEqual(curated_capture_values(dataset), {})
            self.assertEqual(history_batch(dataset, "sample.zip")["events"], [])
            self.assertNotIn("charging/finish_time", curated_values(dataset))
            for point in dataset.points.values():
                if point.field_name != "car_captured_time":
                    self.assertIsNone(point.car_captured_at)

    def test_explicit_timestamps_win_over_neighbouring_capture_markers(self) -> None:
        own_time = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        for timestamp_field in ("timestampUtc", "timestampUTC", "timestamp"):
            with self.subTest(timestamp_field=timestamp_field):
                payload = {"Data": [
                    {"key": "before", "dataFieldName": "car_captured_time", "value": "2026-01-01T00:00:00Z"},
                    {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "77",
                     timestamp_field: "2026-01-02T04:04:05+01:00"},
                    {"key": "after", "dataFieldName": "car_captured_time", "value": "2026-01-03T00:00:00Z"},
                    {"key": "untimed", "dataFieldName": "range", "value": "250"},
                ]}
                original = copy.deepcopy(payload)
                dataset = Dataset.from_json(payload)
                self.assertEqual(payload, original)
                self.assertEqual(dataset.points["soc"].car_captured_at, own_time)
                self.assertIsNone(dataset.points["untimed"].car_captured_at)
                self.assertEqual(dataset.captured_at, parse_timestamp("2026-01-03T00:00:00Z"))
                self.assertEqual(history_batch(dataset, "sample.zip")["event_count"], 1)
                self.assertEqual(curated_capture_values(dataset), {
                    "battery/soc/car_captured_at": own_time.isoformat(),
                })

    def test_invalid_point_timestamps_do_not_fall_back_to_other_rows(self) -> None:
        for invalid in (None, "", "not-a-date", "2026-01-02T03:04:05", {}, True, "9" * 100):
            with self.subTest(invalid=invalid):
                dataset = Dataset.from_json({"Data": [
                    {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "77",
                     "timestampUtc": invalid},
                    {"key": "capture", "dataFieldName": "car_captured_time", "value": "2026-01-03T00:00:00Z"},
                ]})
                self.assertIsNone(dataset.points["soc"].car_captured_at)
                self.assertEqual(history_batch(dataset, "sample.zip")["events"], [])

    def test_latest_explicit_reading_and_all_history_survive_shuffling(self) -> None:
        rows = [
            {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "70",
             "timestampUtc": "2026-01-02T03:00:00Z"},
            {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "80",
             "timestampUtc": "2026-01-02T04:00:00Z"},
            {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "99"},
            {"key": "range", "dataFieldName": "range", "value": "250",
             "timestampUtc": "2026-01-02T03:00:00Z"},
            {"key": "capture", "dataFieldName": "car_captured_time", "value": "2026-01-03T00:00:00Z"},
        ]
        reference = Dataset.from_json({"Data": rows})
        expected_history = history_batch(reference, "historical.zip")
        rng = random.Random(23)
        for _ in range(30):
            shuffled = rows.copy()
            rng.shuffle(shuffled)
            dataset = Dataset.from_json({"Data": shuffled})
            self.assertEqual(sum(len(group.points) for group in dataset.groups), len(rows))
            self.assertEqual(dataset.by_field("battery_state_report.soc").value, 80)
            self.assertEqual(structured_values(dataset), structured_values(reference))
            self.assertEqual(history_batch(dataset, "historical.zip"), expected_history)
        self.assertEqual(expected_history["event_count"], 3)
        self.assertEqual(
            [(event["value"], event["car_captured_at"]) for event in expected_history["events"] if event["key"] == "soc"],
            [(70, "2026-01-02T03:00:00+00:00"), (80, "2026-01-02T04:00:00+00:00")],
        )

    def test_repeated_identical_historical_records_are_not_dropped(self) -> None:
        row = {"key": "soc", "dataFieldName": "battery_state_report.soc", "value": "70",
               "timestampUtc": "2026-01-02T03:00:00Z"}
        batch = history_batch(Dataset.from_json({"Data": [row, row.copy()]}), "historical.zip")
        self.assertEqual(batch["event_count"], 2)
        self.assertEqual(len({event["event_id"] for event in batch["events"]}), 2)

    def test_timestamp_alias_fallback_stays_within_the_same_point(self) -> None:
        dataset = Dataset.from_json({"Data": [{
            "key": "soc", "dataFieldName": "battery_state_report.soc", "value": "70",
            "timestampUtc": "invalid", "timestampUTC": "2026-01-02T03:00:00Z",
        }]})
        self.assertEqual(dataset.points["soc"].car_captured_at, parse_timestamp("2026-01-02T03:00:00Z"))

    def test_timestamp_parsing_normalizes_offsets_and_accepts_integer_milliseconds(self) -> None:
        self.assertEqual(parse_timestamp("2026-01-02T04:00:00+01:00"), parse_timestamp("2026-01-02T03:00:00Z"))
        self.assertEqual(parse_timestamp(1700000000000), parse_timestamp("1700000000000"))
        for invalid in ("2026-01-02", "2026-01-02T03:00:00", {}, [], True, 1.5):
            with self.subTest(invalid=invalid):
                self.assertIsNone(parse_timestamp(invalid))


if __name__ == "__main__":
    unittest.main()
