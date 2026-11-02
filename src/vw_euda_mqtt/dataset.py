"""Dataset helpers."""

from __future__ import annotations

from dataclasses import dataclass,field
from datetime import datetime,timezone
from typing import Any
import re


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


def parse_timestamp(raw: str | None) -> datetime | None:
    value = (raw or "").strip()
    if not value:
        return None
    if INT_RE.match(value) and len(value) >= 12:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
        points: dict[str, DataPoint] = {}
        groups: list[DataGroup] = []
        pending_group: list[DataPoint] = []

        def close_group(captured_at: datetime | None) -> None:
            if not pending_group:
                return
            group_index = len(groups)
            group_points = list(pending_group)
            for point in group_points:
                point.group_index = group_index
                point.car_captured_at = captured_at
            groups.append(DataGroup(index=group_index, captured_at=captured_at, points=group_points))
            pending_group.clear()

        for item in payload.get("Data", []):
            key = item.get("key")
            if not key:
                continue
            field_name = item.get("dataFieldName") or key
            datapoint = DataPoint(
                key=key,
                field_name=field_name,
                raw_value=item.get("value", ""),
            )
            points[key] = datapoint
            pending_group.append(datapoint)
            if field_name == "car_captured_time":
                timestamp = parse_timestamp(datapoint.raw_value)
                close_group(timestamp)

        if pending_group and groups:
            last_group = groups[-1]
            for point in list(pending_group):
                point.group_index = last_group.index
                point.car_captured_at = last_group.captured_at
                last_group.points.append(point)
            pending_group.clear()
        else:
            close_group(None)
        captured = [group.captured_at for group in groups if group.captured_at is not None]
        return cls(
            vin=payload.get("vin", ""),
            user_id=payload.get("user_id"),
            points=points,
            groups=groups,
            captured_at=max(captured) if captured else None,
        )

    def by_field(self, field_name: str) -> DataPoint | None:
        matches = [point for point in self.points.values() if point.field_name == field_name]
        if not matches:
            return None
        latest_capture = max((point.car_captured_at for point in matches if point.car_captured_at), default=None)
        if latest_capture is not None:
            latest_matches = [point for point in matches if point.car_captured_at == latest_capture]
            return min(latest_matches, key=lambda point: point.key)
        return min(matches, key=lambda point: point.key)
