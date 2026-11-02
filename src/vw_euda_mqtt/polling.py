"""Polling helpers."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime,timedelta,timezone
import asyncio
import logging
import aiohttp
from .api import EudaApiClient,ApiError,NO_CONTENT_SUFFIX
from .config import ServiceConfig
from .data import Dataset
from .archive import save_original_dataset
from .publishing import publish_dataset,publish_status
from .state import _state_path,_load_state,_save_state,_state_car_captured_at
LOG=logging.getLogger(__name__)


DATASET_LIST_RETRY_ATTEMPTS = 3


DATASET_LIST_RETRY_BASE_SECONDS = 5


DATASET_LIST_RETRY_MAX_SECONDS = 30


def _created_on(entry: dict) -> datetime | None:
    raw = entry.get("createdOn")
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    name = entry.get("name", "").rsplit(".", 1)[0]
    for part in reversed(name.split("_")):
        try:
            return datetime.strptime(part, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _next_sleep(listing: list[dict], poll_interval_seconds: int, retry_interval_seconds: int) -> int:
    timestamps = [timestamp for entry in listing if (timestamp := _created_on(entry))]
    if not timestamps:
        return retry_interval_seconds
    newest = max(timestamps)
    target = newest + timedelta(seconds=poll_interval_seconds + 45)
    delta = target - datetime.now(timezone.utc)
    if delta.total_seconds() > 30:
        return max(30, int(delta.total_seconds()))
    return retry_interval_seconds


def _is_dataset_listing_pending(err: ApiError) -> bool:
    text = str(err)
    return (
        "/datadelivery/" in text
        and "/list" in text
        and any(f"HTTP {status}" in text for status in (404, 500, 502, 503, 504))
    )


def _dataset_listing_retry_delay(attempt_index: int) -> int:
    delay = DATASET_LIST_RETRY_BASE_SECONDS * (2 ** max(0, attempt_index - 1))
    return min(delay, DATASET_LIST_RETRY_MAX_SECONDS)


async def _list_datasets_with_retries(
    client: EudaApiClient,
    vin: str,
    identifier: str,
    *,
    sleep=asyncio.sleep,
) -> list[dict]:
    last_err: ApiError | None = None
    for attempt in range(1, DATASET_LIST_RETRY_ATTEMPTS + 1):
        try:
            return await client.async_list_datasets(vin, identifier)
        except ApiError as err:
            if not _is_dataset_listing_pending(err):
                raise
            last_err = err
            if attempt >= DATASET_LIST_RETRY_ATTEMPTS:
                break
            delay = _dataset_listing_retry_delay(attempt)
            LOG.warning(
                "Dataset listing failed transiently for identifier %s (attempt %s/%s), retrying in %ss: %s",
                _mask_identifier(identifier),
                attempt,
                DATASET_LIST_RETRY_ATTEMPTS,
                delay,
                err,
            )
            await sleep(delay)
    assert last_err is not None
    raise last_err


async def _select_vehicle_and_identifier(
    client: EudaApiClient,
    config: ServiceConfig,
    state: dict | None = None,
) -> tuple[str, str]:
    state = state or {}
    state_vin = state.get("vin") if isinstance(state.get("vin"), str) else None
    state_identifier = state.get("identifier") if isinstance(state.get("identifier"), str) else None

    vin = config.vin or state_vin
    if not vin:
        vehicles = await client.async_list_vehicles()
        if not vehicles:
            raise ApiError("No vehicles returned by EU Data Act portal")
        if len(vehicles) > 1:
            names = ", ".join(f"{item.get('vin')} ({item.get('nickname') or 'no name'})" for item in vehicles)
            raise ApiError(f"Multiple vehicles found, set vin in config: {names}")
        vin = vehicles[0]["vin"]

    identifier = config.identifier
    if not identifier and state_vin == vin:
        identifier = state_identifier
    if not identifier:
        metadata = await client.async_get_metadata(vin)
        identifier = metadata.get("Identifier")
        if not identifier:
            raise ApiError(
                "No continuous-data Identifier returned. Enable customised continuous data "
                "for this VIN in the EU Data Act portal first."
            )
    return vin, identifier


async def poll_once(config: ServiceConfig, config_path: Path, dry_run: bool = False) -> int:
    state_path = _state_path(config_path, config.state_file)
    state = _load_state(state_path)
    state_car_captured_at = _state_car_captured_at(state)

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = EudaApiClient(session, config.portal)
        vin, identifier = await _select_vehicle_and_identifier(client, config, state)
        try:
            listing = await _list_datasets_with_retries(client, vin, identifier)
        except ApiError as err:
            if not _is_dataset_listing_pending(err):
                raise
            message = f"Dataset listing not available yet for identifier {_mask_identifier(identifier)}: {err}"
            LOG.warning(message)
            publish_status(
                config,
                vin,
                connected=True,
                error=message,
                error_type="PendingData",
                car_captured_at=state_car_captured_at,
                dry_run=dry_run,
            )
            return config.retry_interval_seconds
        content = sorted(
            (entry for entry in listing if entry.get("name") and not entry["name"].endswith(NO_CONTENT_SUFFIX)),
            key=lambda entry: _created_on(entry) or datetime.min.replace(tzinfo=timezone.utc),
        )
        if not content:
            message = "No content datasets available yet"
            LOG.info(message)
            publish_status(
                config,
                vin,
                connected=True,
                error=message,
                error_type="PendingData",
                car_captured_at=state_car_captured_at,
                dry_run=dry_run,
            )
            return _next_sleep(listing, config.poll_interval_seconds, config.retry_interval_seconds)

        newest = content[-1]
        name = newest["name"]
        if not config.publish_unchanged and state.get("last_dataset") == name:
            LOG.info("No new dataset: %s", name)
            publish_status(config, vin, connected=True, car_captured_at=state_car_captured_at, dry_run=dry_run)
            return _next_sleep(listing, config.poll_interval_seconds, config.retry_interval_seconds)

        download = await client.async_download_dataset(vin, identifier, name)
        if config.save_original_data and not dry_run:
            save_original_dataset(config, config_path, download)
        elif config.save_original_data:
            LOG.info("Dry-run enabled; not saving original dataset ZIP %s", name)
        dataset = Dataset.from_json(download.payload)
        publish_dataset(config, vin, name, dataset, download.payload, download=download, state=state, dry_run=dry_run)

        now = datetime.now(timezone.utc)
        state.update(
            {
                "vin": vin,
                "identifier": identifier,
                "last_dataset": name,
                "last_car_captured_at": dataset.captured_at.isoformat() if dataset.captured_at else "",
                "last_publish": now.isoformat(),
                "last_success_at": now.isoformat(),
            }
        )
        if not dry_run:
            _save_state(state_path, state)
        return _next_sleep(listing, config.poll_interval_seconds, config.retry_interval_seconds)


def _mask_identifier(value: str | None, visible: int = 4) -> str:
    if not value:
        return "not configured"
    if len(value) <= visible * 2:
        return "<redacted>"
    return f"{value[:visible]}...{value[-visible:]}"
