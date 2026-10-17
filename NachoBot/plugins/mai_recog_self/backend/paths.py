"""Stable data locations for the backend library and its CLI."""

from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_ROOT = (CORE_ROOT / "data" / "plugins" / "mai_recog_self").resolve()
DEFAULT_RAW_ROOT = (CORE_ROOT / "data" / "self_recognition_raw").resolve()

