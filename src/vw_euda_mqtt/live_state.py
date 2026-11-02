"""Reconcile partial deliveries without discarding absent vehicle readings."""

from __future__ import annotations

import math
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .catalog import CURATED_TOPIC_FIELDS
from .dataset import DataPoint, Dataset, parse_timestamp
from .dataset_values import data_catalog_entry, structured_values

SOC_FIELDS = ("battery_state_report.soc", "battery_level_HV.value")
HV_VALIDITY_FIELD = "battery_level_HV.state"
CACHE_VERSION = 1


def _newer(incoming: dict, previous: dict) -> bool:
    captured = parse_timestamp(incoming.get("car_captured_at"))
    old_captured = parse_timestamp(previous.get("car_captured_at"))
    if captured is not None and old_captured is not None and captured != old_captured:
        return captured > old_captured
    delivered = parse_timestamp(incoming.get("delivered_at"))
    old_delivered = parse_timestamp(previous.get("delivered_at"))
    if delivered is not None and old_delivered is not None and delivered != old_delivered:
        return delivered > old_delivered
    return incoming.get("sequence", 0) > previous.get("sequence", 0)


def _valid_number(value: Any, *, percentage: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and (not percentage or 0 <= value <= 100)


def _latest_record(records: list[dict], preferred_fields: tuple[str, ...] = ()) -> dict | None:
    if not records:
        return None
    timed = all(parse_timestamp(record.get("car_captured_at")) is not None for record in records)

    def order(record):
        timestamp = parse_timestamp(record.get("car_captured_at") if timed else record.get("delivered_at"))
        priority = -preferred_fields.index(record["field_name"]) if preferred_fields else 0
        return timestamp or datetime.min.replace(tzinfo=timezone.utc), record["sequence"], priority

    return max(sorted(records, key=lambda record: record["key"]), key=order)


class LiveState:
    """A VIN/identifier-scoped cache of original, last-valid datapoint keys."""

    def __init__(self, state: dict, vin: str, identifier: str) -> None:
        saved = state.get("live_state", {})
        if not isinstance(saved, dict) or (
            saved.get("version"), saved.get("vin"), saved.get("identifier")
        ) != (CACHE_VERSION, vin, identifier):
            saved = {}
        self.vin = vin
        self.identifier = identifier
        self.records = deepcopy(saved.get("points", {}))
        self.processed = list(saved.get("processed", []))
        self.sequence = saved.get("sequence", 0)
        self.updated_keys: set[str] = set()

    def merge(
        self, dataset: Dataset, name: str, delivered_at: datetime | None,
        *, delivery_type: str = "partial",
    ) -> None:
        if delivery_type != "partial":
            raise ValueError("Historical deliveries cannot update live state")
        if dataset.vin and dataset.vin != self.vin:
            raise ValueError("Dataset VIN does not match the selected vehicle")
        self.sequence += 1
        validity = next((point for point in dataset.points.values()
                         if self.records.get(point.key, {}).get("field_name", point.field_name) == HV_VALIDITY_FIELD), None)
        for point in dataset.points.values():
            previous = self.records.get(point.key)
            field = previous["field_name"] if previous else point.field_name
            value = point.value
            if value is None or value == "":
                continue
            if field in SOC_FIELDS:
                if not _valid_number(value, percentage=True):
                    continue
                if field == SOC_FIELDS[1] and (validity is None or validity.value != "VALID"):
                    continue
            unit = CURATED_TOPIC_FIELDS.get(field, (None, None))[1]
            if unit in ("%", "km", "kW", "C", "s") and not _valid_number(value, percentage=unit == "%"):
                continue
            if field == "mileage.value" and (
                value <= 0 or (previous is not None and value < DataPoint(point.key, point.field_name, previous["raw_value"]).value)
            ):
                continue
            record = {
                "key": point.key,
                "field_name": field,
                "raw_value": point.raw_value,
                "car_captured_at": point.car_captured_at.isoformat() if point.car_captured_at else None,
                "dataset": name,
                "delivered_at": delivered_at.isoformat() if delivered_at else None,
                "sequence": self.sequence,
            }
            if previous is None or _newer(record, previous):
                self.records[point.key] = record
                self.updated_keys.add(point.key)
        self.processed = [item for item in self.processed if item != name] + [name]
        self.processed = self.processed[-1000:]

    def _latest(self, fields: tuple[str, ...]) -> dict | None:
        return _latest_record([record for record in self.records.values() if record["field_name"] in fields], fields)

    def dataset(self) -> Dataset:
        points = {
            key: DataPoint(
                key=key, field_name=record["field_name"], raw_value=record["raw_value"],
                car_captured_at=parse_timestamp(record.get("car_captured_at")),
            )
            for key, record in self.records.items()
        }
        return Dataset(vin=self.vin, user_id=None, points=points)

    def values(self, *, structured: bool = False, only_updated: bool = False) -> dict[str, Any]:
        snapshot = self.dataset()
        values: dict[str, Any] = {}
        for field, (topic, _unit) in CURATED_TOPIC_FIELDS.items():
            record = self._latest(SOC_FIELDS if topic == "battery/soc" else (field,))
            if record is None or (only_updated and record["key"] not in self.updated_keys):
                continue
            values[topic] = snapshot.points[record["key"]].value
            values[f"{topic}/car_captured_at"] = record.get("car_captured_at") or ""
            values[f"{topic}/source_key"] = record["key"]
            values[f"{topic}/source_dataset"] = record["dataset"]
            values[f"{topic}/source_delivered_at"] = record.get("delivered_at") or ""
        if structured:
            by_name = defaultdict(list)
            for point in snapshot.points.values():
                by_name[data_catalog_entry(point).name].append(self.records[point.key])
            winners = {name: _latest_record(records)["key"] for name, records in by_name.items()}

            def point_order(point):
                return (point.key == winners[data_catalog_entry(point).name],
                        self.records[point.key]["sequence"], point.key)

            tree = structured_values(snapshot, point_order=point_order)
            for topic, key in list(tree.items()):
                if topic.endswith("/key"):
                    prefix = topic.removesuffix("/key")
                    record = self.records[key]
                    tree[f"{prefix}/source_dataset"] = record["dataset"]
                    tree[f"{prefix}/source_delivered_at"] = record.get("delivered_at") or ""
            if only_updated:
                prefixes = {topic.removesuffix("/key") + "/" for topic, key in tree.items()
                            if topic.endswith("/key") and key in self.updated_keys}
                tree = {topic: value for topic, value in tree.items()
                        if any(topic.startswith(prefix) for prefix in prefixes)}
            values.update(tree)
        return values

    def save(self, state: dict) -> None:
        state["live_state"] = {
            "version": CACHE_VERSION, "vin": self.vin, "identifier": self.identifier,
            "points": deepcopy(self.records), "processed": list(self.processed),
            "sequence": self.sequence,
        }
