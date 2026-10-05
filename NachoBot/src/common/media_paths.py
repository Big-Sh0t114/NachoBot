"""Shared filesystem paths for media exchanged with platform adapters."""

from pathlib import Path


CORE_ROOT = Path(__file__).resolve().parents[2]


def get_shared_media_temp_dir() -> Path:
    """Return Core's writable temporary-media directory, creating it on demand."""

    media_temp_dir = CORE_ROOT / "data" / "media-tmp"
    media_temp_dir.mkdir(parents=True, exist_ok=True)
    return media_temp_dir
