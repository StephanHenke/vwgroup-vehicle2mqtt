"""Cli helpers."""

from __future__ import annotations

from pathlib import Path
import argparse
import asyncio
import logging
from .config import ServiceConfig
from .state import healthcheck
from .diagnose import run_diagnose
from .runtime import run_service


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VW Group vehicle data to MQTT bridge")
    parser.add_argument("--config", required=True, type=Path, help="Path to config JSON")
    parser.add_argument("--once", action="store_true", help="Run one poll and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print MQTT publishes instead of sending")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--diagnose", action="store_true", help="Check portal, vehicle, dataset and MQTT access")
    parser.add_argument("--healthcheck", action="store_true", help="Check recent successful dataset processing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = ServiceConfig.from_file(args.config)
    if args.healthcheck:
        return healthcheck(config, args.config)
    if args.diagnose:
        return asyncio.run(run_diagnose(config, args.config, dry_run=args.dry_run))
    try:
        asyncio.run(run_service(config, args.config, once=args.once, dry_run=args.dry_run))
    except KeyboardInterrupt:
        return 130
    return 0
