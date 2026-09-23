"""Small, dependency-light contracts for Core multimodal operations."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


AUDIO_TRANSCRIBE_V1 = "audio.transcribe.v1"
IMAGE_DESCRIBE_V1 = "image.describe.v1"
VIDEO_UNDERSTAND_V1 = "video.understand.v1"
TTS_SYNTHESIZE_V1 = "tts.synthesize.v1"

# Bound transport payloads before they reach a provider.  These are deliberately
# conservative; adapters should still capture and send bounded utterances.
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 10_000


def max_base64_chars(max_bytes: int) -> int:
    """Return the largest canonical base64 length for ``max_bytes``."""

    return 4 * ((max_bytes + 2) // 3)


# FastAPI/Pydantic rejects larger JSON strings before the router attempts a
# decode.  Operation-specific limits are still enforced below (audio/image are
# smaller than video), but the public request model itself is never unbounded.
MAX_MEDIA_BASE64_CHARS = max_base64_chars(MAX_VIDEO_BYTES)
MAX_MEDIA_REQUEST_CHARS = MAX_MEDIA_BASE64_CHARS + 256


def _bounded_text(value: Any, *, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "").strip()
    return text[:limit]


def decode_bounded_base64(value: Any, *, max_bytes: int) -> bytes:
    """Decode a base64 payload while bounding decoded bytes and malformed input."""

    if isinstance(value, bytes):
        raw = value
    else:
        raw_value = str(value or "")
        if raw_value.startswith("data:") and "," in raw_value:
            raw_value = raw_value.split(",", 1)[1]
        if len(raw_value) > max_base64_chars(max_bytes):
            raise ValueError(f"media payload exceeds {max_bytes} bytes")
        try:
            raw = base64.b64decode(raw_value, validate=True)
        except Exception as exc:
            raise ValueError("media payload is not valid base64") from exc
    if not raw:
        raise ValueError("media payload is empty")
    if len(raw) > max_bytes:
        raise ValueError(f"media payload exceeds {max_bytes} bytes")
    return raw


def encode_base64(raw: bytes, *, max_bytes: int) -> str:
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("media payload must be bytes")
    if not raw:
        raise ValueError("media payload is empty")
    if len(raw) > max_bytes:
        raise ValueError(f"media payload exceeds {max_bytes} bytes")
    return base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True)
class MediaInput:
    """One bounded media operation input."""

    operation: str
    data: str
    media_format: str = ""
    mime_type: str = ""
    prompt: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.operation not in {
            AUDIO_TRANSCRIBE_V1,
            IMAGE_DESCRIBE_V1,
            VIDEO_UNDERSTAND_V1,
        }:
            raise ValueError(f"unsupported perception operation: {self.operation}")
        if not isinstance(self.data, str) or not self.data.strip():
            raise ValueError("media data is required")
        limits = {
            AUDIO_TRANSCRIBE_V1: MAX_AUDIO_BYTES,
            IMAGE_DESCRIBE_V1: MAX_IMAGE_BYTES,
            VIDEO_UNDERSTAND_V1: MAX_VIDEO_BYTES,
        }
        encoded = self.data.split(",", 1)[1] if self.data.startswith("data:") and "," in self.data else self.data
        if len(encoded) > max_base64_chars(limits[self.operation]):
            raise ValueError(f"media payload exceeds {limits[self.operation]} bytes")


@dataclass(frozen=True)
class PerceptionResult:
    """Result of a Core perception attempt or textual degradation."""

    operation: str
    text: str = ""
    provider: str = "none"
    degraded: bool = False
    attempted: tuple[str, ...] = ()
    error: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.text.strip()) and not self.degraded


@dataclass(frozen=True)
class TTSResult:
    """Audio returned by the local runtime for an explicit tts_text field."""

    text: str
    audio_base64: Optional[str] = None
    audio_format: str = "wav"
    provider: str = "none"
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.audio_base64)


def normalize_operation_payload(operation: str, data: Any) -> tuple[str, int]:
    """Validate a payload and return ``(base64 text, byte limit)``."""

    limits = {
        AUDIO_TRANSCRIBE_V1: MAX_AUDIO_BYTES,
        IMAGE_DESCRIBE_V1: MAX_IMAGE_BYTES,
        VIDEO_UNDERSTAND_V1: MAX_VIDEO_BYTES,
    }
    if operation not in limits:
        raise ValueError(f"unsupported perception operation: {operation}")
    encoded = str(data or "").strip()
    # Decode now to enforce the bound and reject malformed payloads.  Re-encode
    # to a canonical ASCII representation for transport and stable tests.
    canonical = encode_base64(decode_bounded_base64(encoded, max_bytes=limits[operation]), max_bytes=limits[operation])
    return canonical, limits[operation]
