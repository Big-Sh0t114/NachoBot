"""Explicit Core runtime profile selection.

The desired profile is read from ``NACHOBOT_RUNTIME_PROFILE`` at service
construction time.  The legacy TTS-only variable describes a model environment
and is deliberately ignored; observed local health is also never used to infer
the desired product profile.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any


class RuntimeProfile(str, Enum):
    FULL = "full"
    LITE = "lite"
    POTATO = "potato"

    @property
    def allows_local_perception(self) -> bool:
        return self is RuntimeProfile.FULL

    @property
    def allows_tts(self) -> bool:
        return self is not RuntimeProfile.POTATO


def normalize_runtime_profile(value: Any, *, default: RuntimeProfile = RuntimeProfile.FULL) -> RuntimeProfile:
    """Normalize one of the three product profiles.

    ``gpu``/``cpu`` (and other launcher-era names) are deliberately not
    accepted here.  They describe the implementation environment of the TTS
    process, not a Core product profile.  Treating them as aliases made a
    legacy TTS variable able to change Core's routing mode.
    """

    if isinstance(value, RuntimeProfile):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {item.value for item in RuntimeProfile}:
        return RuntimeProfile(normalized)
    return default


def get_runtime_profile(value: Any = None) -> RuntimeProfile:
    """Return the explicit desired profile.

    ``full`` is the backwards-compatible default for direct Python launches;
    launchers should set ``NACHOBOT_RUNTIME_PROFILE`` explicitly.  The older
    ``NACHOBOT_TTS_RUNTIME_PROFILE`` describes a model environment (gpu/cpu),
    not the Core product profile, and is intentionally ignored.
    """

    if value is not None:
        return normalize_runtime_profile(value)
    explicit = os.environ.get("NACHOBOT_RUNTIME_PROFILE", "").strip()
    return normalize_runtime_profile(explicit)
