"""Keep Core's failed-ASR display markers out of verified QQ voice turns."""

from collections.abc import Mapping
from typing import Any


_FAILURE_MARKERS = frozenset({
    "[语音识别失败，请稍后重试]",
    "[语音(文本生成失败)]",
    "[语音]",
})


def failed_voice_transcription(message: Any) -> bool:
    """Inspect the processed outcome, never the user's quoted/transcribed words.

    Successful Core transcripts are wrapped in ``[语音：...]``. The bare
    markers are produced by Core after failed/disabled ASR. Missing text can
    occur before preprocessing, so it must not suppress the WAV fallback.
    This helper is used only by the QQCall voice proof providers.
    """
    text = (
        message.get("processed_plain_text")
        if isinstance(message, Mapping)
        else getattr(message, "processed_plain_text", None)
    )
    return isinstance(text, str) and text.strip() in _FAILURE_MARKERS
