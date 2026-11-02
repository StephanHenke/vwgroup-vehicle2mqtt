"""State-file helpers and value stabilization."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ServiceConfig

LOG = logging.getLogger(__name__)
HEALTHCHECK_MISSED_POLLS = 4


def _state_path(config_path: Path, state_file: str) -> Path:
    path = Path(state_file)
    return path if path.is_absolute() else config_path.parent / path


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _state_car_captured_at(state: dict) -> datetime | None:
    return _parse_datetime(state.get("last_car_captured_at"))


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _stabilize_odometer(values: dict[str, Any], state: dict) -> None:
    if "odometer/km" not in values:
        return

    current = _numeric_value(values["odometer/km"])
    previous = _numeric_value(state.get("last_odometer_km"))
    current_captured_at = values.get("odometer/km/car_captured_at")
    # Older state files may contain timestamps inferred from array position.
    previous_captured_at = (
        state.get("last_odometer_car_captured_at")
        if state.get("last_odometer_capture_source") == "datapoint"
        else None
    )
    if not previous_captured_at:
        state.pop("last_odometer_car_captured_at", None)
        state.pop("last_odometer_capture_source", None)

    if current is None or current <= 0:
        if previous is not None:
            LOG.warning("Ignoring implausible odometer value %r, keeping %s km", values["odometer/km"], previous)
            values["odometer/km"] = int(previous) if previous.is_integer() else previous
            if previous_captured_at:
                values["odometer/km/car_captured_at"] = previous_captured_at
            else:
                values.pop("odometer/km/car_captured_at", None)
        else:
            LOG.warning("Ignoring implausible odometer value %r without previous fallback", values["odometer/km"])
            values.pop("odometer/km", None)
            values.pop("odometer/km/car_captured_at", None)
        return

    if previous is not None and current < previous:
        LOG.warning("Ignoring decreasing odometer value %s km, keeping %s km", current, previous)
        values["odometer/km"] = int(previous) if previous.is_integer() else previous
        if previous_captured_at:
            values["odometer/km/car_captured_at"] = previous_captured_at
        else:
            values.pop("odometer/km/car_captured_at", None)
        return

    state["last_odometer_km"] = int(current) if current.is_integer() else current
    if isinstance(current_captured_at, str) and current_captured_at:
        state["last_odometer_car_captured_at"] = current_captured_at
        state["last_odometer_capture_source"] = "datapoint"
    else:
        state.pop("last_odometer_car_captured_at", None)
        state.pop("last_odometer_capture_source", None)


def healthcheck(config: ServiceConfig, config_path: Path) -> int:
    state = _load_state(_state_path(config_path, config.state_file))
    last_success = _parse_datetime(state.get("last_success_at") or state.get("last_publish"))
    if last_success is None:
        print("unhealthy: no successful dataset publish recorded", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    age = max(0, int((now - last_success).total_seconds()))
    max_age = max(config.poll_interval_seconds * HEALTHCHECK_MISSED_POLLS, 3600)
    if age <= max_age:
        print(f"healthy: last successful dataset publish {age}s ago")
        return 0

    print(f"unhealthy: last successful dataset publish {age}s ago exceeds {max_age}s", file=sys.stderr)
    return 1
