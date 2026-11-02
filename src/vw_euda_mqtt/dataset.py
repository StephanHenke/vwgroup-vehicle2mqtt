"""Dataset parsing and curated value extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
DURATION_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*s$", re.I)
TOPIC_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def parse_value(raw: str | None) -> Any:
    if raw is None:
        return None
    value = raw.strip()
    if value == "":
        return None
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    duration = DURATION_RE.match(value)
    if duration:
        return float(duration.group(1))
    if INT_RE.match(value):
        return int(value)
    if FLOAT_RE.match(value):
        return float(value)
    return value


def parse_timestamp(raw: str | int | None) -> datetime | None:
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    value = str(raw).strip()
    if not value:
        return None
    if INT_RE.match(value) and len(value) >= 12:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def topic_safe(value: str) -> str:
    cleaned = TOPIC_SAFE_RE.sub("_", value.strip())
    return cleaned.strip("_") or "unknown"


def _unique_topic_segments(values: list[str]) -> dict[str, str]:
    segments: dict[str, str] = {}
    used: set[str] = set()
    for value in sorted(set(values)):
        base = topic_safe(value)
        candidate = base
        index = 2
        while candidate in used:
            candidate = f"{base}_{index}"
            index += 1
        used.add(candidate)
        segments[value] = candidate
    return segments


@dataclass
class DataPoint:
    key: str
    field_name: str
    raw_value: str
    group_index: int | None = None
    car_captured_at: datetime | None = None

    @property
    def value(self):
        return parse_value(self.raw_value)


@dataclass
class DataGroup:
    """Points sharing a known measurement time, or an untimed bucket."""
    index: int
    captured_at: datetime | None
    points: list[DataPoint] = field(default_factory=list)


@dataclass
class Dataset:
    vin: str
    user_id: str | None
    points: dict[str, DataPoint] = field(default_factory=dict)
    groups: list[DataGroup] = field(default_factory=list)
    captured_at: datetime | None = None

    @classmethod
    def from_json(cls, payload: dict) -> "Dataset":
        by_time: dict[datetime | None, list[DataPoint]] = {}
        captured: list[datetime] = []
        for item in payload.get("Data", []):
            key = item.get("key")
            if not key:
                continue
            field_name = item.get("dataFieldName") or key
            datapoint = DataPoint(
                key=key,
                field_name=field_name,
                raw_value=item.get("value", ""),
                car_captured_at=next(
                    (
                        timestamp
                        for name in ("timestampUtc", "timestampUTC", "timestamp")
                        if (timestamp := parse_timestamp(item.get(name))) is not None
                    ),
                    None,
                ),
            )
            if field_name == "car_captured_time":
                timestamp = parse_timestamp(datapoint.raw_value)
                if timestamp is not None:
                    captured.append(timestamp)
                    if datapoint.car_captured_at is None:
                        datapoint.car_captured_at = timestamp
            if datapoint.car_captured_at is not None:
                captured.append(datapoint.car_captured_at)
            # A flat array contains no report membership. A capture-time row
            # dates itself only; its neighbours must never inherit its value.
            by_time.setdefault(datapoint.car_captured_at, []).append(datapoint)

        groups: list[DataGroup] = []
        points: dict[str, DataPoint] = {}
        ordered_times = sorted(
            by_time,
            key=lambda timestamp: (timestamp is None, timestamp or datetime.max.replace(tzinfo=timezone.utc)),
        )
        for timestamp in ordered_times:
            group_points = sorted(
                by_time[timestamp], key=lambda point: (point.key, point.field_name, point.raw_value or ""),
            )
            index = len(groups)
            for point in group_points:
                point.group_index = index
            groups.append(DataGroup(index=index, captured_at=timestamp, points=group_points))
        # Keep every source record in groups, and expose the freshest known
        # reading per key as live state. Ties do not depend on array order.
        for point in sorted((point for group in groups for point in group.points), key=_point_selection_key):
            points.setdefault(point.key, point)
        return cls(
            vin=payload.get("vin", ""),
            user_id=payload.get("user_id"),
            points=points,
            groups=groups,
            captured_at=max(captured) if captured else None,
        )

    def by_field(self, field_name: str) -> DataPoint | None:
        matches = [point for point in self.points.values() if point.field_name == field_name]
        return min(matches, key=_point_selection_key) if matches else None


def _point_selection_key(point: DataPoint) -> tuple[bool, float, str, str, str]:
    timestamp = point.car_captured_at
    return (
        timestamp is None,
        -timestamp.timestamp() if timestamp is not None else 0,
        point.key,
        point.field_name,
        point.raw_value or "",
    )
