"""Per-session UniversalVC audio engine.

This module is deliberately independent of the standalone adapter's live
``config.toml``. Callers provide all capture, output, identity, denoise, and
speaker-log choices in the session request; only model/resource paths and Core
connectivity come from service settings.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import math
import tempfile
import uuid
import wave
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .audio_capture import AudioCapture, MicrophoneCapture
from .audio_output import AudioOutput
from .config import (
    AdapterConfig,
    AudioCaptureConfig,
    AudioOutputConfig,
    DenoiseConfig,
    MicrophoneConfig,
    NachoBotConfig,
    PromptsConfig,
    SpeakerConfig,
    VADConfig,
)
from .core_stream_client import CoreAudioStreamClient
from .service_config import ServiceSettings
from .vad_processor import VADProcessor
from .voice_codec import samples_to_wav_base64


class SessionRequestError(ValueError):
    """A malformed or unsupported per-session audio request."""


class SessionUnavailableError(RuntimeError):
    """A requested service capability or selected resource is unavailable."""


def _text(value: Any, name: str, *, maximum: int = 256, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > maximum:
        raise SessionRequestError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def _number(value: Any, name: str, low: float, high: float, *, default: float | None = None) -> float:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SessionRequestError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise SessionRequestError(f"{name} is outside the supported range")
    return result


def validate_session_options(options: dict[str, Any], settings: ServiceSettings) -> dict[str, Any]:
    """Validate and normalize the complete, independently scoped request."""
    if not isinstance(options, dict):
        raise SessionRequestError("session request must be an object")
    allowed_options = {
        "platform", "scope", "mode", "sources", "output", "denoise", "speaker",
        "speaker_log", "vad", "max_duration_seconds",
    }
    if set(options) - allowed_options:
        raise SessionRequestError("session request contains unsupported fields")
    platform = _text(options.get("platform"), "platform", maximum=64)
    scope = _text(options.get("scope"), "scope", maximum=256)
    mode = options.get("mode")
    if mode not in {"pipeline", "vad"}:
        raise SessionRequestError("mode must be 'pipeline' or 'vad'")
    raw_sources = options.get("sources")
    if not isinstance(raw_sources, list) or not 1 <= len(raw_sources) <= settings.max_sources_per_session:
        raise SessionRequestError("sources must contain between one and the configured source limit")
    denoise = options.get("denoise", False)
    speaker = options.get("speaker", False)
    speaker_log = options.get("speaker_log", False)
    if type(denoise) is not bool or type(speaker) is not bool or type(speaker_log) is not bool:
        raise SessionRequestError("denoise, speaker, and speaker_log must be booleans")
    output = options.get("output")
    if not isinstance(output, dict):
        raise SessionRequestError("output must be an object")
    if set(output) - {"mode", "device_name", "sample_rate"}:
        raise SessionRequestError("output contains unsupported fields")
    output_mode = output.get("mode")
    if output_mode not in {"none", "device"}:
        raise SessionRequestError("output.mode must be 'none' or 'device'")
    output_name = _text(output.get("device_name"), "output.device_name", maximum=256, required=output_mode == "device")
    output_rate = output.get("sample_rate", 44100)
    if isinstance(output_rate, bool) or not isinstance(output_rate, int) or not 8_000 <= output_rate <= 192_000:
        raise SessionRequestError("output.sample_rate is invalid")

    vad_options = options.get("vad", {})
    if not isinstance(vad_options, dict):
        raise SessionRequestError("vad must be an object")
    if set(vad_options) - {"threshold", "min_silence_duration", "min_speech_duration"}:
        raise SessionRequestError("vad contains unsupported fields")
    vad = {
        "threshold": _number(vad_options.get("threshold"), "vad.threshold", 0.01, 0.99, default=0.5),
        "min_silence_duration": _number(vad_options.get("min_silence_duration"), "vad.min_silence_duration", 0.05, 5.0, default=0.25),
        "min_speech_duration": _number(vad_options.get("min_speech_duration"), "vad.min_speech_duration", 0.05, 5.0, default=0.3),
    }

    normalized_sources = []
    source_ids: set[str] = set()
    for source in raw_sources:
        if not isinstance(source, dict):
            raise SessionRequestError("each source must be an object")
        if set(source) - {"id", "input", "format", "sample_rate", "channels", "capture", "identity"}:
            raise SessionRequestError("source contains unsupported fields")
        source_id = _text(source.get("id"), "source.id", maximum=64)
        if source_id in source_ids:
            raise SessionRequestError("source IDs must be unique")
        source_ids.add(source_id)
        input_mode = source.get("input")
        if input_mode not in {"external", "process", "microphone"}:
            raise SessionRequestError("source.input must be external, process, or microphone")
        fmt = source.get("format")
        if fmt not in {"s16le", "f32le"}:
            raise SessionRequestError("source.format must be s16le or f32le")
        sample_rate = source.get("sample_rate")
        channels = source.get("channels")
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8_000 <= sample_rate <= 192_000:
            raise SessionRequestError("source.sample_rate is invalid")
        if isinstance(channels, bool) or not isinstance(channels, int) or channels not in (1, 2):
            raise SessionRequestError("source.channels must be one or two")
        capture = source.get("capture", {})
        identity = source.get("identity", {})
        if not isinstance(capture, dict) or not isinstance(identity, dict):
            raise SessionRequestError("source.capture and source.identity must be objects")
        if set(capture) - {
            "target_pid", "target_process_name", "system_capture_device", "device_name",
            "push_to_talk", "ptt_key",
        }:
            raise SessionRequestError("source.capture contains unsupported fields")
        if set(identity) - {"mode", "id", "name"}:
            raise SessionRequestError("source.identity contains unsupported fields")
        target_pid = capture.get("target_pid")
        target_name = capture.get("target_process_name")
        system_device = capture.get("system_capture_device")
        mic_device = capture.get("device_name")
        ptt = capture.get("push_to_talk", False)
        ptt_key = capture.get("ptt_key", "v")
        if type(ptt) is not bool:
            raise SessionRequestError("capture.push_to_talk must be a boolean")
        if input_mode == "external":
            if any(value not in (None, "", False) for value in (target_pid, target_name, system_device, mic_device, ptt)):
                raise SessionRequestError("external source must not select a process or device")
        elif input_mode == "process":
            has_pid = target_pid is not None
            has_name = bool(target_name)
            has_system_device = bool(system_device)
            if sum((has_pid, has_name, has_system_device)) != 1:
                raise SessionRequestError("process source requires exactly one process selector or system_capture_device")
            if has_pid and (isinstance(target_pid, bool) or not isinstance(target_pid, int) or target_pid <= 0):
                raise SessionRequestError("capture.target_pid must be a positive integer")
            if has_name:
                target_name = _text(target_name, "capture.target_process_name", maximum=256)
            if has_system_device:
                system_device = _text(system_device, "capture.system_capture_device", maximum=256)
            if mic_device or ptt:
                raise SessionRequestError("process source cannot configure microphone fields")
        else:
            if not mic_device or target_pid is not None or target_name or system_device:
                raise SessionRequestError("microphone source requires only capture.device_name")
            mic_device = _text(mic_device, "capture.device_name", maximum=256)
            ptt_key = _text(ptt_key, "capture.ptt_key", maximum=32)
        identity_mode = identity.get("mode")
        if identity_mode not in {"fixed", "speaker"}:
            raise SessionRequestError("source.identity.mode must be fixed or speaker")
        identity_id = _text(identity.get("id"), "identity.id", maximum=128, required=identity_mode == "fixed")
        identity_name = _text(identity.get("name"), "identity.name", maximum=128, required=identity_mode == "fixed")
        if identity_mode == "speaker" and not speaker:
            raise SessionRequestError("speaker identity requires speaker=true")
        if input_mode == "microphone" and identity_mode != "fixed":
            raise SessionRequestError("microphone source requires a fixed identity")
        if identity_mode == "fixed" and speaker:
            # Fixed platform-provided identities are allowed and bypass inference.
            pass
        normalized_sources.append({
            "id": source_id,
            "input": input_mode,
            "format": fmt,
            "sample_rate": sample_rate,
            "channels": channels,
            "capture": {
                "target_pid": target_pid,
                "target_process_name": target_name,
                "system_capture_device": system_device,
                "device_name": mic_device,
                "push_to_talk": ptt,
                "ptt_key": ptt_key,
            },
            "identity": {"mode": identity_mode, "id": identity_id, "name": identity_name},
        })

    if mode == "vad" and (
        len(normalized_sources) != 1
        or normalized_sources[0]["input"] != "external"
        or normalized_sources[0]["format"] != "s16le"
        or normalized_sources[0]["sample_rate"] != 16_000
        or normalized_sources[0]["channels"] != 1
    ):
        raise SessionRequestError("vad mode requires one external s16le 16000 Hz mono source")
    if mode == "vad" and (denoise or speaker):
        raise SessionRequestError("vad mode does not support denoise or speaker identification")
    max_duration = _number(
        options.get("max_duration_seconds"), "max_duration_seconds", 1,
        min(60, settings.max_utterance_seconds),
        default=min(60, settings.max_utterance_seconds),
    )

    return {
        "platform": platform,
        "scope": scope,
        "mode": mode,
        "sources": normalized_sources,
        "output": {"mode": output_mode, "device_name": output_name, "sample_rate": output_rate},
        "denoise": denoise,
        "speaker": speaker,
        "speaker_log": speaker_log,
        "vad": vad,
        "max_duration_seconds": max_duration,
    }


@dataclass
class SourceRuntime:
    source_id: str
    options: dict[str, Any]
    pipeline: Any = None
    vad: Any = None
    capture: Any = None
    speaking: bool = False
    speech_bytes: bytearray = field(default_factory=bytearray)
    turn_counter: int = 0
    failed: bool = False
    last_error: str = ""
    active_turn_id: str | None = None
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class UniversalVCSessionEngine:
    """Owns isolated source state and optional input/output device resources."""

    def __init__(
        self,
        settings: ServiceSettings,
        options: dict[str, Any],
        *,
        service_instance_id: str,
        session_id: str | None = None,
        session_generation: str | None = None,
        emit: Callable[..., Any] | None = None,
        pipeline_factory: Callable[..., Any] | None = None,
        vad_factory: Callable[..., Any] | None = None,
        capture_factory: Callable[..., Any] | None = None,
        microphone_factory: Callable[..., Any] | None = None,
        output_factory: Callable[..., Any] | None = None,
        core_factory: Callable[..., Any] | None = None,
    ):
        self.settings = settings
        self.options = validate_session_options(options, settings)
        self.service_instance_id = service_instance_id
        self.session_id = session_id or uuid.uuid4().hex
        self.session_generation = session_generation or uuid.uuid4().hex
        self.emit = emit
        self.logger = logging.getLogger(f"universalvc.session.{self.session_id[:12]}")
        self.pipeline_factory = pipeline_factory
        self.vad_factory = vad_factory or VADProcessor
        self.capture_factory = capture_factory or AudioCapture
        self.microphone_factory = microphone_factory or MicrophoneCapture
        self.output_factory = output_factory or AudioOutput
        self.core_factory = core_factory or CoreAudioStreamClient
        self.sources: dict[str, SourceRuntime] = {}
        self.output = None
        self.core_stream = None
        self._closed = False
        self._paused = False
        self._audio_sequences: dict[tuple[str, str], int] = {}
        self._stream_playback_metadata: dict[str, dict] = {}
        self._active_stream_source: str | None = None
        self._active_stream_id: str | None = None
        self._playback_generation = 0
        self._playback_lock = asyncio.Lock()
        self._scope_turns: OrderedDict[str, str] = OrderedDict()

    @property
    def resource_keys(self) -> set[str]:
        keys = set()
        for source in self.options["sources"]:
            capture = source["capture"]
            if source["input"] == "microphone":
                keys.add("input:" + capture["device_name"].casefold())
            elif source["input"] == "process":
                if capture["target_pid"] is not None:
                    keys.add(f"process:pid:{capture['target_pid']}")
                elif capture["target_process_name"]:
                    keys.add("process:name:" + capture["target_process_name"].casefold())
                else:
                    keys.add("input:" + capture["system_capture_device"].casefold())
        if self.options["output"]["mode"] == "device":
            keys.add("output:" + self.options["output"]["device_name"].casefold())
        return keys

    def _emit(self, event: str, source_id: str, **fields):
        if self.emit is None or self._closed:
            return
        result = self.emit(event, source_id, **fields)
        if asyncio.iscoroutine(result):
            asyncio.get_running_loop().create_task(result)

    def _asr_scope(self, source_id: str, context_key: str) -> str:
        # The server does not accept arbitrary Core scopes. Hash the complete
        # binding tuple and retain only the resulting opaque receipt key.
        seed = "\x1f".join((
            self.options["platform"], self.session_id, self.session_generation,
            source_id, context_key,
        ))
        scope = "uvc1:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()
        turn_id = uuid.uuid5(uuid.NAMESPACE_URL, seed).hex
        self._scope_turns[scope] = turn_id
        while len(self._scope_turns) > 256:
            self._scope_turns.popitem(last=False)
        return scope

    async def start(self) -> None:
        """Open only explicitly requested model and device resources."""
        try:
            if self.options["output"]["mode"] == "device":
                self.output = self.output_factory(
                    AudioOutputConfig(
                        device_name=self.options["output"]["device_name"],
                        sample_rate=self.options["output"]["sample_rate"],
                    ),
                    self.logger,
                    strict_device_selection=True,
                )
                await asyncio.to_thread(self.output.initialize)
            if self.options["mode"] == "vad":
                self._check_model(self.settings.vad_model_path, "VAD")
                source = self.options["sources"][0]
                vad = self.vad_factory(
                    model_path=str(self.settings.vad_model_path),
                    threshold=self.options["vad"]["threshold"],
                    min_silence_duration=self.options["vad"]["min_silence_duration"],
                    min_speech_duration=self.options["vad"]["min_speech_duration"],
                    logger=self.logger,
                )
                if not getattr(vad, "available", False):
                    raise SessionUnavailableError("requested VAD capability is unavailable")
                self.sources[source["id"]] = SourceRuntime(source["id"], source, vad=vad)
                return

            if self.pipeline_factory is None:
                from .audio_pipeline import AudioPipeline
                pipeline_factory = AudioPipeline
            else:
                pipeline_factory = self.pipeline_factory

            if not self.settings.vad_model_path.is_file():
                raise SessionUnavailableError("configured VAD model is unavailable")
            if self.options["denoise"]:
                try:
                    from .denoise import _RNNOISE_AVAILABLE
                except (ImportError, OSError) as exc:
                    raise SessionUnavailableError("requested denoise capability is unavailable") from exc
                if not _RNNOISE_AVAILABLE:
                    raise SessionUnavailableError("requested denoise capability is unavailable")
            if self.options["speaker"] and any(
                source["identity"]["mode"] == "speaker" for source in self.options["sources"]
            ):
                self._check_model(self.settings.speaker_model_path, "speaker identification")
                from .speaker_tracker import _SHERPA_AVAILABLE
                if not _SHERPA_AVAILABLE:
                    raise SessionUnavailableError("requested speaker identification is unavailable")

            self.core_stream = self.core_factory(
                self.settings.core_host,
                self.settings.core_port,
                self.settings.core_token,
            )
            loop = asyncio.get_running_loop()
            for source in self.options["sources"]:
                runtime = SourceRuntime(source["id"], source)
                self.sources[source["id"]] = runtime
                source_label = "".join(
                    character if character.isalnum() or character in "-_" else "_"
                    for character in source["id"]
                )[:64]
                source_logger = self.logger.getChild(f"source.{source_label or 'audio'}")
                microphone = source["input"] == "microphone"
                input_cfg = AudioCaptureConfig(
                    target_pid=source["capture"]["target_pid"],
                    target_process_name=source["capture"]["target_process_name"],
                    system_capture_device=source["capture"]["system_capture_device"],
                )
                mic_cfg = MicrophoneConfig(
                    enabled=microphone,
                    device_name=source["capture"]["device_name"],
                    owner_speaker_id=source["identity"]["id"] if source["identity"]["mode"] == "fixed" else "owner",
                    owner_speaker_name=source["identity"]["name"] if source["identity"]["mode"] == "fixed" else "Owner",
                    push_to_talk=source["capture"]["push_to_talk"],
                    ptt_key=source["capture"]["ptt_key"],
                )
                config = AdapterConfig(
                    capture=input_cfg,
                    output=AudioOutputConfig(),
                    nachobot=NachoBotConfig(self.settings.core_host, self.settings.core_port),
                    prompts=PromptsConfig(),
                    denoise=DenoiseConfig(enabled=self.options["denoise"]),
                    vad=VADConfig(
                        model_path=str(self.settings.vad_model_path),
                        threshold=self.options["vad"]["threshold"],
                        min_silence_duration=self.options["vad"]["min_silence_duration"],
                        min_speech_duration=self.options["vad"]["min_speech_duration"],
                    ),
                    speaker=SpeakerConfig(
                        enabled=self.options["speaker"] and source["identity"]["mode"] == "speaker",
                        embedding_model_path=str(self.settings.speaker_model_path),
                        db_path=str(
                            self.settings.speaker_db_root
                            / self.session_id
                            / (hashlib.sha256(source["id"].encode("utf-8")).hexdigest() + ".json")
                        ),
                    ),
                    microphone=mic_cfg,
                )
                identity = source["identity"]
                fixed_identity = None
                if not microphone and identity["mode"] == "fixed":
                    fixed_identity = (identity["id"], identity["name"])

                def make_speech_callback(event_name: str, source_id: str, runtime_ref: SourceRuntime):
                    async def callback(asr_scope=None):
                        if asr_scope and asr_scope not in self._scope_turns:
                            return
                        turn_id = self._scope_turns.get(asr_scope) if asr_scope else None
                        if event_name == "speech_start":
                            turn_id = turn_id or uuid.uuid4().hex
                            runtime_ref.active_turn_id = turn_id
                        else:
                            turn_id = turn_id or runtime_ref.active_turn_id or uuid.uuid4().hex
                            if runtime_ref.active_turn_id == turn_id:
                                runtime_ref.active_turn_id = None
                        self._emit(event_name, source_id, turn_id=turn_id)
                    return callback

                async def result_callback(speaker_id, speaker_name, voice_base64, result_id, text, asr_scope, *, _source_id=source["id"], _runtime=runtime):
                    if asr_scope and asr_scope not in self._scope_turns:
                        return
                    turn_id = self._scope_turns.pop(asr_scope, None) if asr_scope else None
                    turn_id = turn_id or _runtime.active_turn_id or uuid.uuid4().hex
                    if _runtime.active_turn_id == turn_id:
                        _runtime.active_turn_id = None
                    self._emit(
                        "speech_result",
                        _source_id,
                        turn_id=turn_id,
                        speaker_id=speaker_id,
                        speaker_name=speaker_name,
                        voice_base64=voice_base64,
                        precomputed_asr_result_id=result_id,
                        precomputed_asr_text=text,
                        asr_scope=asr_scope,
                    )

                runtime.pipeline = pipeline_factory(
                    config,
                    source_logger,
                    on_result_with_scope=result_callback,
                    on_speech_start_with_scope=(
                        make_speech_callback("speech_start", source["id"], runtime)
                        if not microphone else None
                    ),
                    on_speech_end_with_scope=(
                        make_speech_callback("speech_end", source["id"], runtime)
                        if not microphone else None
                    ),
                    on_mic_speech_start_with_scope=(
                        make_speech_callback("speech_start", source["id"], runtime)
                        if microphone else None
                    ),
                    on_mic_speech_end_with_scope=(
                        make_speech_callback("speech_end", source["id"], runtime)
                        if microphone else None
                    ),
                    stream_client=self.core_stream,
                    stream_platform=self.options["platform"],
                    stream_scope_factory=lambda key, source_id=source["id"]: self._asr_scope(source_id, key),
                    result_scope_factory=lambda key, source_id=source["id"]: self._asr_scope(source_id, key),
                    speaker_log=self.options["speaker_log"],
                    fixed_identity=fixed_identity,
                    interrupt_fences_segments=True,
                )
                runtime.pipeline.MAX_UTTERANCE_SECONDS = min(
                    self.options["max_duration_seconds"], self.settings.max_utterance_seconds
                )
                if not getattr(runtime.pipeline.vad, "available", False):
                    raise SessionUnavailableError("configured VAD capability is unavailable")
                if self.options["denoise"] and not getattr(runtime.pipeline.denoiser, "enabled", False):
                    raise SessionUnavailableError("requested denoise capability failed to initialize")
                if self.options["speaker"] and identity["mode"] == "speaker" and not getattr(runtime.pipeline.speaker_tracker, "enabled", False):
                    raise SessionUnavailableError("requested speaker identification failed to initialize")
                runtime.pipeline.set_loop(loop)
                if source["input"] != "external":
                    on_frame = runtime.pipeline.process_mic_frame if microphone else runtime.pipeline.process_frame
                    on_error = lambda reason, source_id=source["id"]: self._capture_failed(source_id, reason)
                    capture_factory = self.microphone_factory if microphone else self.capture_factory
                    capture_kwargs = {
                        "on_frame": on_frame,
                        "strict_device_selection": True,
                        "queue_size": self.settings.source_queue_size,
                        "on_error": on_error,
                    }
                    if microphone:
                        capture_kwargs["ptt_release_silence_duration"] = self.options["vad"]["min_silence_duration"]
                    runtime.capture = capture_factory(
                        mic_cfg if microphone else input_cfg,
                        self.logger,
                        **capture_kwargs,
                    )
                    await runtime.capture.start(loop)
        except BaseException:
            await self.close()
            raise

    @staticmethod
    def _check_model(path: Path, capability: str) -> None:
        if not path.is_file():
            raise SessionUnavailableError(f"configured {capability} model is unavailable")

    async def _capture_failed(self, source_id: str, reason: str):
        runtime = self.sources.get(source_id)
        if runtime is None or runtime.failed or self._closed:
            return
        runtime.failed = True
        runtime.last_error = reason
        if runtime.capture is not None:
            try:
                await runtime.capture.stop()
            except Exception:
                self.logger.exception("failed source capture cleanup failed")
        if runtime.pipeline is not None:
            await runtime.pipeline.interrupt()
        self._emit("error", source_id, code="capture_discontinuity", message=reason, turn_id=None)

    def _get_source(self, source_id: str) -> SourceRuntime:
        runtime = self.sources.get(source_id)
        if runtime is None:
            raise SessionRequestError("audio source is not declared for this session")
        if runtime.failed:
            raise SessionUnavailableError("audio source failed and must be recreated")
        return runtime

    def validate_sequence(self, source_id: str, op: str, sequence: Any, *, namespace: str | None = None) -> None:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise SessionRequestError("audio sequence number is invalid")
        self._get_source(source_id)
        key = (source_id, namespace or ("input" if op in {"send_pcm", "feed_vad"} else op))
        expected = self._audio_sequences.get(key, 0)
        if sequence != expected:
            raise SessionRequestError("audio sequence is duplicate or out of order")
        self._audio_sequences[key] = expected + 1

    async def process_audio(self, op: str, source_id: str, sequence: int, pcm: bytes) -> dict[str, Any]:
        runtime = self._get_source(source_id)
        async with runtime.input_lock:
            return await self._process_audio_locked(runtime, op, source_id, sequence, pcm)

    async def _process_audio_locked(
        self,
        runtime: SourceRuntime,
        op: str,
        source_id: str,
        sequence: int,
        pcm: bytes,
    ) -> dict[str, Any]:
        self.validate_sequence(source_id, op, sequence)
        if not pcm or len(pcm) > 65_536:
            raise SessionRequestError("audio frame is empty or exceeds 65536 bytes")
        if self.options["mode"] == "vad":
            return await self._process_vad(runtime, pcm)
        if op != "send_pcm":
            raise SessionRequestError("pipeline mode accepts send_pcm audio only")
        source = runtime.options
        if source["input"] != "external":
            raise SessionRequestError("PCM upload is not allowed for a captured source")
        sample_width = 2 if source["format"] == "s16le" else 4
        if len(pcm) % (sample_width * source["channels"]):
            raise SessionRequestError("PCM frame does not contain complete samples")
        samples = np.frombuffer(pcm, dtype="<i2" if sample_width == 2 else "<f4")
        if sample_width == 2:
            samples = samples.astype(np.float32) / 32768.0
        else:
            samples = np.nan_to_num(samples.astype(np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
        if len(samples) > source["sample_rate"] * source["channels"] * 2:
            raise SessionRequestError("PCM frame duration exceeds two seconds")
        method = runtime.pipeline.process_mic_frame if source["input"] == "microphone" else runtime.pipeline.process_frame
        await asyncio.to_thread(method, samples.tobytes(), source["sample_rate"], source["channels"])
        return {"accepted": True}

    async def _process_vad(self, runtime: SourceRuntime, pcm: bytes) -> dict[str, Any]:
        source = runtime.options
        if source["format"] != "s16le" or source["sample_rate"] != 16_000 or source["channels"] != 1:
            raise SessionRequestError("VAD input must be s16le 16000 Hz mono")
        if len(pcm) % 2:
            raise SessionRequestError("VAD input must contain complete 16-bit samples")
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if len(samples) > 16_000 * 2:
            raise SessionRequestError("VAD frame duration exceeds two seconds")
        previous_speaking = runtime.speaking
        segments = await asyncio.to_thread(runtime.vad.feed, samples)
        runtime.speaking = bool(runtime.vad.is_speaking)
        if runtime.speaking:
            if not previous_speaking:
                runtime.speech_bytes.clear()
            runtime.speech_bytes.extend(pcm)
            if len(runtime.speech_bytes) > int(self.options["max_duration_seconds"] * 16_000 * 2):
                runtime.speech_bytes.clear()
                runtime.speaking = False
                runtime.vad.reset()
                raise SessionRequestError("VAD utterance exceeded max_duration_seconds")
        completed_segments = []
        for segment in segments:
            segment_samples = np.asarray(segment.samples, dtype=np.float32).reshape(-1)
            if segment_samples.size > int(self.options["max_duration_seconds"] * 16_000):
                raise SessionRequestError("completed VAD segment exceeds max_duration_seconds")
            completed_segments.append(np.rint(np.clip(segment_samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())
        if sum(map(len, completed_segments)) > int(self.options["max_duration_seconds"] * 16_000 * 2):
            raise SessionRequestError("completed VAD segments exceed max_duration_seconds")
        if completed_segments or (previous_speaking and not runtime.speaking):
            runtime.speech_bytes.clear()
        # Send one representation only so a full 60-second result stays well
        # below the WebSocket event bound while retaining segment boundaries.
        result = {"completed_pcm": ""}
        if len(completed_segments) == 1:
            result["completed_pcm"] = base64.b64encode(completed_segments[0]).decode("ascii")
        elif len(completed_segments) > 1:
            result["completed_segments"] = [
                base64.b64encode(item).decode("ascii") for item in completed_segments
            ]
        return {"speaking": runtime.speaking, **result}

    async def encode_wav(self, source_id: str, pcm: bytes, max_duration_seconds: float) -> dict[str, str]:
        runtime = self._get_source(source_id)
        maximum = _number(max_duration_seconds, "max_duration_seconds", 1, min(60, self.options["max_duration_seconds"]))
        source = runtime.options
        if len(pcm) % 2 or len(pcm) > int(maximum * 16_000 * 2):
            raise SessionRequestError("WAV PCM must be bounded s16le mono 16kHz audio")
        if source["input"] != "external" or source["format"] != "s16le" or source["sample_rate"] != 16_000 or source["channels"] != 1:
            raise SessionRequestError("encode_wav requires an external s16le 16000 Hz mono source")
        wav_base64 = samples_to_wav_base64(
            np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0,
            sample_rate=16_000,
            max_duration_seconds=maximum,
        )
        if not wav_base64:
            raise SessionRequestError("WAV payload is empty")
        return {"wav_base64": wav_base64}

    async def play_voice(self, source_id: str, wav_bytes: bytes) -> dict[str, str]:
        self._get_source(source_id)
        if self.output is None:
            raise SessionUnavailableError("playback is disabled for this session")
        if not wav_bytes or len(wav_bytes) > 16 * 1024 * 1024:
            raise SessionRequestError("Core voice WAV is empty or too large")
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as audio:
                if audio.getnchannels() not in (1, 2) or audio.getsampwidth() != 2 or not 8_000 <= audio.getframerate() <= 192_000:
                    raise SessionRequestError("Core voice WAV format is unsupported")
                if audio.getnframes() > audio.getframerate() * 60:
                    raise SessionRequestError("Core voice WAV exceeds 60 seconds")
        except (wave.Error, EOFError) as exc:
            raise SessionRequestError("Core voice is not a valid WAV file") from exc
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as stream:
            stream.write(wav_bytes)
            path = stream.name
        await self.output.play(path)
        return {"status": "queued"}

    async def play_voice_stream(self, source_id: str, segment: dict[str, Any], pcm: bytes) -> dict[str, str]:
        self._get_source(source_id)
        if self.output is None:
            raise SessionUnavailableError("playback is disabled for this session")
        event = segment.get("event")
        if event == "start":
            metadata = dict(segment)
            stream_id = metadata.get("stream_id")
            if not isinstance(stream_id, str) or not stream_id:
                raise SessionRequestError("Core voice stream requires a stream_id")
            async with self._playback_lock:
                # AudioOutput owns one physical stream. Reserve the new
                # generation before awaiting start so an older draining end
                # cannot later retire this stream.
                self._playback_generation += 1
                generation = self._playback_generation
                self._stream_playback_metadata.clear()
                self._active_stream_source = source_id
                self._active_stream_id = stream_id
                try:
                    await self.output.start_voice_stream(metadata)
                except BaseException:
                    if generation == self._playback_generation:
                        self._stream_playback_metadata.clear()
                        self._active_stream_source = None
                        self._active_stream_id = None
                    raise
                if generation == self._playback_generation:
                    self._stream_playback_metadata[stream_id] = metadata
            return {"status": "queued"}

        stream_id = segment.get("stream_id")
        if not isinstance(stream_id, str):
            raise SessionRequestError("Core voice stream event has no active start")
        if event == "end":
            async with self._playback_lock:
                if (
                    stream_id not in self._stream_playback_metadata
                    or self._active_stream_source != source_id
                    or self._active_stream_id != stream_id
                ):
                    raise SessionRequestError("Core voice stream event has no active start")
                metadata = {**self._stream_playback_metadata[stream_id], **segment}
                generation = self._playback_generation
            try:
                # Drain outside the state lock so a replacement or interrupt
                # can fence it while the device finishes queued audio.
                await self.output.end_voice_stream(metadata)
            except Exception:
                async with self._playback_lock:
                    if generation == self._playback_generation and self._active_stream_id == stream_id:
                        self._stream_playback_metadata.pop(stream_id, None)
                        self._active_stream_source = None
                        self._active_stream_id = None
                raise
            async with self._playback_lock:
                still_current = (
                    generation == self._playback_generation
                    and self._active_stream_id == stream_id
                    and self._active_stream_source == source_id
                )
                if still_current:
                    self._stream_playback_metadata.pop(stream_id, None)
                    self._active_stream_source = None
                    self._active_stream_id = None
            return {"status": "render_drained" if still_current else "aborted"}

        async with self._playback_lock:
            if (
                stream_id not in self._stream_playback_metadata
                or self._active_stream_source != source_id
                or self._active_stream_id != stream_id
            ):
                raise SessionRequestError("Core voice stream event has no active start")
            metadata = {**self._stream_playback_metadata[stream_id], **segment}
            generation = self._playback_generation
            if event == "chunk":
                encoded = base64.b64encode(pcm).decode("ascii")
                try:
                    await self.output.write_voice_stream_chunk({**metadata, "audio_base64": encoded})
                except Exception:
                    if generation == self._playback_generation and self._active_stream_id == stream_id:
                        self._stream_playback_metadata.pop(stream_id, None)
                        self._active_stream_source = None
                        self._active_stream_id = None
                        try:
                            await self.output.abort_voice_stream(metadata)
                        except Exception:
                            pass
                    raise
                return {"status": "queued"}
            if event == "abort":
                # Invalidate any concurrent drain before asking the output to
                # discard the stream. The output operation stays serialized
                # with replacement starts.
                self._playback_generation += 1
                self._stream_playback_metadata.pop(stream_id, None)
                self._active_stream_source = None
                self._active_stream_id = None
                await self.output.abort_voice_stream(metadata)
                return {"status": "aborted"}
        raise SessionRequestError("Core voice stream event is unsupported")

    async def command(self, source_id: str, op: str, extra: dict[str, Any] | None = None, pcm: bytes = b"") -> dict[str, Any]:
        extra = extra or {}
        self._get_source(source_id)
        if op in {"send_pcm", "feed_vad"}:
            return await self.process_audio(op, source_id, extra.get("seq"), pcm)
        if op == "encode_wav":
            return await self.encode_wav(source_id, pcm, extra.get("max_duration_seconds", 60))
        if op == "play_voice":
            return await self.play_voice(source_id, pcm)
        if op == "play_voice_stream":
            segment = extra.get("segment")
            if not isinstance(segment, dict):
                raise SessionRequestError("voice stream metadata is missing")
            return await self.play_voice_stream(source_id, segment, pcm)
        if op == "pause_audio":
            if self.output is None:
                raise SessionUnavailableError("playback is disabled for this session")
            await self.output.stop_and_pause()
            self._paused = True
            return {"status": "paused"}
        if op == "resume_audio":
            if self.output is None:
                raise SessionUnavailableError("playback is disabled for this session")
            self.output.resume()
            self._paused = False
            return {"status": "resumed"}
        if op == "interrupt":
            await self.interrupt()
            return {"status": "interrupted"}
        raise SessionRequestError("unknown session operation")

    async def interrupt(self) -> None:
        async with self._playback_lock:
            self._playback_generation += 1
            self._stream_playback_metadata.clear()
            self._active_stream_source = None
            self._active_stream_id = None
            self._scope_turns.clear()
            if self.output is not None:
                interrupt = getattr(self.output, "interrupt", None)
                if interrupt is not None:
                    await interrupt()
                else:
                    await self.output.stop_current()
                    clear_buffer = getattr(self.output, "_discard_buffered_playback", None)
                    if clear_buffer is not None:
                        await clear_buffer()
            for runtime in self.sources.values():
                runtime.active_turn_id = None
                if runtime.pipeline is not None:
                    await runtime.pipeline.interrupt()
                if runtime.vad is not None:
                    async with runtime.input_lock:
                        runtime.vad.reset()
                        runtime.speaking = False
                        runtime.speech_bytes.clear()
                else:
                    runtime.speaking = False
                    runtime.speech_bytes.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        captures = [runtime.capture for runtime in self.sources.values() if runtime.capture is not None]
        for capture in captures:
            try:
                await capture.stop()
            except Exception:
                self.logger.exception("capture source cleanup failed")
        pipelines = [runtime.pipeline for runtime in self.sources.values() if runtime.pipeline is not None]
        for pipeline in pipelines:
            try:
                await pipeline.stop_streaming()
            except Exception:
                self.logger.exception("audio pipeline cleanup failed")
        if self.output is not None:
            try:
                await self.output.stop()
            except Exception:
                self.logger.exception("audio output cleanup failed")
        if self.core_stream is not None:
            try:
                await self.core_stream.close()
            except Exception:
                self.logger.exception("Core audio stream cleanup failed")
