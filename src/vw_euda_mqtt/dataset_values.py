"""Dataset values helpers."""

from __future__ import annotations

from datetime import datetime,timezone
from typing import Any
from .catalog import CURATED_TOPIC_FIELDS,DATA_CATALOG_BY_FIELD,DataCatalogEntry
from .dataset import Dataset,DataPoint,topic_safe,_unique_topic_segments


def curated_values(dataset: Dataset) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field_name, (topic, _unit) in CURATED_TOPIC_FIELDS.items():
        point = dataset.by_field(field_name)
        if point is not None:
            values[topic] = point.value
    return values


def curated_capture_values(dataset: Dataset) -> dict[str, str]:
    values: dict[str, str] = {}
    for field_name, (topic, _unit) in CURATED_TOPIC_FIELDS.items():
        point = dataset.by_field(field_name)
        if point is not None and point.car_captured_at is not None:
            values[f"{topic}/car_captured_at"] = point.car_captured_at.isoformat()
    return values


def data_catalog_entry(point: DataPoint) -> DataCatalogEntry:
    if point.key in DATA_CATALOG_BY_FIELD:
        return DATA_CATALOG_BY_FIELD[point.key]
    if point.field_name in DATA_CATALOG_BY_FIELD:
        return DATA_CATALOG_BY_FIELD[point.field_name]
    return DataCatalogEntry(name=point.field_name)


def datapoint_json(point: DataPoint) -> dict[str, Any]:
    entry = data_catalog_entry(point)
    car_captured_at = point.car_captured_at.isoformat() if point.car_captured_at else ""
    return {
        "id": topic_safe(point.key),
        "key": point.key,
        "field_name": point.field_name,
        "name": entry.name or point.field_name,
        "value": point.value,
        "raw_value": point.raw_value,
        "unit": entry.unit,
        "description": entry.description,
        "data_type": entry.data_type,
        "cluster": entry.cluster,
        "group_index": point.group_index if point.group_index is not None else None,
        "car_captured_at": car_captured_at,
    }


def structured_values(dataset: Dataset) -> dict[str, Any]:
    values: dict[str, Any] = {}
    points = list(dataset.points.values())
    key_topics = _unique_topic_segments([point.key for point in points])
    point_metadata = [(point, datapoint_json(point)) for point in points]
    name_topics = _unique_topic_segments([metadata["name"] for _point, metadata in point_metadata])
    by_name: dict[str, list[tuple[DataPoint, dict[str, Any], str]]] = {}

    def add_datapoint_tree(prefix: str, metadata: dict[str, Any]) -> None:
        values[f"{prefix}/value"] = metadata["value"]
        values[f"{prefix}/name"] = metadata["name"]
        values[f"{prefix}/key"] = metadata["key"]
        values[f"{prefix}/unit"] = metadata["unit"]
        values[f"{prefix}/description"] = metadata["description"]
        values[f"{prefix}/field_name"] = metadata["field_name"]
        values[f"{prefix}/data_type"] = metadata["data_type"]
        values[f"{prefix}/cluster"] = metadata["cluster"]
        values[f"{prefix}/group_index"] = metadata["group_index"] if metadata["group_index"] is not None else ""
        values[f"{prefix}/car_captured_at"] = metadata["car_captured_at"]
        values[f"{prefix}/json"] = metadata

    for point, metadata in point_metadata:
        key_topic = key_topics[point.key]
        name_topic = name_topics[metadata["name"]]
        by_key_prefix = f"structured/by_key/{key_topic}"
        by_name_prefix = f"structured/by_name/{name_topic}"
        metadata["topics"] = {
            "by_key": by_key_prefix,
            "by_name": by_name_prefix,
        }
        add_datapoint_tree(by_key_prefix, metadata)
        by_name.setdefault(name_topic, []).append((point, metadata, key_topic))

    for name_topic, items in by_name.items():
        sorted_items = sorted(items, key=lambda item: _datapoint_sort_key(item[0]), reverse=True)
        _latest_point, latest_metadata, _latest_key_topic = sorted_items[0]
        add_datapoint_tree(f"structured/by_name/{name_topic}", latest_metadata)
        values[f"structured/by_name/{name_topic}/keys"] = [
            {
                "key": metadata["key"],
                "car_captured_at": metadata["car_captured_at"],
            }
            for _point, metadata, _key_topic in sorted_items
        ]

    return values


def raw_file_values(download: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    files = list(getattr(download, "files", []) or [])
    for index, file in enumerate(files):
        prefix = f"raw/file/{index}"
        values[f"{prefix}/filename"] = file.name
        values[f"{prefix}/content"] = file.content
    return values


def history_batch(dataset: Dataset, dataset_name: str) -> dict[str, Any]:
    eligible_points = [
        point
        for group in dataset.groups
        for point in group.points
        if point.field_name != "car_captured_time" and point.car_captured_at is not None
    ]
    point_metadata = [(point, datapoint_json(point)) for point in eligible_points]
    key_topics = _unique_topic_segments([point.key for point in eligible_points])
    name_topics = _unique_topic_segments([metadata["name"] for _point, metadata in point_metadata])

    events: list[dict[str, Any]] = []
    sorted_items = sorted(point_metadata, key=lambda item: _history_sort_key(item[0]))
    for sequence, (point, metadata) in enumerate(sorted_items):
        key_topic = key_topics[point.key]
        name_topic = name_topics[metadata["name"]]
        event = {
            "event_id": f"{topic_safe(dataset_name)}:{metadata['group_index']}:{sequence}:{key_topic}",
            "dataset": dataset_name,
            "key": metadata["key"],
            "field_name": metadata["field_name"],
            "name": metadata["name"],
            "value": metadata["value"],
            "raw_value": metadata["raw_value"],
            "unit": metadata["unit"],
            "description": metadata["description"],
            "data_type": metadata["data_type"],
            "cluster": metadata["cluster"],
            "group_index": metadata["group_index"],
            "car_captured_at": metadata["car_captured_at"],
            "curated_topic": CURATED_TOPIC_FIELDS.get(point.field_name, ("", None))[0],
            "structured_by_name_topic": f"structured/by_name/{name_topic}",
            "structured_by_key_topic": f"structured/by_key/{key_topic}",
        }
        events.append(event)

    return {
        "vin": dataset.vin,
        "dataset": dataset_name,
        "event_count": len(events),
        "events": events,
    }


def _datapoint_sort_key(point: DataPoint) -> tuple[datetime, str]:
    captured_at = point.car_captured_at or datetime.min.replace(tzinfo=timezone.utc)
    return captured_at, point.key


def _history_sort_key(point: DataPoint) -> tuple[datetime, int]:
    captured_at = point.car_captured_at or datetime.min.replace(tzinfo=timezone.utc)
    group_index = point.group_index if point.group_index is not None else -1
    return captured_at, group_index
