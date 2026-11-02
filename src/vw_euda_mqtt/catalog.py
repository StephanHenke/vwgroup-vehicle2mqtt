"""Catalog helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DataCatalogEntry:
    name: str
    unit: str = ""
    description: str = ""
    data_type: str = ""
    cluster: str = ""


CURATED_TOPIC_FIELDS: dict[str, tuple[str, str | None]] = {
    "battery_state_report.soc": ("battery/soc", "%"),
    "settings.target_soc": ("battery/target_soc", "%"),
    "battery_state_report.charge_bulk_threshold": ("battery/charge_bulk_threshold", "%"),
    "battery_state_report.charge_power": ("battery/charge_power_kw", "kW"),
    "mileage.value": ("odometer/km", "km"),
    "range": ("range/km", "km"),
    "charging_state_report.current_charge_state": ("charging/state", None),
    "charging_state_report.charge_mode": ("charging/mode", None),
    "charging_state_report.charging_scenario": ("charging/scenario", None),
    "charging_state_report.immediate_action_state": ("charging/action_state", None),
    "settings.charge_mode_selection": ("charging/mode_selection", None),
    "settings.max_charge_current_ac": ("charging/max_charge_current_ac", None),
    "locked": ("doors/locked", None),
    "parking_brake": ("parking_brake", None),
    "min_temperature": ("battery/min_temperature_c", "C"),
    "max_temperature": ("battery/max_temperature_c", "C"),
    "remaining_climate_time": ("climate/remaining_time_s", "s"),
}


DATA_CATALOG_BY_FIELD: dict[str, DataCatalogEntry] = {
    "battery_state_report.soc": DataCatalogEntry(
        name="soc",
        unit="%",
        description="Battery state of charge.",
        data_type="int",
        cluster="Charging",
    ),
    "settings.target_soc": DataCatalogEntry(
        name="target_soc",
        unit="%",
        description="Configured charging target state of charge.",
        data_type="int",
        cluster="Charging",
    ),
    "battery_state_report.charge_bulk_threshold": DataCatalogEntry(
        name="charge_bulk_threshold",
        unit="%",
        description="Configured bulk charging threshold.",
        data_type="int",
        cluster="Charging",
    ),
    "battery_state_report.charge_power": DataCatalogEntry(
        name="charge_power",
        unit="kW",
        description="Reported charging power.",
        data_type="number",
        cluster="Charging",
    ),
    "mileage.value": DataCatalogEntry(
        name="odometer",
        unit="km",
        description="Vehicle odometer value.",
        data_type="number",
        cluster="Vehicle Status",
    ),
    "range": DataCatalogEntry(
        name="range",
        unit="km",
        description="Reported electric range.",
        data_type="number",
        cluster="Vehicle Status",
    ),
    "charging_state_report.current_charge_state": DataCatalogEntry(
        name="charging_state",
        description="Current charging state.",
        data_type="string",
        cluster="Charging",
    ),
    "charging_state_report.charge_mode": DataCatalogEntry(
        name="charge_mode",
        description="Current charging mode.",
        data_type="string",
        cluster="Charging",
    ),
    "charging_state_report.charging_scenario": DataCatalogEntry(
        name="charging_scenario",
        description="Current charging scenario.",
        data_type="string",
        cluster="Charging",
    ),
    "charging_state_report.immediate_action_state": DataCatalogEntry(
        name="immediate_action",
        description="Immediate charging action state.",
        data_type="string",
        cluster="Charging",
    ),
    "settings.charge_mode_selection": DataCatalogEntry(
        name="mode_selection",
        description="Selected charging mode setting.",
        data_type="string",
        cluster="Charging",
    ),
    "settings.max_charge_current_ac": DataCatalogEntry(
        name="max_current",
        description="Maximum AC charging current setting.",
        data_type="string",
        cluster="Charging",
    ),
    "locked": DataCatalogEntry(
        name="locked",
        description="Vehicle lock state.",
        data_type="boolean",
        cluster="Vehicle Status",
    ),
    "parking_brake": DataCatalogEntry(
        name="parking_brake",
        description="Parking brake state.",
        data_type="boolean",
        cluster="Parking Data",
    ),
    "min_temperature": DataCatalogEntry(
        name="min_temperature",
        unit="C",
        description="Minimum reported battery temperature.",
        data_type="number",
        cluster="Charging",
    ),
    "max_temperature": DataCatalogEntry(
        name="max_temperature",
        unit="C",
        description="Maximum reported battery temperature.",
        data_type="number",
        cluster="Charging",
    ),
    "remaining_climate_time": DataCatalogEntry(
        name="remaining_climate_time",
        unit="s",
        description="Remaining climate runtime.",
        data_type="number",
        cluster="Climate",
    ),
    "car_captured_time": DataCatalogEntry(
        name="car_captured_time",
        description="Vehicle-side capture timestamp assigned to the surrounding datapoint group.",
        data_type="timestamp",
        cluster="Metadata",
    ),
}
