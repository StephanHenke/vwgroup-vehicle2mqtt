"""Diagnose helpers."""

from __future__ import annotations

from pathlib import Path
import sys
import aiohttp
from .api import EudaApiClient,NO_CONTENT_SUFFIX
from .config import ServiceConfig
from .mqtt import check_mqtt_connection
from .polling import _mask_identifier,_select_vehicle_and_identifier,_list_datasets_with_retries
from .state import _load_state,_state_path


def _redact_text(text: str, *values: str | None) -> str:
    redacted = text
    for value in values:
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


async def run_diagnose(config: ServiceConfig, config_path: Path, dry_run: bool = False) -> int:
    assert config.mqtt is not None
    selected_vin: str | None = None
    selected_identifier: str | None = None
    state = _load_state(_state_path(config_path, config.state_file))
    print("Configuration: ok")
    print(f"Brand: {config.brand.upper()} / locale {config.country}-{config.language}")
    print(f"Configured VIN: {_mask_identifier(config.vin)}")
    print(f"State file: {_state_path(config_path, config.state_file)}")

    try:
        check_mqtt_connection(config.mqtt, dry_run=dry_run)
        print("MQTT connection: ok" if not dry_run else "MQTT connection: skipped in dry-run")

        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
            client = EudaApiClient(session, config.portal)
            await client.async_login()
            print("Portal login: ok")
            selected_vin, selected_identifier = await _select_vehicle_and_identifier(client, config, state)
            print(f"Vehicle: {_mask_identifier(selected_vin)}")
            print(f"Continuous-data identifier: {_mask_identifier(selected_identifier)}")
            listing = await _list_datasets_with_retries(client, selected_vin, selected_identifier)
            content = [entry for entry in listing if entry.get("name") and not entry["name"].endswith(NO_CONTENT_SUFFIX)]
            print(f"Dataset listing: ok ({len(listing)} files, {len(content)} content files)")
    except Exception as err:
        message = _redact_text(
            str(err),
            config.email,
            config.password,
            config.vin,
            config.identifier,
            selected_vin,
            selected_identifier,
        )
        print(f"Diagnosis failed: {type(err).__name__}: {message}", file=sys.stderr)
        return 1
    return 0
