"""Small, per-synthesis environmental ambience mixer for public TTS audio.

The built-in ``room`` and ``desk`` presets are procedural sounds, not field
recordings. Optional local WAV sources can be used when a recorded ambience is
preferred. Streams are mixed incrementally; this module never collects a full
PCM stream before returning its first chunk.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import math
from pathlib import Path
import wave
from typing import AsyncIterable, AsyncIterator

import numpy as np
from scipy.signal import lfilter, resample_poly

try:
    from nachobot_multimodal.config import AmbientConfig
    from nachobot_multimodal.tts.base import PCMChunk, close_async_iterator
except ImportError:  # pragma: no cover - direct src.utils imports in local scripts
    from config import AmbientConfig
    from tts.base import PCMChunk, close_async_iterator


MAX_AMBIENT_SOURCE_BYTES = 8 * 1024 * 1024
MAX_AMBIENT_SOURCE_SECONDS = 30.0
MAX_AMBIENT_SOURCE_RATE = 96_000
MAX_AMBIENT_AUDIO_SECONDS = 120.0
_PCM16_SCALE = 32768.0


def ambient_is_active(config: AmbientConfig) -> bool:
    """Whether this setting produces an audible mix instead of exact passthrough."""

    return bool(config.enabled and config.level_db > -90.0)


def mix_wav(
    wav_bytes: bytes,
    config: AmbientConfig,
    *,
    config_dir: str | Path,
    rng: np.random.Generator | None = None,
) -> bytes:
    """Mix ambience into PCM16 WAV while preserving format and frame count."""

    if not ambient_is_active(config):
        return wav_bytes

    try:
        with wave.open(BytesIO(wav_bytes), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
            pcm_bytes = source.readframes(frame_count)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"TTS ambience requires a readable PCM16 WAV: {exc}") from exc

    if compression != "NONE" or sample_width != 2:
        raise ValueError("TTS ambience requires uncompressed PCM16 WAV audio")
    _validate_audio_format(sample_rate, channels)
    if frame_count > int(sample_rate * MAX_AMBIENT_AUDIO_SECONDS):
        raise ValueError("TTS ambience WAV exceeds the 120-second mixing limit")
    expected_bytes = frame_count * channels * sample_width
    if len(pcm_bytes) != expected_bytes:
        raise ValueError("TTS ambience WAV ended before its declared PCM frame count")

    mixer = AmbientPCMProcessor(
        config,
        sample_rate,
        channels,
        config_dir=config_dir,
        rng=rng,
        max_frames=frame_count,
    )
    mixed_pcm = mixer.process(pcm_bytes)
    output = BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(sample_width)
        target.setframerate(sample_rate)
        target.writeframes(mixed_pcm)
    return output.getvalue()


async def mix_pcm_stream(
    stream: AsyncIterable[PCMChunk],
    config: AmbientConfig,
    *,
    config_dir: str | Path,
    rng: np.random.Generator | None = None,
) -> AsyncIterator[PCMChunk]:
    """Yield same-format PCM chunks with request-scoped ambience state."""

    iterator = stream.__aiter__()
    processor: AmbientPCMProcessor | None = None
    try:
        async for chunk in iterator:
            if not ambient_is_active(config):
                yield chunk
                continue
            if processor is None:
                _validate_audio_format(chunk.sample_rate, chunk.channels)
                processor = await asyncio.to_thread(
                    AmbientPCMProcessor,
                    config,
                    chunk.sample_rate,
                    chunk.channels,
                    config_dir=config_dir,
                    rng=rng,
                )
            yield PCMChunk(
                data=await asyncio.to_thread(processor.process, chunk.data),
                sample_rate=chunk.sample_rate,
                channels=chunk.channels,
                sample_width=chunk.sample_width,
                codec=chunk.codec,
            )
    finally:
        await close_async_iterator(iterator)


class AmbientPCMProcessor:
    """Stateful mono-noise mixer reusable across arbitrary chunk boundaries."""

    def __init__(
        self,
        config: AmbientConfig,
        sample_rate: int,
        channels: int,
        *,
        config_dir: str | Path,
        rng: np.random.Generator | None = None,
        max_frames: int | None = None,
    ) -> None:
        _validate_audio_format(sample_rate, channels)
        if not ambient_is_active(config):
            raise ValueError("AmbientPCMProcessor requires enabled, non-muted ambience")

        self.config = config
        self.sample_rate = sample_rate
        self.channels = channels
        self.rng = rng if rng is not None else np.random.default_rng()
        # Fixed independent streams keep event generation and source offset
        # choices from changing the continuous bed as request duration varies.
        child_seeds = self.rng.integers(0, np.iinfo(np.uint64).max, size=3, dtype=np.uint64)
        self._bed_rng = np.random.default_rng(int(child_seeds[0]))
        self._event_rng = np.random.default_rng(int(child_seeds[1]))
        self._source_rng = np.random.default_rng(int(child_seeds[2]))
        # Draw a fourth seed after the existing three so the original bed,
        # event, and source-offset streams keep their seeded sequences.
        self._variation_rng = np.random.default_rng(
            int(self.rng.integers(0, np.iinfo(np.uint64).max, dtype=np.uint64))
        )
        self._level_periods = (
            float(self._variation_rng.uniform(3.0, 8.0)),
            float(self._variation_rng.uniform(8.0, 16.0)),
        )
        self._level_phases = self._variation_rng.uniform(0.0, 2.0 * math.pi, size=2)
        self._brightness_period = float(self._variation_rng.uniform(5.0, 12.0))
        self._brightness_phase = float(self._variation_rng.uniform(0.0, 2.0 * math.pi))
        self.cursor = 0
        self._maximum_frames = max_frames or int(sample_rate * MAX_AMBIENT_AUDIO_SECONDS)
        self._target_rms = _PCM16_SCALE * 10.0 ** (config.level_db / 20.0)

        # A stateful one-pole filter creates a soft air/fan bed. ``zi`` carries
        # its state across chunks, so changing backend chunk sizes cannot reset
        # the ambience texture.
        cutoff = 300.0 if config.preset == "room" else 520.0
        pole = math.exp(-2.0 * math.pi * cutoff / sample_rate)
        self._filter_pole = pole
        self._filter_gain = math.sqrt(max(1.0 - pole * pole, 1e-9))
        self._filter_state = 0.0
        self._fan_frequency = float(self._bed_rng.uniform(46.0, 96.0))
        self._fan_phase = float(self._bed_rng.uniform(0.0, 2.0 * math.pi))

        self._source = self._load_source(config.source_file, config_dir)
        if self._source is not None:
            self._source_rms = float(np.sqrt(np.mean(np.square(self._source, dtype=np.float64))))
            self._source_offset = int(self._source_rng.integers(0, len(self._source)))
        else:
            self._source_rms = 0.0
            self._source_offset = 0
        self._events = self._make_events()
        self._next_event = 0

    def process(self, pcm_bytes: bytes) -> bytes:
        frame_width = self.channels * 2
        if not isinstance(pcm_bytes, bytes) or len(pcm_bytes) % frame_width:
            raise ValueError("TTS ambience received non-frame-aligned PCM16 audio")
        frame_count = len(pcm_bytes) // frame_width
        if frame_count == 0:
            return pcm_bytes
        if self.cursor + frame_count > self._maximum_frames:
            raise ValueError("TTS ambience audio exceeds the 120-second mixing limit")

        original = np.frombuffer(pcm_bytes, dtype="<i2").reshape(frame_count, self.channels)
        level_gain, brightness = self._modulation(frame_count)
        ambience = self._make_bed(frame_count, brightness=brightness)
        self._add_events(ambience, self.cursor, frame_count)
        # This smooth gain applies to both procedural and recorded ambience,
        # including sparse events, while leaving the TTS speech samples alone.
        ambience *= level_gain

        mixed = original.astype(np.float32) + ambience[:, None]
        # Saturation is deliberate: integer conversion must never wrap a peak.
        output = np.rint(np.clip(mixed, -32768.0, 32767.0)).astype("<i2")
        self.cursor += frame_count
        return output.tobytes()

    def _modulation(self, frame_count: int) -> tuple[np.ndarray, np.ndarray]:
        time_values = (self.cursor + np.arange(frame_count, dtype=np.float64)) / self.sample_rate
        first, second = self._level_periods
        level_db = (
            0.35 * np.sin(2.0 * math.pi * time_values / first + self._level_phases[0])
            + 0.25 * np.sin(2.0 * math.pi * time_values / second + self._level_phases[1])
        )
        level_gain = np.power(10.0, level_db / 20.0).astype(np.float32, copy=False)
        brightness = np.sin(
            2.0 * math.pi * time_values / self._brightness_period + self._brightness_phase
        ).astype(np.float32, copy=False)
        return level_gain, brightness

    def _make_bed(self, frame_count: int, *, brightness: np.ndarray) -> np.ndarray:
        if self._source is not None:
            if self._source_rms <= 1e-8:
                return np.zeros(frame_count, dtype=np.float32)
            positions = (self._source_offset + self.cursor + np.arange(frame_count)) % len(self._source)
            bed = self._source[positions].astype(np.float32, copy=True)
            bed *= self._target_rms / self._source_rms
            return bed

        white = self._bed_rng.standard_normal(frame_count).astype(np.float64, copy=False)
        shaped, final_state = lfilter(
            [self._filter_gain],
            [1.0, -self._filter_pole],
            white,
            zi=[self._filter_state],
        )
        self._filter_state = float(final_state[0])
        cursor = self.cursor + np.arange(frame_count, dtype=np.float64)
        fan = np.sin(
            2.0 * math.pi * self._fan_frequency * cursor / self.sample_rate + self._fan_phase
        )
        if self.config.preset == "room":
            shaped_base, white_base, fan_weight = 0.80, 0.17, 0.035
        else:
            shaped_base, white_base, fan_weight = 0.72, 0.20, 0.08
        # Shift up to six percentage points between low-passed and white noise.
        # Compensate the expected RMS so the timbral movement stays level-neutral.
        shift = 0.06 * brightness
        shaped_weight = shaped_base - shift
        white_weight = white_base + shift
        # ``shaped`` is filtered from ``white``; their same-sample covariance
        # is the one-pole feed-forward coefficient, so include that cross-term
        # in the reference power instead of treating them as independent.
        rms_reference = np.sqrt(
            shaped_weight**2
            + white_weight**2
            + 2.0 * shaped_weight * white_weight * self._filter_gain
            + fan_weight**2 / 2.0
        )
        bed_signal = shaped_weight * shaped + white_weight * white + fan_weight * fan
        bed_signal *= self._target_rms / rms_reference
        return bed_signal.astype(np.float32, copy=False)

    def _make_events(self) -> list[tuple[int, np.ndarray]]:
        rate = self.config.events_per_minute
        if rate <= 0.0 or self._maximum_frames <= 0:
            return []
        mean_gap = self.sample_rate * 60.0 / rate
        rng = self._event_rng
        onset = int(rng.exponential(mean_gap))
        events: list[tuple[int, np.ndarray]] = []
        while onset < self._maximum_frames:
            duration = float(rng.uniform(0.10, 0.42))
            length = max(2, int(duration * self.sample_rate))
            time_values = np.arange(length, dtype=np.float64) / self.sample_rate
            phase = float(rng.uniform(0.0, 2.0 * math.pi))
            strength = self._target_rms * float(rng.uniform(0.22, 0.48))
            if self.config.preset == "desk" and rng.random() < 0.42:
                # A very short, softly attacked tap with a quickly dying tone.
                decay = float(rng.uniform(0.018, 0.050))
                frequency = float(rng.uniform(450.0, 1_300.0))
                envelope = np.exp(-time_values / decay)
                envelope *= np.clip(time_values / 0.002, 0.0, 1.0)
                waveform = np.sin(2.0 * math.pi * frequency * time_values + phase)
                event = strength * 0.55 * envelope * waveform
            else:
                # A quiet, finite rustle with smooth fade-in/out and softened
                # high-frequency noise; its samples are fixed at request start.
                noise = rng.standard_normal(length)
                pole = math.exp(-2.0 * math.pi * 1_800.0 / self.sample_rate)
                filtered = lfilter([1.0 - pole], [1.0, -pole], noise)
                envelope = np.sin(math.pi * np.linspace(0.0, 1.0, length)) ** 0.7
                event = strength * 0.75 * filtered * envelope
            events.append((onset, event.astype(np.float32, copy=False)))
            onset += max(1, int(rng.exponential(mean_gap)))
        return events

    def _add_events(self, target: np.ndarray, start: int, frame_count: int) -> None:
        end = start + frame_count
        first_pending = self._next_event
        scan = first_pending
        while scan < len(self._events):
            onset, event = self._events[scan]
            if onset >= end:
                break
            event_end = onset + len(event)
            overlap_start = max(start, onset)
            overlap_end = min(end, event_end)
            if overlap_start < overlap_end:
                target[overlap_start - start : overlap_end - start] += event[
                    overlap_start - onset : overlap_end - onset
                ]
            scan += 1

        # Keep any event whose tail continues past this chunk for the next
        # call. Later overlapping events were still mixed into this chunk.
        self._next_event = scan
        for index in range(scan - 1, first_pending - 1, -1):
            onset, event = self._events[index]
            if onset + len(event) > end:
                self._next_event = index

    def _load_source(self, source_file: str, config_dir: str | Path) -> np.ndarray | None:
        if not source_file:
            return None
        path = Path(source_file).expanduser()
        if not path.is_absolute():
            path = Path(config_dir) / path
        path = path.resolve()
        try:
            size = path.stat().st_size
            if size <= 0 or size > MAX_AMBIENT_SOURCE_BYTES:
                raise ValueError(f"file size must be between 1 and {MAX_AMBIENT_SOURCE_BYTES} bytes")
            with wave.open(str(path), "rb") as source:
                channels = source.getnchannels()
                sample_width = source.getsampwidth()
                sample_rate = source.getframerate()
                frame_count = source.getnframes()
                compression = source.getcomptype()
                if compression != "NONE" or sample_width != 2:
                    raise ValueError("source must be uncompressed PCM16 WAV")
                if channels not in (1, 2):
                    raise ValueError("source must be mono or stereo")
                if not 8_000 <= sample_rate <= MAX_AMBIENT_SOURCE_RATE:
                    raise ValueError("source sample rate must be between 8000 and 96000 Hz")
                if frame_count <= 0 or frame_count > sample_rate * MAX_AMBIENT_SOURCE_SECONDS:
                    raise ValueError("source duration must be between 0 and 30 seconds")
                raw = source.readframes(frame_count)
            if len(raw) != frame_count * channels * 2:
                raise ValueError("source ended before its declared PCM frame count")
        except (OSError, wave.Error, EOFError, ValueError) as exc:
            raise ValueError(f"Invalid TTS ambient source_file '{path}': {exc}") from exc

        values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / _PCM16_SCALE
        if channels == 2:
            values = values.reshape(-1, 2).mean(axis=1)
        if sample_rate != self.sample_rate:
            common = math.gcd(sample_rate, self.sample_rate)
            values = resample_poly(values, self.sample_rate // common, sample_rate // common)
        loop = _crossfade_loop(values.astype(np.float32, copy=False), self.sample_rate)
        return loop


def _crossfade_loop(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Build a periodic mono source with a short smoothed seam."""

    if len(samples) < 8:
        return samples.copy()
    fade = min(max(1, int(sample_rate * 0.05)), len(samples) // 3)
    if fade < 2 or len(samples) <= 2 * fade:
        return samples.copy()
    ramp = np.linspace(0.0, 1.0, fade, endpoint=True, dtype=np.float32)
    seam = samples[-fade:] * (1.0 - ramp) + samples[:fade] * ramp
    return np.concatenate((samples[fade:-fade], seam)).astype(np.float32, copy=False)


def _validate_audio_format(sample_rate: int, channels: int) -> None:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8_000 <= sample_rate <= 192_000:
        raise ValueError("TTS ambience supports sample rates from 8000 to 192000 Hz")
    if isinstance(channels, bool) or not isinstance(channels, int) or not 1 <= channels <= 8:
        raise ValueError("TTS ambience supports between 1 and 8 PCM channels")
