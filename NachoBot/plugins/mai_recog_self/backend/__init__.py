"""Pure backend for importing and serving the MaiSelf image classifier."""

from .paths import DEFAULT_DATA_ROOT, DEFAULT_RAW_ROOT
from .runtime import InferenceService

__all__ = ["DEFAULT_DATA_ROOT", "DEFAULT_RAW_ROOT", "InferenceService"]
