"""Opt-in, explicitly routed WASAPI PCM transport owned by QQCall.

This module owns PCM transport only. It does not recognize speech, generate
speech, select an AVSDK device, or claim that local render reached a caller.
UniversalVC owns speech processing through its public API. The PortAudio
dependency is imported lazily, so importing this module never opens a device
stream.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import functools
import importlib
import inspect
import io
import math
import queue
import struct
import threading
import time
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

CORE_SAMPLE_RATE = 16_000
CORE_CHANNELS = 1
SAMPLE_WIDTH = 2
PCM_CODEC = "pcm_s16le"
_VIRTUAL_MARKERS = (
    "virtual audio cable", "vb-audio", "voicemeeter", "steelseries sonar",
    " sonar", "mixline", "steam streaming", "oculus virtual audio",
)
_MAX_DEVICE_NAME_CHARS = 256
_MIN_RATE = 8_000
_MAX_RATE = 96_000
_MAX_NATIVE_RATE = 192_000
_NATIVE_OPERATION_TIMEOUT_S = 5.0


class QQCallAudioError(RuntimeError):
    """A fixed-code audio error; native/device exception strings are discarded."""

    def __init__(self, code: str, *, native_errno: int | None = None):
        self.code = code
        self.native_errno = native_errno if _valid_native_errno(native_errno) else None
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class QQCallAudioDeviceScope:
    """Expected virtual endpoint names isolating inbound and outbound audio."""

    capture_render_name: str
    playback_render_name: str

    def __post_init__(self) -> None:
        _validate_device_name(self.capture_render_name)
        _validate_device_name(self.playback_render_name)
        if self.capture_render_name.casefold() == self.playback_render_name.casefold():
            raise QQCallAudioError("audio_routes_must_be_distinct")


@dataclass(frozen=True, slots=True)
class QQCallAudioConfig:
    """Frozen, fail-closed audio configuration for one adapter lease."""

    enabled: bool = False
    capture_device: str | None = None
    capture_mode: Literal["loopback", "input"] = "loopback"
    playback_device: str | None = None
    device_scope: QQCallAudioDeviceScope | None = None
    frame_ms: int = 20
    queue_ms: int = 240
    capture_queue_ms: int = 240
    max_wav_seconds: int = 60
    max_chunk_bytes: int = 65_536
    drain_timeout_s: float = 5.0
    cancel_timeout_s: float = 0.5

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or self.capture_mode not in ("loopback", "input"):
            raise QQCallAudioError("audio_config_invalid")
        if self.capture_device is not None:
            _validate_device_name(self.capture_device)
        if self.playback_device is not None:
            _validate_device_name(self.playback_device)
        if type(self.frame_ms) is not int or self.frame_ms not in (10, 20, 40, 60):
            raise QQCallAudioError("audio_config_invalid")
        if type(self.queue_ms) is not int or not self.frame_ms <= self.queue_ms <= 2_000:
            raise QQCallAudioError("audio_config_invalid")
        if type(self.capture_queue_ms) is not int or not self.frame_ms <= self.capture_queue_ms <= 2_000:
            raise QQCallAudioError("audio_config_invalid")
        if type(self.max_wav_seconds) is not int or not 1 <= self.max_wav_seconds <= 120:
            raise QQCallAudioError("audio_config_invalid")
        if type(self.max_chunk_bytes) is not int or not 2 <= self.max_chunk_bytes <= 1_048_576:
            raise QQCallAudioError("audio_config_invalid")
        if not _finite_range(self.drain_timeout_s, 0.1, 15.0):
            raise QQCallAudioError("audio_config_invalid")
        if not _finite_range(self.cancel_timeout_s, 0.05, 3.0):
            raise QQCallAudioError("audio_config_invalid")
        if self.device_scope is not None and not isinstance(self.device_scope, QQCallAudioDeviceScope):
            raise QQCallAudioError("audio_config_invalid")
        if self.enabled:
            if self.capture_device is None or self.playback_device is None or self.device_scope is None:
                raise QQCallAudioError("audio_config_incomplete")
            if self.capture_mode == "loopback":
                if _strip_loopback_suffix(self.capture_device).casefold() != self.device_scope.capture_render_name.casefold():
                    raise QQCallAudioError("audio_config_scope_mismatch")
            elif self.capture_device.casefold() != self.device_scope.capture_render_name.casefold():
                raise QQCallAudioError("audio_config_scope_mismatch")
            if self.playback_device.casefold() != self.device_scope.playback_render_name.casefold():
                raise QQCallAudioError("audio_config_scope_mismatch")
            if self.device_scope.capture_render_name.casefold() == self.playback_device.casefold():
                raise QQCallAudioError("audio_routes_must_be_distinct")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> QQCallAudioConfig:
        """Parse the qqcall.audio table without accepting unknown options."""
        if mapping is None:
            return cls()
        if not isinstance(mapping, Mapping):
            raise QQCallAudioError("audio_config_invalid")
        allowed = {
            "enabled", "capture_device", "capture_mode", "playback_device", "device_scope",
            "frame_ms", "queue_ms", "capture_queue_ms", "max_wav_seconds", "max_chunk_bytes",
            "drain_timeout_s", "cancel_timeout_s",
        }
        if set(mapping) - allowed:
            raise QQCallAudioError("audio_config_invalid")
        values = dict(mapping)
        scope = values.get("device_scope")
        if isinstance(scope, Mapping):
            if set(scope) != {"capture_render_name", "playback_render_name"}:
                raise QQCallAudioError("audio_config_invalid")
            values["device_scope"] = QQCallAudioDeviceScope(
                capture_render_name=scope["capture_render_name"],
                playback_render_name=scope["playback_render_name"],
            )
        elif scope is not None and not isinstance(scope, QQCallAudioDeviceScope):
            raise QQCallAudioError("audio_config_invalid")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class AudioDeviceInfo:
    """Safe device metadata. PortAudio indexes are intentionally omitted."""

    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    is_loopback_device: bool
    default_sample_rate: int
    is_virtual_named: bool


@dataclass(slots=True)
class _ResolvedDevice:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    is_loopback_device: bool
    sample_rate: int


@dataclass(slots=True)
class _CaptureItem:
    pcm: bytes
    metadata: dict[str, Any]


@dataclass(slots=True)
class _PlaybackSession:
    stream_id: str
    run_generation: int
    playback_generation: int
    fence: Callable[[], bool]
    source_rate: int
    source_channels: int
    resampler: _Pcm16Resampler
    output_stream: Any
    output_queue: queue.Queue[bytes]
    block_bytes: int
    output_channels: int
    output_rate: int
    stage: bytearray = field(default_factory=bytearray)
    callback_pending: bytearray = field(default_factory=bytearray)
    expected_seq: int = 0
    producer_eof: bool = False
    cancelled: bool = False
    abort_started: bool = False
    completed: threading.Event = field(default_factory=threading.Event)
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    output_frames: int = 0
    underrun_frames: int = 0


class _BandlimitedPcm16Downsampler:
    """Bounded windowed-sinc FIR with a short lookahead and zero-padded EOF.

    Filter before discarding native-rate samples. A 16 kHz source roundtrip
    cannot detect aliases introduced by wideband 44.1/48/96 kHz device audio.
    The lookahead is about 1.3 ms at 16 kHz output, independent of chunk size.
    """

    __slots__ = ("input_rate", "output_rate", "_radius", "_history", "_count", "_next", "_kernels", "_flushed")

    def __init__(self, input_rate: int, output_rate: int):
        self.input_rate, self.output_rate = input_rate, output_rate
        self._radius = math.ceil(21 * input_rate / output_rate)
        self._history: list[int] = []
        self._count = self._next = 0
        self._kernels: dict[int, tuple[float, ...]] = {}
        self._flushed = False

    def _kernel(self, remainder: int) -> tuple[float, ...]:
        cached = self._kernels.get(remainder)
        if cached is not None:
            return cached
        fraction = remainder / self.output_rate
        cutoff = 0.45 * self.output_rate / self.input_rate
        radius = self._radius
        coefficients = []
        for offset in range(-radius, radius + 1):
            distance = offset - fraction
            sinc = 2 * cutoff if abs(distance) < 1e-12 else math.sin(2 * math.pi * cutoff * distance) / (math.pi * distance)
            window = 0.54 + 0.46 * math.cos(math.pi * offset / radius)
            coefficients.append(sinc * window)
        gain = sum(coefficients)
        kernel = tuple(value / gain for value in coefficients)
        # Standard device rates use <=160 phases; cap arbitrary-rate requests.
        if len(self._kernels) < 512:
            self._kernels[remainder] = kernel
        return kernel

    def _render(self, samples: list[int], *, final: bool) -> bytes:
        base = self._count - len(self._history)
        joined = self._history + samples
        self._count += len(samples)
        output = []
        radius = self._radius
        while self._next < self._count * self.output_rate:
            position, remainder = divmod(self._next, self.output_rate)
            if not final and position + radius >= self._count:
                break
            left = position - radius
            relative = left - base
            if left >= 0 and relative < 0:
                raise QQCallAudioError("audio_resampler_state_invalid")
            kernel = self._kernel(remainder)
            start = max(0, relative)
            end = min(len(joined), relative + len(kernel))
            value = sum(sample * coefficient for sample, coefficient in
                        zip(joined[start:end], kernel[start-relative:end-relative]))
            output.append(_clamp16(round(value)))
            self._next += self.input_rate
        self._history = joined[-(2 * radius + 2):]
        return _pack_mono(output)

    def process(self, pcm: bytes) -> bytes:
        if self._flushed:
            raise QQCallAudioError("audio_resampler_closed")
        return self._render(_unpack_mono(pcm), final=False)

    def flush(self) -> bytes:
        if self._flushed:
            return b""
        self._flushed = True
        return self._render([], final=True)


class _Pcm16Resampler:
    """Band-limited downsampling and linear upsampling, independent of chunks."""

    __slots__ = ("input_rate", "output_rate", "_sample_count", "_last", "_next_num", "_flushed", "_downsampler")

    def __init__(self, input_rate: int, output_rate: int):
        if not _valid_rate(input_rate) or not _valid_rate(output_rate):
            raise QQCallAudioError("audio_format_invalid")
        self.input_rate = input_rate
        self.output_rate = output_rate
        self._sample_count = 0
        self._last: int | None = None
        self._next_num = 0
        self._flushed = False
        self._downsampler = _BandlimitedPcm16Downsampler(input_rate, output_rate) if input_rate > output_rate else None

    def process(self, pcm: bytes) -> bytes:
        if self._flushed:
            raise QQCallAudioError("audio_resampler_closed")
        if self._downsampler is not None:
            return self._downsampler.process(pcm)
        samples = _unpack_mono(pcm)
        if not samples:
            return b""
        base = self._sample_count - 1 if self._last is not None else self._sample_count
        joined = ([self._last] if self._last is not None else []) + samples
        new_count = self._sample_count + len(samples)
        output: list[int] = []
        while self._next_num < new_count * self.output_rate:
            position, remainder = divmod(self._next_num, self.output_rate)
            if position + 1 >= new_count:
                break
            relative = position - base
            if relative < 0 or relative + 1 >= len(joined):
                raise QQCallAudioError("audio_resampler_state_invalid")
            first, second = joined[relative], joined[relative + 1]
            output.append(_clamp16(round(first + (second - first) * remainder / self.output_rate)))
            self._next_num += self.input_rate
        self._sample_count = new_count
        self._last = samples[-1]
        return _pack_mono(output)

    def flush(self) -> bytes:
        if self._flushed:
            return b""
        self._flushed = True
        if self._downsampler is not None:
            return self._downsampler.flush()
        if self._last is None:
            return b""
        output: list[int] = []
        end = self._sample_count * self.output_rate
        while self._next_num < end:
            position, _ = divmod(self._next_num, self.output_rate)
            if position != self._sample_count - 1:
                raise QQCallAudioError("audio_resampler_state_invalid")
            output.append(self._last)
            self._next_num += self.input_rate
        return _pack_mono(output)


class QQCallAudioBackend:
    """Bounded full-duplex PCM transport on explicitly selected WASAPI devices."""

    def __init__(self, config: QQCallAudioConfig, *, audio_module: Any | None = None):
        if not isinstance(config, QQCallAudioConfig):
            raise QQCallAudioError("audio_config_invalid")
        self.config = config
        self._audio_module = audio_module
        self._audio_api: Any | None = audio_module
        self._native_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._audio: Any | None = None
        self._capture_stream: Any | None = None
        self._capture_device: _ResolvedDevice | None = None
        self._playback_device: _ResolvedDevice | None = None
        self._capture_channels = 0
        self._capture_resampler: _Pcm16Resampler | None = None
        self._capture_queue: queue.Queue[_CaptureItem] | None = None
        self._capture_available: asyncio.Event | None = None
        self._capture_dispatch_task: asyncio.Task[None] | None = None
        self._capture_stop = threading.Event()
        self._run_generation = 0
        self._playback_generation = 0
        self._capture_sample_counter = 0
        self._capture_overruns = 0
        self._failure_signaled = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._capture_callback: Callable[[bytes, dict[str, Any]], Any] | None = None
        from .diagnostic_capture import BoundedPcmSnapshot
        self._diagnostic_snapshot = BoundedPcmSnapshot()
        self._failure_callback: Callable[[str], Any] | None = None
        self._uplink_send_pcm: Callable[..., Any] | None = None
        self._uplink_fence: Callable[[], bool] | None = None
        self._uplink_expected_sample = 0
        self._uplink_frames_sent = 0
        self._scope: Any = None
        self._session: _PlaybackSession | None = None
        self._core_stream_lock = asyncio.Lock()
        self._core_stream_id: str | None = None
        self._core_stream_spec: dict[str, Any] | None = None
        self._core_stream_next_seq = 0
        self._lifecycle_lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._state = "disabled" if not config.enabled else "stopped"
        self._last_error: str | None = None
        self._native_errno: int | None = None
        self._cleanup_task: asyncio.Task[Any] | None = None

    @property
    def status(self) -> dict[str, Any]:
        """A redacted snapshot containing endpoint metadata and counters only."""
        with self._state_lock:
            session = self._session
            return {
                "enabled": self.config.enabled,
                "state": self._state,
                "ready": self._state == "ready",
                "capture_device": self._capture_device.name if self._capture_device else None,
                "capture_mode": self.config.capture_mode if self._capture_device else None,
                "playback_device": self._playback_device.name if self._playback_device else None,
                "capture_sample_counter": self._capture_sample_counter,
                "capture_overruns": self._capture_overruns,
                "uplink_frames_sent": self._uplink_frames_sent,
                "playback_active": session is not None and not session.cancelled,
                "playback_underrun_frames": session.underrun_frames if session else 0,
                "last_error": self._last_error,
                "native_errno": self._native_errno,
                "generation": self._run_generation,
                "cleanup_unconfirmed": self._state in ("cleanup_pending", "cleanup_failed", "failed"),
            }

    async def enumerate_devices(self) -> list[AudioDeviceInfo]:
        """Enumerate endpoint metadata only; no stream is opened and indexes omitted."""
        async with self._lifecycle_lock:
            if self._cleanup_task is not None or self._state in ("starting", "stopping", "cleanup_pending"):
                raise QQCallAudioError("audio_backend_busy")
            if self._audio is not None:
                devices = await self._run_native(
                    self._enumerate_raw,
                    _NATIVE_OPERATION_TIMEOUT_S,
                    "audio_device_enumeration_timeout",
                    self._audio,
                    late_cleanup=lambda _value: self._cleanup_quarantined(),
                )
            else:
                module = self._load_audio_module()
                devices = await self._run_native(
                    self._enumerate_with_module,
                    _NATIVE_OPERATION_TIMEOUT_S,
                    "audio_device_enumeration_timeout",
                    module,
                )
                if self._state == "stopped":
                    self._shutdown_native_executor()
        return [
            AudioDeviceInfo(
                device.name, device.host_api, device.max_input_channels, device.max_output_channels,
                device.is_loopback_device, device.sample_rate, _has_virtual_marker(device.name),
            )
            for device in devices
        ]

    async def start(
        self,
        capture_callback: Callable[[bytes, dict[str, Any]], Any] | None,
        on_failure: Callable[[str], Any],
        scope: Any,
        *,
        send_pcm: Callable[..., Any] | None = None,
        capture_fence: Callable[[], bool] | None = None,
    ) -> None:
        """Start continuous capture after resolving exact, distinct WASAPI endpoints."""
        if not self.config.enabled:
            raise QQCallAudioError("audio_disabled")
        if (not callable(capture_callback) and send_pcm is None) or not callable(on_failure) or scope is None:
            raise QQCallAudioError("audio_start_invalid")
        if send_pcm is not None:
            if capture_callback is not None or not callable(capture_fence):
                raise QQCallAudioError("audio_start_invalid")
            self._uplink_send_pcm = send_pcm
            self._uplink_fence = capture_fence
            self._uplink_expected_sample = 0
            self._uplink_frames_sent = 0
            capture_callback = self._forward_capture_to_universal
        async with self._lifecycle_lock:
            if self._state == "ready":
                raise QQCallAudioError("audio_already_started")
            if self._state != "stopped" or self._cleanup_task is not None:
                raise QQCallAudioError("audio_backend_busy")
            self._state = "starting"
            self._last_error = None
            self._native_errno = None
            self._failure_signaled.clear()
            self._capture_stop.clear()
            self._capture_sample_counter = 0
            self._capture_overruns = 0
            self._run_generation += 1
            generation = self._run_generation
            self._loop = asyncio.get_running_loop()
            self._capture_callback = capture_callback
            self._failure_callback = on_failure
            self._scope = scope
            try:
                self._audio_api = self._load_audio_module()
                capacity = max(2, self.config.capture_queue_ms // self.config.frame_ms)
                self._capture_queue = queue.Queue(maxsize=capacity)
                self._capture_available = asyncio.Event()
                self._capture_dispatch_task = asyncio.create_task(self._capture_dispatcher(generation))
                setup = await self._run_native(
                    self._start_capture_native,
                    _NATIVE_OPERATION_TIMEOUT_S,
                    "audio_start_timeout",
                    generation,
                    late_cleanup=self._dispose_capture_setup,
                )
                audio, capture_device, playback_device, channels, resampler, stream = setup
                self._audio = audio
                self._capture_device = capture_device
                self._playback_device = playback_device
                self._capture_channels = channels
                self._capture_resampler = resampler
                self._capture_stream = stream
                if generation != self._run_generation:
                    raise QQCallAudioError("audio_generation_stale")
                if self._failure_signaled.is_set():
                    raise QQCallAudioError("audio_start_failed")
                self._state = "ready"
            except QQCallAudioError:
                if self._cleanup_task is None:
                    await self._cleanup_failed_start()
                raise
            except Exception as exc:
                del exc
                self._last_error = "audio_start_failed"
                if self._cleanup_task is None:
                    await self._cleanup_failed_start()
                raise QQCallAudioError("audio_start_failed") from None

    async def stop(self) -> None:
        """Invalidate callbacks first, then stop and release streams idempotently."""
        snapshot = getattr(self, "_diagnostic_snapshot", None)
        if snapshot is not None:
            try:
                snapshot.close()
            except Exception:
                pass
        async with self._lifecycle_lock:
            if self._state in ("disabled", "stopped") and self._audio is None and self._cleanup_task is None:
                return
            self._run_generation += 1
            self._capture_stop.set()
            self._wake_capture_dispatcher()
            self._state = "stopping"
            pending = self._cleanup_task
            if pending is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=self.config.cancel_timeout_s)
                except asyncio.TimeoutError:
                    self._state = "cleanup_pending"
                    return
                if self._cleanup_task is not None and not self._cleanup_task.done():
                    self._state = "cleanup_pending"
                    return
                if self._state == "stopped":
                    return
            try:
                await self.cancel_playback()
            except QQCallAudioError:
                if self._cleanup_task is not None:
                    self._state = "cleanup_pending"
                    return
                self._last_error = "audio_cancel_failed"
            task = self._capture_dispatch_task
            self._capture_dispatch_task = None
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=self.config.cancel_timeout_s + 0.25)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task.cancel()
            try:
                await self._run_native(
                    self._cleanup_native_resources,
                    self.config.cancel_timeout_s,
                    "audio_stop_timeout",
                )
            except QQCallAudioError:
                if self._cleanup_task is not None:
                    self._state = "cleanup_pending"
                    return
                self._state = "cleanup_failed"
                self._last_error = "audio_stop_failed"
                return
            self._clear_released_resources()
            self._shutdown_native_executor()
            self._state = "stopped"

    async def play_wav(self, audio_base64: str, fence: Callable[[], bool]) -> dict[str, Any]:
        """Play bounded PCM16 WAV data; receipt means local render drain only."""
        self._check_fence(fence)
        pcm, rate, channels = self._decode_wav(audio_base64)
        stream_id = f"wav-{time.monotonic_ns()}"
        await self.stream_start(
            stream_id,
            {"codec": PCM_CODEC, "sample_rate": rate, "channels": channels, "sample_width": SAMPLE_WIDTH},
            fence,
        )
        frame_bytes = channels * SAMPLE_WIDTH
        chunk_limit = self.config.max_chunk_bytes - self.config.max_chunk_bytes % frame_bytes
        try:
            for sequence, offset in enumerate(range(0, len(pcm), chunk_limit)):
                await self.stream_chunk(stream_id, sequence, pcm[offset : offset + chunk_limit])
            return await self.stream_end(stream_id)
        except Exception:
            await self.stream_abort(stream_id)
            raise

    async def play_core_segment(
        self,
        kind: str,
        data: Any,
        fence: Callable[[], bool],
    ) -> dict[str, Any]:
        """Decode and drain one Core-generated voice segment on this QQ route.

        Audio payload parsing stays inside the optional QQCall package. The
        shared runtime passes the segment through opaquely and owns only the
        call-scoped fence.
        """
        self._check_fence(fence)
        async with self._core_stream_lock:
            if kind == "voice":
                if not isinstance(data, str) or not data:
                    raise QQCallAudioError("voice_payload_invalid")
                if self._core_stream_id is not None:
                    await self.stream_abort(self._core_stream_id)
                    self._reset_core_stream()
                return await self.play_wav(data, fence)
            if kind != "voice_stream" or not isinstance(data, Mapping):
                raise QQCallAudioError("voice_segment_unsupported")

            event = data.get("event")
            stream_id = data.get("stream_id")
            parent_id = data.get("parent_message_id")
            rate = data.get("sample_rate")
            channels = data.get("channels")
            width = data.get("sample_width")
            codec = data.get("codec")
            if (
                event not in {"start", "chunk", "end", "abort"}
                or not _valid_core_id(stream_id)
                or not _valid_core_id(parent_id)
                or type(rate) is not int or not _valid_rate(rate)
                or type(channels) is not int or channels not in (1, 2)
                or type(width) is not int or width != SAMPLE_WIDTH
                or codec not in (PCM_CODEC, "s16le")
            ):
                raise QQCallAudioError("voice_stream_event_invalid")
            spec = {"sample_rate": rate, "channels": channels, "sample_width": width, "codec": codec}

            if event == "start":
                if set(data) != {"event", "stream_id", "parent_message_id", "sample_rate", "channels", "sample_width", "codec"}:
                    raise QQCallAudioError("voice_stream_event_invalid")
                if self._core_stream_id is not None:
                    await self.stream_abort(self._core_stream_id)
                    self._reset_core_stream()
                await self.stream_start(stream_id, spec, fence)
                if not fence():
                    await self.stream_abort(stream_id)
                    raise QQCallAudioError("audio_fence_stale")
                self._core_stream_id = stream_id
                self._core_stream_spec = spec
                self._core_stream_next_seq = 0
                return {"render_drained": False, "drain_confirmed": False, "event": "start"}

            if self._core_stream_id != stream_id or self._core_stream_spec != spec:
                raise QQCallAudioError("voice_stream_not_current")
            if event == "chunk":
                if set(data) != {"event", "stream_id", "parent_message_id", "sample_rate", "channels", "sample_width", "codec", "seq", "audio_base64"}:
                    raise QQCallAudioError("voice_stream_event_invalid")
                seq = data.get("seq")
                encoded = data.get("audio_base64")
                if type(seq) is not int or seq != self._core_stream_next_seq or not isinstance(encoded, str):
                    raise QQCallAudioError("voice_stream_sequence_invalid")
                if len(encoded) > 4 * ((self.config.max_chunk_bytes + 2) // 3):
                    raise QQCallAudioError("voice_stream_payload_invalid")
                try:
                    pcm = base64.b64decode(encoded.encode("ascii"), validate=True)
                except (UnicodeEncodeError, binascii.Error, ValueError):
                    raise QQCallAudioError("voice_stream_payload_invalid") from None
                if not pcm or len(pcm) > self.config.max_chunk_bytes or len(pcm) % (channels * width):
                    raise QQCallAudioError("voice_stream_payload_invalid")
                await self.stream_chunk(stream_id, seq, pcm)
                if not fence():
                    await self.stream_abort(stream_id)
                    self._reset_core_stream()
                    raise QQCallAudioError("audio_fence_stale")
                self._core_stream_next_seq += 1
                return {"render_drained": False, "drain_confirmed": False, "event": "chunk"}

            if set(data) != {"event", "stream_id", "parent_message_id", "sample_rate", "channels", "sample_width", "codec"}:
                raise QQCallAudioError("voice_stream_event_invalid")
            if event == "abort":
                await self.stream_abort(stream_id)
                self._reset_core_stream()
                return {"render_drained": False, "drain_confirmed": False, "event": "abort"}
            if self._core_stream_next_seq == 0:
                await self.stream_abort(stream_id)
                self._reset_core_stream()
                raise QQCallAudioError("playback_drain_unconfirmed")
            try:
                receipt = await self.stream_end(stream_id)
                self._check_fence(fence)
                return receipt
            finally:
                self._reset_core_stream()

    def _reset_core_stream(self) -> None:
        self._core_stream_id = None
        self._core_stream_spec = None
        self._core_stream_next_seq = 0

    async def stream_start(self, stream_id: str, spec: Mapping[str, Any], fence: Callable[[], bool]) -> None:
        """Begin one bounded producer stream; accept only PCM16LE mono/stereo."""
        self._check_fence(fence)
        if not isinstance(stream_id, str) or not 1 <= len(stream_id) <= 128:
            raise QQCallAudioError("audio_stream_invalid")
        if not isinstance(spec, Mapping):
            raise QQCallAudioError("audio_format_invalid")
        codec = spec.get("codec", spec.get("format", spec.get("sample_format")))
        rate, channels = spec.get("sample_rate"), spec.get("channels")
        width = spec.get("sample_width", SAMPLE_WIDTH)
        if codec not in (PCM_CODEC, "s16le") or type(rate) is not int or not _valid_rate(rate):
            raise QQCallAudioError("audio_format_unsupported")
        if type(channels) is not int or channels not in (1, 2) or type(width) is not int or width != SAMPLE_WIDTH:
            raise QQCallAudioError("audio_format_unsupported")
        async with self._lifecycle_lock:
            if self._state != "ready" or self._audio is None or self._playback_device is None:
                raise QQCallAudioError("audio_runtime_not_ready")
            if self._session is not None or self._cleanup_task is not None:
                raise QQCallAudioError("audio_playback_busy")
            device = self._playback_device
            output_channels = min(2, device.max_output_channels)
            if output_channels < 1:
                raise QQCallAudioError("playback_device_unavailable")
            block_frames = _frames_for_ms(device.sample_rate, self.config.frame_ms)
            capacity = max(2, self.config.queue_ms // self.config.frame_ms)
            self._playback_generation += 1
            session = _PlaybackSession(
                stream_id, self._run_generation, self._playback_generation, fence, rate, channels,
                _Pcm16Resampler(rate, device.sample_rate), None,
                queue.Queue(maxsize=capacity), block_frames * output_channels * SAMPLE_WIDTH,
                output_channels, device.sample_rate,
            )
            self._session = session
            try:
                stream = await self._run_native(
                    self._open_and_start_playback,
                    _NATIVE_OPERATION_TIMEOUT_S,
                    "playback_start_timeout",
                    self._audio,
                    device,
                    output_channels,
                    session,
                    late_cleanup=lambda value: self._cleanup_quarantined(extra_stream=value),
                )
                session.output_stream = stream
                if not self._session_is_current(session):
                    raise QQCallAudioError("audio_generation_stale")
            except QQCallAudioError:
                if self._cleanup_task is None:
                    self._session = None
                else:
                    session.cancelled = True
                raise
            except Exception as exc:
                del exc
                self._session = None
                self._last_error = "playback_start_failed"
                raise QQCallAudioError("playback_start_failed") from None

    async def stream_chunk(self, stream_id: str, seq: int, pcm_bytes: bytes) -> None:
        """Accept a strictly sequenced PCM chunk with bounded queue backpressure."""
        session = self._get_session(stream_id)
        try:
            self._check_session_fence(session)
        except QQCallAudioError:
            await self._abort_session(session)
            raise
        if type(seq) is not int or seq != session.expected_seq:
            await self._abort_session(session)
            raise QQCallAudioError("audio_sequence_invalid")
        if not isinstance(pcm_bytes, bytes) or not pcm_bytes or len(pcm_bytes) > self.config.max_chunk_bytes:
            await self._abort_session(session)
            raise QQCallAudioError("audio_chunk_invalid")
        if len(pcm_bytes) % (session.source_channels * SAMPLE_WIDTH):
            await self._abort_session(session)
            raise QQCallAudioError("audio_pcm_unaligned")
        session.expected_seq += 1
        converted = session.resampler.process(_to_mono(pcm_bytes, session.source_channels))
        if session.output_channels == 2:
            converted = _to_stereo(converted)
        await self._stage_output(session, converted)

    async def stream_end(self, stream_id: str) -> dict[str, Any]:
        """Signal producer EOF, then await callback completion and inactive stream."""
        session = self._get_session(stream_id)
        try:
            self._check_session_fence(session)
        except QQCallAudioError:
            await self._abort_session(session)
            raise
        tail = session.resampler.flush()
        if session.output_channels == 2:
            tail = _to_stereo(tail)
        await self._stage_output(session, tail)
        if session.stage:
            valid_frames = len(session.stage) // (session.output_channels * SAMPLE_WIDTH)
            await self._enqueue_block(session, bytes(session.stage) + bytes(session.block_bytes - len(session.stage)))
            session.output_frames += valid_frames
            session.stage.clear()
        with session.state_lock:
            if session.cancelled:
                raise QQCallAudioError("audio_playback_cancelled")
            session.producer_eof = True
        deadline = asyncio.get_running_loop().time() + self.config.drain_timeout_s
        while True:
            try:
                self._check_session_fence(session)
            except QQCallAudioError:
                await self._abort_session(session)
                raise
            if session.completed.is_set():
                active = await self._run_native(
                    session.output_stream.is_active,
                    self.config.cancel_timeout_s,
                    "audio_drain_check_timeout",
                    late_cleanup=lambda _value: self._cleanup_quarantined(),
                )
                if not active:
                    break
            if asyncio.get_running_loop().time() >= deadline:
                await self._abort_session(session)
                raise QQCallAudioError("audio_drain_timeout")
            await asyncio.sleep(0.01)
        try:
            await self._run_native(
                _close_stream,
                self.config.cancel_timeout_s,
                "audio_stop_timeout",
                session.output_stream,
                late_cleanup=lambda _value: self._cleanup_quarantined(skip_stream=session.output_stream),
            )
        except QQCallAudioError:
            self._last_error = "audio_stop_timeout"
            self._state = "cleanup_pending" if self._cleanup_task is not None else "cleanup_failed"
            raise
        await self._clear_session(session)
        return {
            "stream_id": stream_id,
            "render_drained": True,
            "drain_confirmed": True,
            "remote_playback_confirmed": False,
            "sample_rate": session.output_rate,
            "channels": session.output_channels,
            "rendered_frames": session.output_frames,
        }

    async def stream_abort(self, stream_id: str) -> None:
        """Discard queued samples and abort only the matching playback stream."""
        session = self._session
        if session is not None and session.stream_id == stream_id:
            await self._abort_session(session)

    async def cancel_playback(self) -> None:
        """Invalidate queued playback and abort its PortAudio callback stream."""
        if self._session is not None:
            await self._abort_session(self._session)

    async def _abort_session(self, session: _PlaybackSession) -> None:
        with session.state_lock:
            if session.abort_started:
                return
            session.abort_started = True
            if not session.cancelled:
                session.cancelled = True
                session.producer_eof = True
                if self._session is session:
                    self._playback_generation += 1
        while True:
            try:
                session.output_queue.get_nowait()
            except queue.Empty:
                break
        session.stage.clear()
        if session.output_stream is not None:
            try:
                await self._run_native(
                    _abort_close_stream,
                    self.config.cancel_timeout_s,
                    "audio_cancel_timeout",
                    session.output_stream,
                    late_cleanup=lambda _value: self._cleanup_quarantined(skip_stream=session.output_stream),
                )
            except QQCallAudioError:
                self._last_error = "audio_cancel_timeout"
                self._state = "cleanup_pending" if self._cleanup_task is not None else "cleanup_failed"
                raise
        await self._clear_session(session)

    async def _stage_output(self, session: _PlaybackSession, pcm: bytes) -> None:
        if not pcm:
            return
        frame_bytes = session.output_channels * SAMPLE_WIDTH
        if len(pcm) % frame_bytes:
            await self._abort_session(session)
            raise QQCallAudioError("audio_pcm_unaligned")
        session.stage.extend(pcm)
        while len(session.stage) >= session.block_bytes:
            block = bytes(session.stage[: session.block_bytes])
            del session.stage[: session.block_bytes]
            await self._enqueue_block(session, block)
            session.output_frames += session.block_bytes // frame_bytes

    async def _enqueue_block(self, session: _PlaybackSession, block: bytes) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.drain_timeout_s
        while True:
            try:
                self._check_session_fence(session)
            except QQCallAudioError:
                await self._abort_session(session)
                raise
            with session.state_lock:
                if session.cancelled or not self._session_is_current(session):
                    raise QQCallAudioError("audio_playback_cancelled")
            try:
                await asyncio.to_thread(session.output_queue.put, block, True, 0.05)
                return
            except queue.Full:
                if asyncio.get_running_loop().time() >= deadline:
                    await self._abort_session(session)
                    raise QQCallAudioError("audio_playback_queue_full") from None

    def _get_session(self, stream_id: str) -> _PlaybackSession:
        session = self._session
        if session is None or session.stream_id != stream_id:
            raise QQCallAudioError("audio_stream_not_found")
        if session.cancelled or not self._session_is_current(session):
            raise QQCallAudioError("audio_playback_cancelled")
        return session

    def _check_session_fence(self, session: _PlaybackSession) -> None:
        self._check_fence(session.fence)
        if not self._session_is_current(session):
            raise QQCallAudioError("audio_scope_stale")

    @staticmethod
    def _check_fence(fence: Callable[[], bool]) -> None:
        if not callable(fence):
            raise QQCallAudioError("audio_fence_invalid")
        try:
            current = fence()
        except Exception as exc:
            del exc
            raise QQCallAudioError("audio_scope_stale") from None
        if current is not True:
            raise QQCallAudioError("audio_scope_stale")

    def _session_is_current(self, session: _PlaybackSession) -> bool:
        return (
            self._session is session
            and session.run_generation == self._run_generation
            and session.playback_generation == self._playback_generation
        )

    async def _capture_dispatcher(self, generation: int) -> None:
        items = self._capture_queue
        available = self._capture_available
        while items is not None and available is not None and not self._capture_stop.is_set():
            # Native capture already owns a bounded thread-safe queue. Waiting
            # for its notification must not depend on the application's shared
            # executor: unrelated blocking work can otherwise overflow PCM.
            available.clear()
            try:
                item = items.get_nowait()
            except queue.Empty:
                await available.wait()
                continue
            if generation != self._run_generation or self._capture_stop.is_set():
                continue
            callback = self._capture_callback
            if callback is None:
                continue
            try:
                result = callback(item.pcm, item.metadata)
                if inspect.isawaitable(result):
                    await result
            except QQCallAudioError as exc:
                self._signal_failure(exc.code)
                return
            except Exception as exc:
                del exc
                self._signal_failure("capture_callback_failed")
                return

    def _wake_capture_dispatcher(self) -> None:
        available, loop = self._capture_available, self._loop
        if available is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(available.set)
            except RuntimeError:
                pass  # The owning loop has already closed during cleanup.

    async def _forward_capture_to_universal(self, pcm: bytes, metadata: dict[str, Any]) -> None:
        """Validate and forward one canonical frame through the public SDK.

        This method runs in the QQCall PCM package's bounded capture dispatcher;
        it owns sample continuity and contains no speech recognition logic.
        """
        sender = self._uplink_send_pcm
        fence = self._uplink_fence
        if sender is None or fence is None:
            raise QQCallAudioError("universal_api_capture_unsupported")
        generation = self._run_generation
        self._check_fence(fence)
        if (
            type(pcm) is not bytes
            or not pcm
            or not isinstance(metadata, Mapping)
            or metadata.get("codec") != PCM_CODEC
            or metadata.get("sample_rate") != CORE_SAMPLE_RATE
            or metadata.get("channels") != CORE_CHANNELS
            or metadata.get("sample_width") != SAMPLE_WIDTH
            or metadata.get("scope") != self._scope
            or metadata.get("generation") != self._run_generation
            or type(metadata.get("sample_counter")) is not int
            or type(metadata.get("sample_count")) is not int
            or metadata["sample_counter"] != self._uplink_expected_sample
            or metadata["sample_count"] != len(pcm) // SAMPLE_WIDTH
            or len(pcm) > min(self.config.max_chunk_bytes, 65_536)
            or len(pcm) % SAMPLE_WIDTH
        ):
            raise QQCallAudioError("universal_api_capture_frame_invalid")
        self._uplink_expected_sample += metadata["sample_count"]
        captured_at_ms = round(time.time() * 1000)
        result = sender(pcm, source_id="main")
        if inspect.isawaitable(result):
            result = await result
        self._check_fence(fence)
        if generation != self._run_generation or self._capture_stop.is_set():
            raise QQCallAudioError("audio_scope_stale")
        if not isinstance(result, Mapping) or result.get("accepted") is not True:
            raise QQCallAudioError("universal_api_protocol_invalid")
        self._uplink_frames_sent += 1
        snapshot = getattr(self, "_diagnostic_snapshot", None)
        if snapshot is not None:
            try:
                snapshot.observe(pcm, metadata, captured_at_ms)
            except Exception:
                pass  # Opt-in snapshot I/O cannot change a transport receipt.

    def _open_capture_stream(self, audio: Any, device: _ResolvedDevice, channels: int, generation: int) -> Any:
        return audio.open(
            format=self._audio_api.paInt16, channels=channels, rate=device.sample_rate, input=True,
            input_device_index=device.index, frames_per_buffer=_frames_for_ms(device.sample_rate, self.config.frame_ms),
            stream_callback=self._make_capture_callback(generation), start=False,
        )

    def _start_capture_native(self, generation: int) -> tuple[Any, _ResolvedDevice, _ResolvedDevice, int, _Pcm16Resampler, Any]:
        module = self._audio_api
        if module is None:
            raise QQCallAudioError("audio_dependency_unavailable")
        audio = module.PyAudio()
        stream = None
        try:
            capture, playback = self._resolve_configured_devices(audio)
            channels = min(2, capture.max_input_channels)
            if channels < 1:
                raise QQCallAudioError("capture_device_unavailable")
            resampler = _Pcm16Resampler(capture.sample_rate, CORE_SAMPLE_RATE)
            self._capture_device = capture
            self._playback_device = playback
            self._capture_channels = channels
            self._capture_resampler = resampler
            stream = self._open_capture_stream(audio, capture, channels, generation)
            stream.start_stream()
            return audio, capture, playback, channels, resampler, stream
        except Exception as exc:
            native_errno = _native_errno(exc)
            cleanup_failed = False
            if stream is not None:
                try:
                    _abort_close_stream(stream)
                except Exception as cleanup_exc:
                    cleanup_failed = True
                    native_errno = native_errno if native_errno is not None else _native_errno(cleanup_exc)
            try:
                audio.terminate()
            except Exception as cleanup_exc:
                cleanup_failed = True
                native_errno = native_errno if native_errno is not None else _native_errno(cleanup_exc)
            if cleanup_failed:
                raise QQCallAudioError("audio_cleanup_failed", native_errno=native_errno) from None
            if isinstance(exc, QQCallAudioError):
                if native_errno is not None and exc.native_errno is None:
                    raise QQCallAudioError(exc.code, native_errno=native_errno) from None
                raise
            raise QQCallAudioError("audio_start_failed", native_errno=native_errno) from None

    def _open_and_start_playback(
        self, audio: Any, device: _ResolvedDevice, channels: int, session: _PlaybackSession
    ) -> Any:
        stream = self._open_playback_stream(audio, device, channels, session)
        try:
            stream.start_stream()
            return stream
        except Exception as exc:
            native_errno = _native_errno(exc)
            try:
                _abort_close_stream(stream)
            except Exception as cleanup_exc:
                native_errno = native_errno if native_errno is not None else _native_errno(cleanup_exc)
                raise QQCallAudioError("audio_cleanup_failed", native_errno=native_errno) from None
            raise QQCallAudioError("playback_start_failed", native_errno=native_errno) from None

    def _make_capture_callback(self, generation: int) -> Callable[..., tuple[None, int]]:
        def callback(in_data: bytes | None, frame_count: int, time_info: Any, status_flags: int) -> tuple[None, int]:
            del time_info, frame_count
            if generation != self._run_generation or self._capture_stop.is_set():
                return None, getattr(self._audio_api, "paAbort", 2)
            if status_flags & getattr(self._audio_api, "paInputOverflow", 2):
                self._capture_overruns += 1
                self._signal_failure("capture_device_overflow")
                return None, getattr(self._audio_api, "paAbort", 2)
            if not in_data:
                return None, getattr(self._audio_api, "paContinue", 0)
            try:
                resampler = self._capture_resampler
                items = self._capture_queue
                if resampler is None or items is None:
                    return None, getattr(self._audio_api, "paAbort", 2)
                canonical = resampler.process(_to_mono(in_data, self._capture_channels))
                if not canonical:
                    return None, getattr(self._audio_api, "paContinue", 0)
                count = len(canonical) // SAMPLE_WIDTH
                first = self._capture_sample_counter
                self._capture_sample_counter += count
                item = _CaptureItem(canonical, {
                    "codec": PCM_CODEC, "sample_rate": CORE_SAMPLE_RATE, "channels": CORE_CHANNELS,
                    "sample_width": SAMPLE_WIDTH, "sample_counter": first, "sample_count": count,
                    "monotonic_ns": time.monotonic_ns(), "generation": generation, "scope": self._scope,
                })
                items.put_nowait(item)
                if items.qsize() == 1:
                    self._wake_capture_dispatcher()
            except queue.Full:
                self._capture_overruns += 1
                self._signal_failure("capture_queue_overrun")
                return None, getattr(self._audio_api, "paAbort", 2)
            except Exception as exc:
                del exc
                self._signal_failure("capture_processing_failed")
                return None, getattr(self._audio_api, "paAbort", 2)
            return None, getattr(self._audio_api, "paContinue", 0)
        return callback

    def _open_playback_stream(self, audio: Any, device: _ResolvedDevice, channels: int, session: _PlaybackSession) -> Any:
        def abort_stale_callback(length: int) -> tuple[bytes, int]:
            with session.state_lock:
                if not session.cancelled:
                    session.cancelled = True
                    session.producer_eof = True
            session.callback_pending.clear()
            session.stage.clear()
            while True:
                try:
                    session.output_queue.get_nowait()
                except queue.Empty:
                    break
            session.completed.set()
            return bytes(length), getattr(self._audio_api, "paAbort", 2)

        def callback(out_data: Any, frame_count: int, time_info: Any, status_flags: int) -> tuple[bytes, int]:
            del out_data, time_info, status_flags
            length = frame_count * channels * SAMPLE_WIDTH
            try:
                # This callback runs on the native audio thread. Fence checks
                # stay synchronous and precede every dequeue or render.
                self._check_session_fence(session)
            except QQCallAudioError:
                return abort_stale_callback(length)
            if session.cancelled:
                return abort_stale_callback(length)
            output = bytearray()
            try:
                while len(output) < length:
                    if session.callback_pending:
                        take = min(length - len(output), len(session.callback_pending))
                        output.extend(session.callback_pending[:take])
                        del session.callback_pending[:take]
                        if len(output) == length:
                            break
                    else:
                        try:
                            session.callback_pending.extend(session.output_queue.get_nowait())
                        except queue.Empty:
                            if session.producer_eof:
                                output.extend(bytes(length - len(output)))
                                session.completed.set()
                                return bytes(output), getattr(self._audio_api, "paComplete", 1)
                            missing = length - len(output)
                            session.underrun_frames += math.ceil(missing / (channels * SAMPLE_WIDTH))
                            output.extend(bytes(missing))
                            return bytes(output), getattr(self._audio_api, "paContinue", 0)
                return bytes(output), getattr(self._audio_api, "paContinue", 0)
            except Exception as exc:
                del exc
                self._signal_failure("playback_callback_failed")
                session.completed.set()
                return bytes(length), getattr(self._audio_api, "paAbort", 2)

        return audio.open(
            format=self._audio_api.paInt16, channels=channels, rate=device.sample_rate, output=True,
            output_device_index=device.index, frames_per_buffer=_frames_for_ms(device.sample_rate, self.config.frame_ms),
            stream_callback=callback, start=False,
        )

    def _signal_failure(self, code: str) -> None:
        with self._state_lock:
            if self._failure_signaled.is_set():
                return
            self._failure_signaled.set()
            self._last_error = code
            self._native_errno = None
            self._run_generation += 1
            self._capture_stop.set()
            self._wake_capture_dispatcher()
            session = self._session
            if session is not None:
                with session.state_lock:
                    session.cancelled = True
                    session.producer_eof = True
                self._playback_generation += 1
            if self._state in ("ready", "starting"):
                self._state = "failed"
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._dispatch_failure, code)
            except RuntimeError:
                pass

    def _dispatch_failure(self, code: str) -> None:
        callback = self._failure_callback
        if callback is None:
            return

        async def invoke() -> None:
            try:
                result = callback(code)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass

        asyncio.create_task(invoke())

    async def _cleanup_failed_start(self) -> None:
        task = self._capture_dispatch_task
        self._capture_dispatch_task = None
        self._capture_stop.set()
        self._wake_capture_dispatcher()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self._run_native(
                self._cleanup_native_resources,
                self.config.cancel_timeout_s,
                "audio_stop_timeout",
            )
        except QQCallAudioError:
            if self._cleanup_task is not None:
                self._state = "cleanup_pending"
                return
            self._state = "cleanup_failed"
            self._last_error = "audio_cleanup_failed"
            return
        self._clear_released_resources()
        self._shutdown_native_executor()
        self._state = "stopped"

    async def _clear_session(self, session: _PlaybackSession) -> None:
        if self._session is session:
            self._session = None

    async def _run_native(
        self,
        function: Callable[..., Any],
        timeout: float,
        code: str,
        *args: Any,
        late_cleanup: Callable[[Any], None] | None = None,
    ) -> Any:
        """Bound one native operation and quarantine the backend if it outlives the caller."""
        executor = self._ensure_native_executor()
        loop = asyncio.get_running_loop()
        operation = loop.run_in_executor(executor, functools.partial(function, *args))
        try:
            return await asyncio.wait_for(asyncio.shield(operation), timeout=timeout)
        except asyncio.CancelledError:
            self._quarantine_native_operation(operation, late_cleanup, executor, code)
            raise
        except asyncio.TimeoutError:
            self._quarantine_native_operation(operation, late_cleanup, executor, code)
            raise QQCallAudioError(code) from None
        except QQCallAudioError as exc:
            self._last_error = exc.code
            if exc.native_errno is not None:
                self._native_errno = exc.native_errno
            raise
        except Exception as exc:
            native_errno = _native_errno(exc)
            self._last_error = code
            if native_errno is not None:
                self._native_errno = native_errno
            del exc
            raise QQCallAudioError(code) from None

    def _quarantine_native_operation(
        self,
        operation: asyncio.Future[Any],
        late_cleanup: Callable[[Any], None] | None,
        executor: concurrent.futures.ThreadPoolExecutor,
        code: str,
    ) -> None:
        """Retain ownership of an uncancellable native operation until cleanup finishes."""
        self._run_generation += 1
        self._playback_generation += 1
        self._capture_stop.set()
        self._wake_capture_dispatcher()
        self._state = "cleanup_pending"
        self._last_error = code
        if self._session is not None:
            with self._session.state_lock:
                self._session.cancelled = True
                self._session.producer_eof = True
        coordinator = asyncio.create_task(self._finish_quarantined(operation, late_cleanup, executor))
        self._cleanup_task = coordinator
        coordinator.add_done_callback(self._cleanup_task_done)

    async def _finish_quarantined(
        self,
        operation: asyncio.Future[Any],
        late_cleanup: Callable[[Any], None] | None,
        executor: concurrent.futures.ThreadPoolExecutor,
    ) -> None:
        try:
            result = await operation
            if late_cleanup is not None:
                await asyncio.get_running_loop().run_in_executor(executor, functools.partial(late_cleanup, result))
            dispatch_task = self._capture_dispatch_task
            self._capture_dispatch_task = None
            if dispatch_task is not None and dispatch_task is not asyncio.current_task():
                dispatch_task.cancel()
                await asyncio.gather(dispatch_task, return_exceptions=True)
            self._clear_released_resources()
            self._state = "stopped"
            self._shutdown_native_executor()
        except Exception as exc:
            native_errno = _native_errno(exc)
            if native_errno is not None:
                self._native_errno = native_errno
            del exc
            self._last_error = "audio_cleanup_failed"
            self._state = "cleanup_failed"

    def _ensure_native_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._native_executor is None:
            self._native_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="qqcall-wasapi",
            )
        return self._native_executor

    def _shutdown_native_executor(self) -> None:
        executor = self._native_executor
        self._native_executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _cleanup_quarantined(self, *, extra_stream: Any | None = None, skip_stream: Any | None = None) -> None:
        self._cleanup_native_resources(extra_stream=extra_stream, skip_stream=skip_stream)

    def _cleanup_native_resources(self, *, extra_stream: Any | None = None, skip_stream: Any | None = None) -> None:
        session = self._session
        output_stream = session.output_stream if session is not None else None
        streams = []
        for stream in (extra_stream, output_stream, self._capture_stream):
            if stream is not None and stream is not skip_stream and all(stream is not existing for existing in streams):
                streams.append(stream)
        for stream in streams:
            if stream is output_stream or stream is extra_stream:
                _abort_close_stream(stream)
            else:
                _close_stream(stream)
        if self._audio is not None:
            self._audio.terminate()

    def _dispose_capture_setup(self, setup: Any) -> None:
        audio, _capture, _playback, _channels, _resampler, stream = setup
        _abort_close_stream(stream)
        audio.terminate()

    def _clear_released_resources(self) -> None:
        self._audio = None
        self._audio_api = None
        self._capture_stream = None
        self._capture_device = None
        self._playback_device = None
        self._capture_channels = 0
        self._capture_resampler = None
        self._capture_queue = None
        self._capture_available = None
        self._session = None
        self._scope = None
        self._capture_callback = None
        self._failure_callback = None
        self._uplink_send_pcm = None
        self._uplink_fence = None
        self._uplink_expected_sample = 0

    def _cleanup_task_done(self, task: asyncio.Task[Any]) -> None:
        if self._cleanup_task is task:
            self._cleanup_task = None

    def _load_audio_module(self) -> Any:
        module = self._audio_module
        if module is None:
            for module_name in ("pyaudiowpatch", "pyaudio"):
                try:
                    module = importlib.import_module(module_name)
                    break
                except ImportError:
                    continue
        if module is None:
            raise QQCallAudioError("audio_dependency_unavailable")
        self._audio_api = module
        return module

    def _enumerate_with_module(self, module: Any) -> list[_ResolvedDevice]:
        audio = module.PyAudio()
        try:
            return self._enumerate_raw(audio)
        finally:
            audio.terminate()

    def _enumerate_raw(self, audio: Any) -> list[_ResolvedDevice]:
        devices = []
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)
            try:
                host = audio.get_host_api_info_by_index(int(info.get("hostApi", -1)))
                rate = int(round(float(info.get("defaultSampleRate"))))
                inputs, outputs = int(info.get("maxInputChannels", 0)), int(info.get("maxOutputChannels", 0))
                device_index = int(info.get("index", index))
            except (TypeError, ValueError, OverflowError):
                continue
            name, host_name = info.get("name"), host.get("name")
            if (
                not isinstance(name, str) or not 1 <= len(name) <= _MAX_DEVICE_NAME_CHARS
                or not isinstance(host_name, str) or not _MIN_RATE <= rate <= _MAX_NATIVE_RATE
            ):
                continue
            devices.append(_ResolvedDevice(
                device_index, name, host_name, max(inputs, 0), max(outputs, 0),
                info.get("isLoopbackDevice") is True, rate,
            ))
        return devices

    def _resolve_configured_devices(self, audio: Any) -> tuple[_ResolvedDevice, _ResolvedDevice]:
        scope = self.config.device_scope
        if not self.config.enabled or scope is None:
            raise QQCallAudioError("audio_config_incomplete")
        wasapi = [device for device in self._enumerate_raw(audio) if "wasapi" in device.host_api.casefold()]
        if self.config.capture_mode == "loopback":
            capture = self._resolve_unique(
                wasapi, self.config.capture_device,
                lambda d: d.max_input_channels > 0 and d.is_loopback_device, "capture_device_unavailable",
            )
            capture_render = self._resolve_unique(
                wasapi, scope.capture_render_name, lambda d: d.max_output_channels > 0, "capture_route_unavailable",
            )
            if _strip_loopback_suffix(capture.name).casefold() != capture_render.name.casefold():
                raise QQCallAudioError("audio_config_scope_mismatch")
            if not _has_virtual_marker(capture_render.name):
                raise QQCallAudioError("audio_endpoint_not_virtual")
        else:
            capture = self._resolve_unique(
                wasapi, self.config.capture_device,
                lambda d: d.max_input_channels > 0 and not d.is_loopback_device, "capture_device_unavailable",
            )
            if capture.name.casefold() != scope.capture_render_name.casefold():
                raise QQCallAudioError("audio_config_scope_mismatch")
            if not _has_virtual_marker(capture.name):
                raise QQCallAudioError("audio_endpoint_not_virtual")
        playback = self._resolve_unique(
            wasapi, self.config.playback_device, lambda d: d.max_output_channels > 0, "playback_device_unavailable",
        )
        if not _has_virtual_marker(playback.name):
            raise QQCallAudioError("audio_endpoint_not_virtual")
        capture_base = _strip_loopback_suffix(capture.name) if self.config.capture_mode == "loopback" else capture.name
        if capture_base.casefold() == playback.name.casefold():
            raise QQCallAudioError("audio_routes_must_be_distinct")
        return capture, playback

    @staticmethod
    def _resolve_unique(devices: list[_ResolvedDevice], name: str | None, direction: Callable[[_ResolvedDevice], bool],
                        missing_code: str) -> _ResolvedDevice:
        if name is None:
            raise QQCallAudioError("audio_config_incomplete")
        matches = [device for device in devices if device.name.casefold() == name.casefold() and direction(device)]
        if len(matches) != 1:
            raise QQCallAudioError(missing_code if not matches else "audio_device_ambiguous")
        return matches[0]

    def _decode_wav(self, encoded: str) -> tuple[bytes, int, int]:
        if not isinstance(encoded, str) or not encoded or len(encoded) > 32 * 1024 * 1024:
            raise QQCallAudioError("audio_wav_invalid")
        try:
            raw = base64.b64decode(encoded, validate=True)
            with wave.open(io.BytesIO(raw), "rb") as wav:
                channels, rate = wav.getnchannels(), wav.getframerate()
                width, frames = wav.getsampwidth(), wav.getnframes()
                if (wav.getcomptype() != "NONE" or channels not in (1, 2) or width != SAMPLE_WIDTH
                    or not _valid_rate(rate) or frames < 1 or frames / rate > self.config.max_wav_seconds):
                    raise QQCallAudioError("audio_wav_unsupported")
                pcm = wav.readframes(frames)
                if len(pcm) != frames * channels * SAMPLE_WIDTH:
                    raise QQCallAudioError("audio_wav_invalid")
        except QQCallAudioError:
            raise
        except (binascii.Error, ValueError, EOFError, wave.Error) as exc:
            del exc
            raise QQCallAudioError("audio_wav_invalid") from None
        return pcm, rate, channels


def _validate_device_name(value: Any) -> None:
    if (not isinstance(value, str) or not value or len(value) > _MAX_DEVICE_NAME_CHARS
        or value != value.strip() or any(ord(char) < 32 for char in value)):
        raise QQCallAudioError("audio_config_invalid")


def _valid_core_id(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and all(
        char.isascii() and (char.isalnum() or char in "_-.:") for char in value
    )


def _finite_range(value: Any, minimum: float, maximum: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(number) and minimum <= number <= maximum


def _valid_native_errno(value: Any) -> bool:
    return type(value) is int and -65_535 <= value <= 65_535


def _native_errno(exc: BaseException) -> int | None:
    value = getattr(exc, "native_errno", None)
    if not _valid_native_errno(value):
        value = getattr(exc, "errno", None)
    return value if _valid_native_errno(value) else None


def _strip_loopback_suffix(name: str) -> str:
    suffix = " [loopback]"
    return name[:-len(suffix)] if name.casefold().endswith(suffix) else name


def _has_virtual_marker(name: str) -> bool:
    folded = name.casefold()
    return any(marker in folded for marker in _VIRTUAL_MARKERS)


def _valid_rate(rate: int) -> bool:
    return type(rate) is int and _MIN_RATE <= rate <= _MAX_RATE


def _frames_for_ms(rate: int, duration_ms: int) -> int:
    return max(1, (rate * duration_ms + 500) // 1_000)


def _unpack_mono(pcm: bytes) -> list[int]:
    if len(pcm) % SAMPLE_WIDTH:
        raise QQCallAudioError("audio_pcm_unaligned")
    return [item[0] for item in struct.iter_unpack("<h", pcm)]


def _pack_mono(samples: list[int]) -> bytes:
    if not samples:
        return b""
    return struct.pack(f"<{len(samples)}h", *(_clamp16(sample) for sample in samples))


def _clamp16(value: int) -> int:
    return max(-32_768, min(32_767, value))


def _to_mono(pcm: bytes, channels: int) -> bytes:
    if channels not in (1, 2):
        raise QQCallAudioError("audio_format_unsupported")
    if len(pcm) % (channels * SAMPLE_WIDTH):
        raise QQCallAudioError("audio_pcm_unaligned")
    if channels == 1:
        return pcm
    return _pack_mono([round((left + right) / 2) for left, right in struct.iter_unpack("<hh", pcm)])


def _to_stereo(mono: bytes) -> bytes:
    samples = _unpack_mono(mono)
    return struct.pack(f"<{len(samples) * 2}h", *(sample for item in samples for sample in (item, item))) if samples else b""


def _close_stream(stream: Any) -> None:
    if stream.is_active():
        stream.stop_stream()
    stream.close()


def _abort_close_stream(stream: Any) -> None:
    # PyAudio's Stream has no abort_stream method. The active callback observes
    # the cancelled generation and returns paAbort; Pa_CloseStream then tears it down.
    stream.close()


__all__ = [
    "AudioDeviceInfo", "CORE_CHANNELS", "CORE_SAMPLE_RATE", "PCM_CODEC", "QQCallAudioBackend",
    "QQCallAudioConfig", "QQCallAudioDeviceScope", "QQCallAudioError",
]
