"""Archive helpers."""

from __future__ import annotations

from pathlib import Path
import logging
from .api import DatasetDownload
from .config import ServiceConfig
LOG=logging.getLogger(__name__)


def _original_data_dir_path(config_path: Path, original_data_dir: str) -> Path:
    path = Path(original_data_dir)
    return path if path.is_absolute() else config_path.parent / path


def _safe_original_data_filename(name: str) -> str:
    basename = Path(str(name).replace("\\", "/")).name
    safe = "".join(char if char.isascii() and (char.isalnum() or char in "._-") else "_" for char in basename)
    safe = safe.strip("._")
    return safe or "dataset.zip"


def save_original_dataset(config: ServiceConfig, config_path: Path, download: DatasetDownload) -> Path | None:
    if not config.save_original_data:
        return None
    if not download.raw:
        LOG.warning("Original dataset ZIP %s was not saved because the raw download bytes are empty", download.name)
        return None

    target_dir = _original_data_dir_path(config_path, config.original_data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _safe_original_data_filename(download.name)
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_bytes(download.raw)
    temp.replace(target)
    LOG.info("Saved original dataset ZIP to %s", target)
    return target
