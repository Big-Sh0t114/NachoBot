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
TTS_STREAM_CODEC = "pcm_s16le"
TTS_STREAM_VERSION = "1"
TTS_STREAM_CHUNK_BYTES = 64 * 1024
TTS_STREAM_MAX_BYTES = MAX_AUDIO_BYTES
TTS_STREAM_MAX_DURATION_SECONDS = 300
ASR_STREAM_SAMPLE_RATE = 16_000
ASR_STREAM_CHANNELS = 1
ASR_STREAM_CHUNK_MAX_BYTES = 64 * 1024
ASR_STREAM_MAX_DURATION_SECONDS = 60
ASR_STREAM_MAX_PCM_BYTES = (
    ASR_STREAM_SAMPLE_RATE * ASR_STREAM_CHANNELS * 2 * ASR_STREAM_MAX_DURATION_SECONDS
)
ASR_STREAM_MAX_CHUNKS = 4096
ASR_STREAM_MAX_CONCURRENCY = 8
ASR_STREAM_IDLE_TIMEOUT_SECONDS = 15
ASR_RESULT_TTL_SECONDS = 30
ASR_STREAM_SCOPE_MAX_CHARS = 256
ASR_STREAM_PLATFORMS = frozenset({"universal_vc", "discord", "discord_vc", "bilibili", "webui", "qq"})


def normalize_asr_stream_platform(platform: Any) -> Optional[str]:
    """Return a canonical allowed voice platform, or ``None`` if untrusted."""

    if not isinstance(platform, str) or len(platform) > 64:
        return None
    candidate = platform.strip().casefold()
    compact = candidate.replace("_", "").replace("-", "")
    for allowed in ASR_STREAM_PLATFORMS:
        if candidate == allowed or compact == allowed.replace("_", "").replace("-", ""):
            return allowed
    return None


def normalize_asr_stream_scope(scope: Any) -> Optional[str]:
    """Validate and normalize an opaque, bounded stream/receipt scope."""

    if not isinstance(scope, str) or len(scope) > ASR_STREAM_SCOPE_MAX_CHARS:
        return None
    return scope.strip()


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
    task: str = ""

    def __post_init__(self) -> None:
        if self.operation not in {
            AUDIO_TRANSCRIBE_V1,
            IMAGE_DESCRIBE_V1,
            VIDEO_UNDERSTAND_V1,
        }:
            raise ValueError(f"unsupported perception operation: {self.operation}")
        allowed_tasks = {
            AUDIO_TRANSCRIBE_V1: {"", "voice"},
            IMAGE_DESCRIBE_V1: {"", "vlm", "vlm_fast"},
            VIDEO_UNDERSTAND_V1: {"", "video"},
        }
        if self.task not in allowed_tasks[self.operation]:
            raise ValueError(f"invalid task {self.task!r} for {self.operation}")
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


@dataclass(frozen=True)
class TTSStreamSpec:
    """Format metadata shared by every chunk in one PCM TTS response."""

    sample_rate: int
    channels: int
    sample_width: int
    codec: str = TTS_STREAM_CODEC

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_width


@dataclass(frozen=True)
class TTSStreamChunk:
    """One frame-aligned PCM chunk and its response format."""

    pcm_s16le: bytes
    spec: TTSStreamSpec

    def __post_init__(self) -> None:
        if not isinstance(self.pcm_s16le, bytes) or not self.pcm_s16le:
            raise ValueError("TTS stream chunks must contain nonempty bytes")
        if self.spec.frame_bytes <= 0 or len(self.pcm_s16le) % self.spec.frame_bytes:
            raise ValueError("TTS stream chunks must end on a complete PCM frame")


class TTSStreamError(RuntimeError):
    """A bounded stream failure annotated with whether audio already began."""

    def __init__(
        self,
        code: str,
        *,
        audio_started: bool = False,
        status_code: Optional[int] = None,
    ):
        self.code = str(code)
        self.audio_started = bool(audio_started)
        self.status_code = status_code
        super().__init__(self.code)

    @property
    def fallback_allowed(self) -> bool:
        """A sender may use its non-stream path only before any audio chunk."""

        return not self.audio_started


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
