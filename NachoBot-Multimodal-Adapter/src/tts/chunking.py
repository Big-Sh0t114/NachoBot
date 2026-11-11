"""Shared facade-level TTS sentence splitting and bounded PCM scheduling."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Callable, Sequence
import re
import time
import unicodedata
from typing import Any

from nachobot_multimodal.config import TTSChunkingConfig
from nachobot_multimodal.tts.base import (
    MAX_TTS_AUDIO_BYTES,
    MAX_TTS_AUDIO_SECONDS,
    MAX_TTS_STREAM_SECONDS,
    EmptyPCMStreamError,
    PCMChunk,
    PCMStreamError,
    close_async_iterator,
    iter_validated_pcm,
)
from nachobot_multimodal.utils.text_cleaner import clean_text_for_tts


_SENTENCE_END = frozenset(".!?。！？…")
_SOFT_BREAK = frozenset(",，、;；:：")
_CLOSING_MARKS = frozenset("\"'”’」』）》】〕〉〉]})")
_URL_PATTERN = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"'。！？、，；：]+")
_ABBREVIATION_PATTERN = re.compile(
    r"(?i)\b(?:mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|no|e\.g|i\.e|a\.m|p\.m)\."
)
_INITIALS_PATTERN = re.compile(r"\b(?:[A-Za-z]\.){2,}")
_LATIN_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[._'’+-][A-Za-z0-9]+)*")
_URL_TRAILING_PUNCTUATION = ".,!?;:，。！？；：、)]}"
MAX_TTS_TEXT_CHARS = 10_000


def split_tts_text(text: str, config: TTSChunkingConfig) -> tuple[str, ...]:
    """Split text deterministically while retaining every original character.

    Sentence boundaries are recognized for CJK, Japanese, and English text.
    The configured maximum is soft: only punctuation creates boundaries.
    Whitespace and character counts never split otherwise unpunctuated text,
    so long words, URLs, and complete unpunctuated passages remain whole.
    """

    if not isinstance(text, str):
        raise ValueError("TTS text must be a string")
    if len(text) > MAX_TTS_TEXT_CHARS:
        raise ValueError("TTS text exceeds the 10000-character request limit")
    if not text or not text.strip():
        raise ValueError("TTS text cannot be empty")

    units = _sentence_units(text)
    meaningful_units: list[str] = []
    prefix = ""
    for unit in units:
        if _contains_speakable_content(unit):
            meaningful_units.append(prefix + unit)
            prefix = ""
        elif meaningful_units:
            meaningful_units[-1] += unit
        else:
            prefix += unit
    if prefix:
        if meaningful_units:
            meaningful_units[0] = prefix + meaningful_units[0]
        else:
            raise ValueError("TTS text contains no speakable content")
    if not meaningful_units:
        raise ValueError("TTS text contains no speakable content")

    fragments: list[str] = []
    for unit in meaningful_units:
        fragments.extend(_split_long_unit(unit, config.max_chars))

    result: list[str] = []
    pending = ""
    threshold = config.first_chunk_min_chars
    for fragment in fragments:
        if pending and len(pending) + len(fragment) > config.max_chars:
            if _contains_speakable_content(pending):
                result.append(pending)
                pending = fragment
                threshold = config.min_chars
            elif result:
                # A punctuation-only tail cannot become its own model request.
                # Attach it to the previous text even when that exceeds the
                # soft maximum.
                result[-1] += pending
                pending = fragment
                threshold = config.min_chars
            else:
                # Keep a leading punctuation run with the first speakable text.
                pending += fragment
        else:
            pending += fragment
        if len(pending) >= threshold and _contains_speakable_content(pending):
            result.append(pending)
            pending = ""
            threshold = config.min_chars

    if pending:
        if not _contains_speakable_content(pending) and result:
            # Preserve closing punctuation/quotes on the last audio segment.
            result[-1] += pending
        elif len(result) > 1 and len(result[-1]) + len(pending) <= config.max_chars:
            result[-1] += pending
        else:
            result.append(pending)

    # Punctuation-only input is rejected; punctuation produced by a soft split
    # is folded into adjacent speakable text above, even if that exceeds max.
    if not result or any(not _contains_speakable_content(part) for part in result):
        raise ValueError("TTS text contains no speakable content")
    if "".join(result) != text:
        raise AssertionError("TTS chunking must preserve source text exactly")
    return tuple(result)


def prepare_tts_segments(text: str, config: TTSChunkingConfig) -> tuple[str, ...]:
    """Apply the shared speech cleanup once to the full request, then split it."""

    if not isinstance(text, str):
        raise ValueError("TTS text must be a string")
    if len(text) > MAX_TTS_TEXT_CHARS:
        raise ValueError("TTS text exceeds the 10000-character request limit")
    cleaned = clean_text_for_tts(text)
    if not cleaned:
        raise ValueError("TTS text contains no speakable content")
    return split_tts_text(cleaned, config)


def _sentence_units(text: str) -> list[str]:
    protected = _protected_spans(text)
    units: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char not in _SENTENCE_END or _position_is_protected(index, protected):
            index += 1
            continue
        if char == "." and _is_decimal_period(text, index):
            index += 1
            continue

        end = index + 1
        while end < len(text) and text[end] in _SENTENCE_END:
            if text[end] == "." and _is_decimal_period(text, end):
                break
            if _position_is_protected(end, protected):
                break
            end += 1
        while end < len(text) and text[end] in _CLOSING_MARKS:
            end += 1
        while end < len(text) and text[end].isspace():
            end += 1
        units.append(text[start:end])
        start = end
        index = end

    if start < len(text):
        units.append(text[start:])
    if not units and text:
        units.append(text)
    return units


def _protected_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for match in _URL_PATTERN.finditer(text):
        end = match.end()
        while end > match.start() and text[end - 1] in _URL_TRAILING_PUNCTUATION:
            end -= 1
        if end > match.start():
            spans.append((match.start(), end))
    for pattern in (_ABBREVIATION_PATTERN, _INITIALS_PATTERN, _LATIN_TOKEN_PATTERN):
        spans.extend((match.start(), match.end()) for match in pattern.finditer(text))
    return tuple(spans)


def _position_is_protected(index: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in spans)


def _is_decimal_period(text: str, index: int) -> bool:
    return (
        index > 0
        and index + 1 < len(text)
        and text[index - 1].isdigit()
        and text[index + 1].isdigit()
    )


def _contains_speakable_content(text: str) -> bool:
    return any(
        not char.isspace() and unicodedata.category(char)[0] not in {"P", "Z", "C"}
        for char in text
    )


def _split_long_unit(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    protected = _protected_spans(text)
    chunks: list[str] = []
    start = 0
    while len(text) - start > max_chars:
        limit = start + max_chars
        punctuation_cuts = [
            index + 1
            for index in range(start, limit)
            if text[index] in _SOFT_BREAK
            and not _position_is_protected(index, protected)
        ]
        if punctuation_cuts:
            end = punctuation_cuts[-1]
        else:
            next_punctuation = next(
                (
                    index + 1
                    for index in range(limit, len(text))
                    if text[index] in _SOFT_BREAK
                    and not _position_is_protected(index, protected)
                ),
                None,
            )
            if next_punctuation is None:
                break
            end = next_punctuation
        while end < len(text) and text[end] in _CLOSING_MARKS:
            end += 1
        while end < len(text) and text[end].isspace():
            end += 1
        chunks.append(text[start:end])
        start = end
    if start < len(text):
        chunks.append(text[start:])
    return chunks


async def iter_chunked_tts_pcm(
    text: str,
    stream_factory: Callable[..., AsyncIterable[PCMChunk]],
    config: TTSChunkingConfig,
    *,
    segments: Sequence[str] | None = None,
    max_audio_seconds: float = MAX_TTS_AUDIO_SECONDS,
    max_audio_bytes: int = MAX_TTS_AUDIO_BYTES,
    max_stream_seconds: float = MAX_TTS_STREAM_SECONDS,
    **kwargs: Any,
) -> AsyncIterator[PCMChunk]:
    """Synthesize split segments sequentially under one global PCM budget.

    Each model stream is validated independently for empty or malformed audio,
    then its metadata, byte count, duration, and elapsed time also consume the
    same request-wide budgets. Backpressure is preserved between segments.
    """

    if not isinstance(text, str):
        raise ValueError("TTS text must be a string")
    if len(text) > MAX_TTS_TEXT_CHARS:
        raise ValueError("TTS text exceeds the 10000-character request limit")
    max_audio_seconds = min(max_audio_seconds, MAX_TTS_AUDIO_SECONDS)
    max_audio_bytes = min(max_audio_bytes, MAX_TTS_AUDIO_BYTES)
    max_stream_seconds = min(max_stream_seconds, MAX_TTS_STREAM_SECONDS)
    request_segments = tuple(segments) if segments is not None else prepare_tts_segments(text, config)
    if not request_segments:
        raise ValueError("TTS text contains no speakable content")

    started_at = time.monotonic()
    total_bytes = 0
    total_frames = 0
    metadata: tuple[int, int, int, str] | None = None
    current: AsyncIterator[PCMChunk] | None = None

    try:
        for segment in request_segments:
            remaining_time = max_stream_seconds - (time.monotonic() - started_at)
            if remaining_time <= 0:
                raise TimeoutError("TTS stream exceeded its wall-clock limit")
            remaining_bytes = max_audio_bytes - total_bytes
            if remaining_bytes <= 0:
                raise PCMStreamError("TTS audio exceeded the byte limit")
            if metadata is None:
                remaining_audio = max_audio_seconds
            else:
                remaining_audio = max_audio_seconds - total_frames / metadata[0]
            if remaining_audio <= 0:
                raise PCMStreamError("TTS audio exceeded the duration limit")

            current = iter_validated_pcm(
                stream_factory(text=segment, **kwargs),
                max_audio_seconds=remaining_audio,
                max_audio_bytes=remaining_bytes,
                max_stream_seconds=remaining_time,
            )
            segment_has_audio = False
            try:
                async for chunk in current:
                    current_metadata = (chunk.sample_rate, chunk.channels, chunk.sample_width, chunk.codec)
                    if metadata is None:
                        metadata = current_metadata
                    elif metadata != current_metadata:
                        raise PCMStreamError("PCM metadata changed during a TTS stream")

                    frame_width = chunk.channels * chunk.sample_width
                    total_bytes += len(chunk.data)
                    total_frames += len(chunk.data) // frame_width
                    if total_bytes > max_audio_bytes:
                        raise PCMStreamError("TTS audio exceeded the byte limit")
                    if total_frames / chunk.sample_rate > max_audio_seconds:
                        raise PCMStreamError("TTS audio exceeded the duration limit")
                    segment_has_audio = True
                    yield chunk
                if not segment_has_audio:
                    # iter_validated_pcm normally raises this itself. Keep the
                    # guard here for future validators or direct helper users.
                    raise EmptyPCMStreamError("TTS returned empty audio for a text segment")
            finally:
                await close_async_iterator(current)
                current = None
    finally:
        if current is not None:
            await close_async_iterator(current)


