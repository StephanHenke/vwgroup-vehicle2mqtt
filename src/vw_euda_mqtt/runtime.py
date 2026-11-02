"""Runtime helpers."""

from __future__ import annotations

from pathlib import Path
import asyncio
import logging
import signal
from .api import ApiError,AuthError
from .config import ServiceConfig
from .polling import poll_once
from .publishing import publish_error_status
LOG=logging.getLogger(__name__)


async def run_service(config: ServiceConfig, config_path: Path, once: bool, dry_run: bool) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    while not stop.is_set():
        try:
            sleep_seconds = await poll_once(config, config_path, dry_run=dry_run)
            LOG.info("Poll completed, next check in %ss", sleep_seconds)
        except AuthError as err:
            LOG.error("Authentication failed: %s", err)
            publish_error_status(config, err, dry_run=dry_run)
            sleep_seconds = config.retry_interval_seconds
        except ApiError as err:
            LOG.error("Poll failed: %s", err)
            publish_error_status(config, err, dry_run=dry_run)
            sleep_seconds = config.retry_interval_seconds
        except Exception as err:
            LOG.exception("Unexpected poll failure")
            publish_error_status(config, err, dry_run=dry_run)
            sleep_seconds = config.retry_interval_seconds
        if once:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=sleep_seconds)
        except asyncio.TimeoutError:
            pass
