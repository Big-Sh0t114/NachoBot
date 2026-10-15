"""Core-owned multimodal perception and TTS facade.

The Core process owns this public boundary and selects from each configured
model group. Local candidates execute through the adapter's versioned API.
"""

from .contracts import (
    AUDIO_TRANSCRIBE_V1,
    IMAGE_DESCRIBE_V1,
    VIDEO_UNDERSTAND_V1,
    TTS_SYNTHESIZE_V1,
    MediaInput,
    PerceptionResult,
    TTSResult,
)
from .profile import RuntimeProfile, get_runtime_profile, normalize_runtime_profile
from .router import CoreMultimodalRouter, get_multimodal_router

__all__ = [
    "AUDIO_TRANSCRIBE_V1",
    "IMAGE_DESCRIBE_V1",
    "VIDEO_UNDERSTAND_V1",
    "TTS_SYNTHESIZE_V1",
    "MediaInput",
    "PerceptionResult",
    "TTSResult",
    "RuntimeProfile",
    "get_runtime_profile",
    "normalize_runtime_profile",
    "CoreMultimodalRouter",
    "get_multimodal_router",
]
