"""The eager local ASR/VLM runtime owned by the 9874 perception service.

9874 deliberately has no TTS model or TTS transport ownership.  The owning
process calls :meth:`preload` during its lifespan before it accepts requests;
the request handlers only dispatch to the already-loaded model functions.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import numpy as np


class UnsupportedOperation(RuntimeError):
    """The local perception runtime cannot serve a typed operation."""


class RuntimeUnavailable(UnsupportedOperation):
    """The local model process is disabled or not ready."""


class LocalBusy(RuntimeError):
    """The Florence worker cannot accept another bounded request."""


class AudioStreamConflict(RuntimeError):
    """A stream sequence or state transition conflicts with its current state."""


class AudioStreamInferenceError(RuntimeError):
    """The recognizer failed while consuming or finalizing a stream."""

    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(f"streaming ASR {stage} failed")


@dataclass(frozen=True)
class LocalCapabilities:
    """Public capability/readiness state for the perception-only listener."""

    operations: tuple[str, ...]
    ready: bool
    models: Mapping[str, str] = field(default_factory=dict)
    profile: str = "local"
    error: Optional[str] = None
    perception_enabled: bool = True
    perception_disabled: bool = False
    models_loaded: bool = False
    no_local_models: bool = False
    streaming_asr: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "operations": list(self.operations),
            "models": dict(self.models),
            "ready": self.ready,
            "profile": self.profile,
            "error": self.error,
            "perception_enabled": self.perception_enabled,
            "perception_disabled": self.perception_disabled,
            "models_loaded": self.models_loaded,
            "no_local_models": self.no_local_models,
            "streaming_asr": self.streaming_asr,
        }


@dataclass
class _AudioStreamSession:
    stream_id: str
    model: str
    last_activity: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    status: str = "starting"
    next_seq: int = 0
    samples_received: int = 0
    last_partial_text: str = ""
    final_text: str = ""
    chunk_digests: bytearray = field(default_factory=bytearray)


Loader = Callable[[], Any]


class LocalMultimodalRuntime:
    """Bounded dispatcher for eager local ASR and VLM models.

    ``asr_transcriber`` and ``image_captioner`` are test/deployment injection
    points.  Production instances leave them unset, so :meth:`preload`
    imports both model modules and calls their explicit ``load_model``
    functions before marking this runtime ready.
    """

    AUDIO = "audio.transcribe.v1"
    IMAGE = "image.describe.v1"
    VIDEO = "video.understand.v1"
    AUDIO_STREAM = "audio.stream.v1"
    AUDIO_MODEL_IDENTIFIER = "zh-xlarge-int8-2025-06-30"
    AUDIO_STREAM_SAMPLE_RATE = 16000
    AUDIO_STREAM_CHANNELS = 1
    AUDIO_STREAM_SAMPLE_WIDTH = 2
    AUDIO_STREAM_MAX_SECONDS = 60
    AUDIO_STREAM_MAX_CHUNK_BYTES = 64 * 1024
    AUDIO_STREAM_MAX_CHUNKS = 4096
    AUDIO_STREAM_MAX_CONCURRENT = 8
    AUDIO_STREAM_IDLE_SECONDS = 15
    AUDIO_STREAM_SWEEP_SECONDS = 1
    AUDIO_STREAM_MAX_TERMINAL = 64

    def __init__(
        self,
        *,
        config_dir: Path | str | None = None,
        no_local_models: bool | None = None,
        disable_perception: bool | None = None,
        perception_enabled: bool | None = None,
        asr_transcriber: Any = None,
        asr_streamer: Any = None,
        image_captioner: Any = None,
        asr_loader: Loader | None = None,
        vlm_loader: Loader | None = None,
    ):
        # Keep config_dir as a harmless compatibility value for callers that
        # construct the perception runtime alongside other adapter services.
        self.config_dir = (
            Path(config_dir)
            if config_dir
            else Path(__file__).resolve().parents[1] / "configs"
        )
        self.no_local_models = (
            bool(no_local_models)
            if no_local_models is not None
            else os.environ.get("NACHOBOT_NO_LOCAL_MODELS") == "1"
        )
        requested_perception = (
            bool(perception_enabled)
            if perception_enabled is not None
            else not (
                bool(disable_perception)
                if disable_perception is not None
                else os.environ.get("DISABLE_VLM_ASR") == "1"
            )
        )
        self.perception_enabled = not self.no_local_models and requested_perception
        self.disable_perception = not self.perception_enabled

        self._asr_transcriber = asr_transcriber
        self._asr_streamer = asr_streamer
        self._audio_model_identifier = self.AUDIO_MODEL_IDENTIFIER
        self._image_captioner = image_captioner
        self._asr_loader = asr_loader
        self._vlm_loader = vlm_loader
        self._preload_lock = asyncio.Lock()
        # The semaphore follows the worker, not the HTTP request. A cancelled
        # request cannot release Florence while its to_thread call still runs.
        self._vlm_slot = asyncio.Semaphore(1)
        self._vlm_admission = asyncio.Lock()
        self._vlm_pending = 0
        self._vlm_max_pending = 2  # one running and at most one waiting
        self._vlm_queue_timeout = 0.5
        self._audio_streams: OrderedDict[str, _AudioStreamSession] = OrderedDict()
        self._audio_stream_guard = asyncio.Lock()
        self._audio_stream_reaper: asyncio.Task[None] | None = None
        self._audio_streams_closing = False
        self._preload_started = False
        self._ready = False
        self._preload_error: str | None = (
            None if self.perception_enabled else "local perception is disabled"
        )

    @property
    def preload_error(self) -> str | None:
        """Return a bounded diagnostic for a failed or disabled preload."""

        return self._preload_error

    @property
    def models_loaded(self) -> bool:
        return self._ready

    async def _run_loader(self, loader: Loader) -> Any:
        value = await asyncio.to_thread(loader)
        if inspect.isawaitable(value):
            return await value
        return value

    async def _preload_asr(self) -> None:
        loader_ran = False
        loaded = None
        if self._asr_loader is not None:
            loaded = await self._run_loader(self._asr_loader)
            loader_ran = True
            if self._asr_streamer is None and self._is_streaming_asr(loaded):
                self._asr_streamer = loaded
            if self._asr_transcriber is None and callable(loaded):
                self._asr_transcriber = loaded
        if self._asr_transcriber is None:
            module = import_module(".asr.streaming", package=__package__)
            if not loader_ran and self._asr_streamer is None:
                loaded = await self._run_loader(getattr(module, "load_model"))
                self._asr_streamer = loaded
            if self._asr_transcriber is None:
                self._asr_transcriber = getattr(module, "transcribe")

        if self._asr_streamer is not None:
            self._audio_model_identifier = str(
                getattr(
                    self._asr_streamer,
                    "model_identifier",
                    self.AUDIO_MODEL_IDENTIFIER,
                )
                or ""
            )

        if not callable(self._asr_transcriber):
            raise RuntimeError("local ASR transcriber is unavailable after preload")

    @staticmethod
    def _is_streaming_asr(value: Any) -> bool:
        return value is not None and all(
            callable(getattr(value, name, None))
            for name in (
                "start_stream",
                "accept_stream_audio",
                "finish_stream",
                "abort_stream",
            )
        )

    @property
    def streaming_asr_available(self) -> bool:
        if (
            not self._ready
            or self._audio_streams_closing
            or not self._is_streaming_asr(self._asr_streamer)
        ):
            return False
        return bool(getattr(self._asr_streamer, "supports_streaming", True))

    async def _preload_vlm(self) -> None:
        loader_ran = False
        if self._vlm_loader is not None:
            loaded = await self._run_loader(self._vlm_loader)
            loader_ran = True
            if self._image_captioner is None and callable(loaded):
                self._image_captioner = loaded
        if self._image_captioner is None:
            module = import_module(".vlm.florence2", package=__package__)
            if not loader_ran:
                await self._run_loader(getattr(module, "load_model"))
            self._image_captioner = getattr(module, "caption_image_b64")

        if not callable(self._image_captioner):
            raise RuntimeError("local VLM captioner is unavailable after preload")

    async def preload(self) -> None:
        """Import and load ASR plus VLM before exposing readiness.

        A failure is latched.  The lifespan should propagate it so uvicorn
        never binds a falsely-ready listener; if a caller keeps a degraded
        listener alive, :meth:`perceive` still refuses to retry model loading.
        """

        async with self._preload_lock:
            if self._ready:
                return
            if self._preload_started:
                if self._preload_error:
                    raise RuntimeError(self._preload_error)
                raise RuntimeError("local perception preload did not complete")

            self._preload_started = True
            if not self.perception_enabled:
                self._preload_error = self._preload_error or "local perception is disabled"
                return

            try:
                # Sequential loading keeps memory pressure predictable while
                # still making both model loads part of startup readiness.
                await self._preload_asr()
                await self._preload_vlm()
            except Exception as exc:
                self._ready = False
                self._preload_error = f"local ASR/VLM preload failed: {type(exc).__name__}"
                raise RuntimeError("local ASR/VLM preload failed") from exc

            self._ready = True
            self._preload_error = None

    def capabilities(self) -> LocalCapabilities:
        operations = (self.AUDIO, self.IMAGE) if self.perception_enabled else ()
        return LocalCapabilities(
            operations=operations,
            ready=self._ready,
            models={
                self.AUDIO: self._audio_model_identifier,
                self.IMAGE: "Florence-2",
            } if self.perception_enabled else {},
            error=self._preload_error,
            perception_enabled=self.perception_enabled,
            perception_disabled=not self.perception_enabled,
            models_loaded=self._ready,
            no_local_models=self.no_local_models,
            streaming_asr=self.streaming_asr_available,
        )

    async def health(self) -> dict[str, Any]:
        return self.capabilities().to_dict()

    def _require_audio_streamer(self, model: str) -> Any:
        if not self.perception_enabled or not self._ready:
            detail = self._preload_error or "local perception is not ready"
            raise RuntimeUnavailable(detail)
        if self._audio_streams_closing:
            raise RuntimeUnavailable("local streaming ASR is shutting down")
        if not self.streaming_asr_available:
            raise UnsupportedOperation("local streaming ASR is unsupported")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("ASR model is required")
        if model != self._audio_model_identifier:
            raise UnsupportedOperation("selected ASR model is not loaded locally")
        return self._asr_streamer

    async def _run_asr_call(self, method: Callable[..., Any], *args: Any) -> tuple[Any, bool]:
        """Run one synchronous recognizer call and drain it if its client cancels."""

        worker = asyncio.create_task(asyncio.to_thread(method, *args))
        try:
            return await asyncio.shield(worker), False
        except asyncio.CancelledError:
            # Cancelling asyncio.to_thread does not stop its OS thread. Keep the
            # stream lock until the call settles so abort/shutdown cannot race it.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            return worker.result(), True

    def _trim_audio_stream_tombstones(self) -> None:
        terminal = [
            stream_id
            for stream_id, session in self._audio_streams.items()
            if session.status in {"finished", "aborted", "failed"}
        ]
        while len(terminal) > self.AUDIO_STREAM_MAX_TERMINAL:
            stream_id = terminal.pop(0)
            self._audio_streams.pop(stream_id, None)

    async def _complete_terminal_stream(
        self,
        session: _AudioStreamSession,
        status: str,
        *,
        abort_recognizer: bool,
    ) -> None:
        session.status = status
        session.last_activity = time.monotonic()
        session.last_partial_text = ""
        session.chunk_digests.clear()
        if abort_recognizer:
            try:
                await asyncio.to_thread(
                    self._asr_streamer.abort_stream,
                    session.stream_id,
                )
            except Exception:
                # The stream is already terminal at the wrapper boundary. A
                # best-effort abort must not replace the original failure.
                pass
        async with self._audio_stream_guard:
            if self._audio_streams.get(session.stream_id) is session:
                self._audio_streams.move_to_end(session.stream_id)
                self._trim_audio_stream_tombstones()

    async def _remember_terminal_stream(
        self,
        session: _AudioStreamSession,
        status: str,
        *,
        abort_recognizer: bool,
    ) -> bool:
        """Finish terminal bookkeeping even if the HTTP task is cancelled."""

        cleanup = asyncio.create_task(
            self._complete_terminal_stream(
                session,
                status,
                abort_recognizer=abort_recognizer,
            )
        )
        cancelled = False
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
        return cancelled

    async def start_audio_stream(
        self,
        *,
        model: str,
        sample_rate: int,
        channels: int,
    ) -> str:
        streamer = self._require_audio_streamer(model)
        if sample_rate != self.AUDIO_STREAM_SAMPLE_RATE:
            raise ValueError("stream sample_rate must be 16000")
        if channels != self.AUDIO_STREAM_CHANNELS:
            raise ValueError("stream channels must be 1")
        await self.start_audio_stream_reaper()

        now = time.monotonic()
        session = _AudioStreamSession(
            stream_id=uuid.uuid4().hex,
            model=model,
            last_activity=now,
        )
        async with self._audio_stream_guard:
            active_count = sum(
                current.status in {"starting", "active"}
                for current in self._audio_streams.values()
            )
            if self._audio_streams_closing:
                raise RuntimeUnavailable("local streaming ASR is shutting down")
            if active_count >= self.AUDIO_STREAM_MAX_CONCURRENT:
                raise LocalBusy("streaming ASR is busy")
            self._audio_streams[session.stream_id] = session

        try:
            async with session.lock:
                if self._audio_streams_closing:
                    raise RuntimeUnavailable("local streaming ASR is shutting down")
                await self._assert_current_audio_stream(session)
                if session.status != "starting":
                    raise RuntimeUnavailable("local streaming ASR is shutting down")
                started, cancelled = await self._run_asr_call(
                    streamer.start_stream,
                    session.stream_id,
                )
                if not started:
                    raise RuntimeUnavailable("preloaded streaming ASR is unavailable")
                session.status = "active"
                session.last_activity = time.monotonic()
                if cancelled:
                    await self._remember_terminal_stream(
                        session,
                        "aborted",
                        abort_recognizer=True,
                    )
                    raise asyncio.CancelledError
                return session.stream_id
        except asyncio.CancelledError:
            async with session.lock:
                if session.status in {"starting", "active"}:
                    await self._remember_terminal_stream(
                        session,
                        "aborted",
                        abort_recognizer=True,
                    )
            raise
        except RuntimeUnavailable:
            async with session.lock:
                if session.status in {"starting", "active"}:
                    was_cancelled = await self._remember_terminal_stream(
                        session,
                        "failed",
                        abort_recognizer=True,
                    )
                    if was_cancelled:
                        raise asyncio.CancelledError
            raise
        except Exception as exc:
            async with session.lock:
                if session.status in {"starting", "active"}:
                    was_cancelled = await self._remember_terminal_stream(
                        session,
                        "failed",
                        abort_recognizer=True,
                    )
                    if was_cancelled:
                        raise asyncio.CancelledError
            raise AudioStreamInferenceError("start") from exc

    async def _audio_stream_session(self, stream_id: str) -> _AudioStreamSession:
        if not isinstance(stream_id, str) or not stream_id:
            raise ValueError("stream_id is required")
        async with self._audio_stream_guard:
            session = self._audio_streams.get(stream_id)
        if session is None:
            raise KeyError("unknown or expired audio stream")
        return session

    async def _assert_current_audio_stream(self, session: _AudioStreamSession) -> None:
        async with self._audio_stream_guard:
            if self._audio_streams.get(session.stream_id) is not session:
                raise KeyError("unknown or expired audio stream")

    @classmethod
    def _validate_pcm_chunk(cls, pcm_bytes: bytes) -> None:
        if not isinstance(pcm_bytes, bytes) or not pcm_bytes:
            raise ValueError("PCM chunk must be non-empty bytes")
        if len(pcm_bytes) > cls.AUDIO_STREAM_MAX_CHUNK_BYTES:
            raise ValueError("PCM chunk exceeds 64KB")
        if len(pcm_bytes) % cls.AUDIO_STREAM_SAMPLE_WIDTH:
            raise ValueError("PCM chunk must contain complete s16le mono frames")

    async def accept_audio_stream_chunk(
        self,
        *,
        stream_id: str,
        seq: int,
        pcm_bytes: bytes,
    ) -> str:
        self._validate_pcm_chunk(pcm_bytes)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("seq must be a non-negative integer")
        session = await self._audio_stream_session(stream_id)
        async with session.lock:
            await self._assert_current_audio_stream(session)
            if session.status != "active":
                raise AudioStreamConflict(f"audio stream is {session.status}")
            chunk_digest = hashlib.blake2b(pcm_bytes, digest_size=16).digest()
            if seq < session.next_seq:
                offset = seq * len(chunk_digest)
                if session.chunk_digests[offset : offset + len(chunk_digest)] != chunk_digest:
                    raise AudioStreamConflict("duplicate sequence has different PCM data")
                session.last_activity = time.monotonic()
                return session.last_partial_text
            if seq > session.next_seq:
                raise AudioStreamConflict("audio stream sequence gap or out-of-order chunk")
            if session.next_seq >= self.AUDIO_STREAM_MAX_CHUNKS:
                was_cancelled = await self._remember_terminal_stream(
                    session,
                    "failed",
                    abort_recognizer=True,
                )
                if was_cancelled:
                    raise asyncio.CancelledError
                raise AudioStreamConflict("audio stream exceeds 4096 chunks")

            sample_count = len(pcm_bytes) // self.AUDIO_STREAM_SAMPLE_WIDTH
            max_samples = self.AUDIO_STREAM_SAMPLE_RATE * self.AUDIO_STREAM_MAX_SECONDS
            if session.samples_received + sample_count > max_samples:
                raise ValueError("audio stream exceeds 60 seconds")

            streamer = self._require_audio_streamer(session.model)
            session.last_activity = time.monotonic()
            samples = (
                np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
                / 32768.0
            )
            try:
                partial, cancelled = await self._run_asr_call(
                    streamer.accept_stream_audio,
                    session.stream_id,
                    samples,
                )
                if partial is None:
                    partial_text = ""
                elif isinstance(partial, str):
                    partial_text = partial.strip()[:10_000]
                else:
                    raise TypeError("streaming ASR returned an invalid partial result")
            except Exception as exc:
                was_cancelled = await self._remember_terminal_stream(
                    session,
                    "failed",
                    abort_recognizer=True,
                )
                if was_cancelled:
                    raise asyncio.CancelledError
                raise AudioStreamInferenceError("chunk") from exc

            session.samples_received += sample_count
            session.next_seq += 1
            session.chunk_digests.extend(chunk_digest)
            session.last_partial_text = partial_text
            session.last_activity = time.monotonic()
            if cancelled:
                # The accepted sequence is committed so an HTTP retry of this
                # exact chunk is idempotent and does not feed the decoder twice.
                raise asyncio.CancelledError
            return partial_text

    async def finish_audio_stream(self, stream_id: str) -> str:
        session = await self._audio_stream_session(stream_id)
        async with session.lock:
            await self._assert_current_audio_stream(session)
            if session.status == "finished":
                return session.final_text
            if session.status != "active":
                raise AudioStreamConflict(f"audio stream is {session.status}")

            streamer = self._require_audio_streamer(session.model)
            session.last_activity = time.monotonic()
            try:
                final, cancelled = await self._run_asr_call(
                    streamer.finish_stream,
                    session.stream_id,
                )
                if final is None:
                    final_text = ""
                elif isinstance(final, str):
                    final_text = final.strip()[:10_000]
                else:
                    raise TypeError("streaming ASR returned an invalid final result")
            except Exception as exc:
                was_cancelled = await self._remember_terminal_stream(
                    session,
                    "failed",
                    abort_recognizer=True,
                )
                if was_cancelled:
                    raise asyncio.CancelledError
                raise AudioStreamInferenceError("finalization") from exc

            session.final_text = final_text
            terminal_cancelled = await self._remember_terminal_stream(
                session,
                "finished",
                abort_recognizer=False,
            )
            if cancelled or terminal_cancelled:
                raise asyncio.CancelledError
            return final_text

    async def abort_audio_stream(self, stream_id: str) -> None:
        session = await self._audio_stream_session(stream_id)
        async with session.lock:
            await self._assert_current_audio_stream(session)
            if session.status == "aborted":
                return
            if session.status != "active":
                raise AudioStreamConflict(f"audio stream is {session.status}")
            cancelled = await self._remember_terminal_stream(
                session,
                "aborted",
                abort_recognizer=True,
            )
            if cancelled:
                raise asyncio.CancelledError

    async def expire_idle_audio_streams(self) -> None:
        now = time.monotonic()
        async with self._audio_stream_guard:
            candidates = tuple(self._audio_streams.values())
        for session in candidates:
            async with session.lock:
                if now - session.last_activity < self.AUDIO_STREAM_IDLE_SECONDS:
                    continue
                async with self._audio_stream_guard:
                    if (
                        self._audio_streams.get(session.stream_id) is not session
                        or now - session.last_activity < self.AUDIO_STREAM_IDLE_SECONDS
                    ):
                        continue
                    should_abort = session.status in {"starting", "active"}
                    session.status = "expired"
                    self._audio_streams.pop(session.stream_id, None)
                if should_abort:
                    try:
                        await self._run_asr_call(
                            self._asr_streamer.abort_stream,
                            session.stream_id,
                        )
                    except Exception:
                        pass

    async def _audio_stream_reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self.AUDIO_STREAM_SWEEP_SECONDS)
            await self.expire_idle_audio_streams()

    async def start_audio_stream_reaper(self) -> None:
        async with self._audio_stream_guard:
            if self._audio_streams_closing:
                raise RuntimeUnavailable("local streaming ASR is shutting down")
            if self._audio_stream_reaper is None or self._audio_stream_reaper.done():
                self._audio_stream_reaper = asyncio.create_task(
                    self._audio_stream_reaper_loop(),
                    name="local-audio-stream-reaper",
                )

    async def close_audio_streams(self) -> None:
        async with self._audio_stream_guard:
            self._audio_streams_closing = True
            reaper = self._audio_stream_reaper
            self._audio_stream_reaper = None
        if reaper is not None:
            reaper.cancel()
            try:
                await reaper
            except asyncio.CancelledError:
                pass

        async with self._audio_stream_guard:
            sessions = tuple(self._audio_streams.values())
        for session in sessions:
            async with session.lock:
                if session.status not in {"starting", "active"}:
                    continue
                session.status = "aborted"
                try:
                    await self._run_asr_call(
                        self._asr_streamer.abort_stream,
                        session.stream_id,
                    )
                except Exception:
                    pass
        async with self._audio_stream_guard:
            self._audio_streams.clear()

    async def perceive(
        self,
        operation: str,
        data: str,
        *,
        media_format: str = "",
        prompt: str = "",
    ) -> str:
        del media_format, prompt
        if not self.perception_enabled:
            if self.no_local_models:
                raise RuntimeUnavailable("local multimodal models are disabled")
            raise RuntimeUnavailable("local perception is disabled")
        if not self._ready:
            # Do not call preload here: request-time lazy loading is expressly
            # forbidden, and a failed startup must remain latched.
            detail = self._preload_error or "local perception is not ready"
            raise RuntimeUnavailable(detail)
        if not isinstance(data, str) or not data.strip():
            raise ValueError("media payload is empty")

        if operation == self.AUDIO:
            raw = _decode_bounded(data, 16 * 1024 * 1024)
            value = await asyncio.to_thread(self._asr_transcriber, raw)
            if inspect.isawaitable(value):
                value = await value
            return str(value or "").strip()
        if operation == self.IMAGE:
            # Validate the encoded payload before passing it to Florence's
            # service-owned caption policy.
            _decode_bounded(data, 16 * 1024 * 1024)
            value = await self._run_florence(data)
            return str(value or "").strip()
        if operation == self.VIDEO:
            raise UnsupportedOperation("local video understanding is unsupported")
        raise UnsupportedOperation(f"unsupported local operation: {operation}")

    async def _run_florence(self, data: str) -> Any:
        async with self._vlm_admission:
            if self._vlm_pending >= self._vlm_max_pending:
                raise LocalBusy("Florence worker is busy")
            self._vlm_pending += 1
        try:
            await asyncio.wait_for(self._vlm_slot.acquire(), timeout=self._vlm_queue_timeout)
        except asyncio.TimeoutError as exc:
            self._vlm_pending -= 1
            raise LocalBusy("Florence queue wait exceeded") from exc
        except BaseException:
            self._vlm_pending -= 1
            raise

        async def infer() -> Any:
            value = await asyncio.to_thread(self._image_captioner, data)
            if inspect.isawaitable(value):
                value = await value
            return value

        try:
            worker = asyncio.create_task(infer())
        except BaseException:
            self._vlm_slot.release()
            self._vlm_pending -= 1
            raise

        def release_capacity(completed: asyncio.Task[Any]) -> None:
            self._vlm_slot.release()
            self._vlm_pending -= 1
            if not completed.cancelled():
                completed.exception()  # consume errors after client cancellation

        try:
            return await asyncio.shield(worker)
        finally:
            if worker.done():
                release_capacity(worker)
            else:
                worker.add_done_callback(release_capacity)


def _decode_bounded(data: str, limit: int) -> bytes:
    raw = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    max_encoded_chars = 4 * ((limit + 2) // 3)
    if len(raw) > max_encoded_chars:
        raise ValueError(f"media payload exceeds {limit} bytes")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError("media payload is not valid base64") from exc
    if not decoded:
        raise ValueError("media payload is empty")
    if len(decoded) > limit:
        raise ValueError(f"media payload exceeds {limit} bytes")
    return decoded
