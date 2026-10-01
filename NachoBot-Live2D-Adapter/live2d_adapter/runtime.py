"""Standalone Live2D renderer runtime.

This module has no dependency on Bilibili, NachoBot chat models, databases, or
LLM clients. It consumes canonical AvatarCommand objects and emits canonical
InteractionEvent objects.
"""

from __future__ import annotations

import asyncio
import base64
import queue
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .action_adapter import ActionAdapter
from .config import AdapterConfig
from .control_pipeline import ControlPipeline
from .model_adapter import Live2DModelAdapter
from .protocol import (
    AvatarCommand,
    AvatarEvent,
    AvatarInteraction,
    InteractionEvent,
    ProtocolError,
)
from .renderer import Live2DRenderer

InteractionSink = Callable[[InteractionEvent], Awaitable[None]]
ShutdownSink = Callable[[], Awaitable[None]]


MAX_REMOTE_AUDIO_BYTES = 4 * 1024 * 1024
MAX_REMOTE_AUDIO_BASE64_CHARS = ((MAX_REMOTE_AUDIO_BYTES + 2) // 3) * 4
MAX_REMOTE_PCM_CHUNK_BYTES = 64 * 1024
MAX_REMOTE_PCM_TOTAL_BYTES = 32 * 1024 * 1024
MAX_QUEUED_PCM_EVENTS = 64


class AvatarRuntime:
    """Own the renderer thread and translate protocol events into commands."""

    def __init__(self, config: AdapterConfig, logger):
        self.config = config
        self.logger = logger
        self.command_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.renderer: Live2DRenderer | None = None
        self.render_thread: threading.Thread | None = None
        self.model_adapter: Live2DModelAdapter | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._interaction_sink: InteractionSink | None = None
        self._shutdown_sink: ShutdownSink | None = None
        self._last_poke_time = 0.0
        self._started = False
        self.voice_stream_queue = queue.Queue(maxsize=MAX_QUEUED_PCM_EVENTS)
        self._voice_stream_id = None
        self._voice_stream_parent_id = None
        self._voice_stream_client_id = None
        self._voice_stream_format = None
        self._voice_stream_expected_seq = 0
        self._voice_stream_total_bytes = 0
        self._voice_stream_ended = False
        self._renderer_ready = threading.Event()
        self._renderer_start_error: Exception | None = None
        self.control_pipeline = ControlPipeline(
            action_adapter=ActionAdapter(logger),
        )

    @property
    def is_running(self) -> bool:
        return bool(
            self._started
            and self.renderer is not None
            and self.renderer.running
            and self.render_thread is not None
            and self.render_thread.is_alive()
        )

    def set_interaction_sink(self, sink: InteractionSink | None) -> None:
        self._interaction_sink = sink

    def set_shutdown_sink(self, sink: ShutdownSink | None) -> None:
        self._shutdown_sink = sink

    async def start(self) -> None:
        if self._started:
            return

        renderer_config = self.config.renderer
        if not renderer_config.model_path.is_file():
            raise FileNotFoundError(
                f"Live2D model file not found: {renderer_config.model_path}"
            )

        adaptation_config = self.config.adaptation
        self.model_adapter = Live2DModelAdapter.from_model_path(
            renderer_config.model_path,
            enabled=adaptation_config.enabled,
            parameter_mappings=adaptation_config.parameter_mappings,
            expression_mappings=adaptation_config.expression_mappings,
            action_mappings=self.config.action_mappings,
            logger=self.logger,
        )
        moc_path = self.model_adapter.metadata.moc_path
        if moc_path is None or not moc_path.is_file():
            raise FileNotFoundError(f"Live2D model .moc3 file not found: {moc_path}")

        for warning in self.model_adapter.metadata.warnings:
            self.logger.warning("[Live2D] model metadata warning: {}", warning)
        adaptation_report = self.model_adapter.describe()
        self.logger.info("Live2D automatic adaptation: {}", adaptation_report)

        self._event_loop = asyncio.get_running_loop()
        self._renderer_ready.clear()
        self._renderer_start_error = None
        self.renderer = Live2DRenderer(
            model_path=str(renderer_config.model_path),
            logger=self.logger,
            command_queue=self.command_queue,
            transparent=renderer_config.transparent,
            antialiasing=renderer_config.antialiasing,
            width=renderer_config.width,
            height=renderer_config.height,
            scale=renderer_config.scale,
            track_mouse=renderer_config.track_mouse,
            on_click=self._on_renderer_click,
            on_ready=self._on_renderer_ready,
            on_exit=self._on_renderer_exit,
            model_adapter=self.model_adapter,
            desktop_pet_config=self.config.desktop_pet,
            voice_stream_queue=self.voice_stream_queue,
        )
        self.render_thread = threading.Thread(
            target=self._run_renderer,
            name="nachobot-live2d-renderer",
            daemon=True,
        )
        self._started = True
        self.render_thread.start()
        renderer_ready = await asyncio.to_thread(self._renderer_ready.wait, 30.0)
        if not renderer_ready:
            self._started = False
            self.renderer.running = False
            raise RuntimeError("Live2D renderer did not become ready within 30 seconds")
        if self._renderer_start_error is not None:
            self._started = False
            error = self._renderer_start_error
            self.renderer = None
            self.render_thread = None
            raise RuntimeError(f"Live2D renderer failed during startup: {error}") from error
        self.logger.info(
            "Live2D runtime started: mode={} model={} window={}x{}",
            self.config.runtime.mode,
            renderer_config.model_path,
            renderer_config.width,
            renderer_config.height,
        )

        await self._emit(
            InteractionEvent(
                event=AvatarInteraction.READY,
                payload={
                    "model_path": str(renderer_config.model_path),
                    "width": renderer_config.width,
                    "height": renderer_config.height,
                    "mode": self.config.runtime.mode,
                    "adaptation": adaptation_report,
                    "protocol_version": "1.1",
                    "capabilities": {
                        "prepare_reply": True,
                        "apply_control": True,
                        "voice_stream_pcm": bool(self.renderer and self.renderer.pcm_audio_ready),
                        "commands": ["prepare_reply", "apply_control", "voice_stream"],
                        "interactions": ["reply_prepared", "control_applied"],
                    },
                },
            )
        )

    async def stop(self) -> None:
        if not self._started:
            self.control_pipeline.clear()
            return

        self._started = False
        self._reset_voice_stream_state(clear_queue=True)
        self.control_pipeline.clear()
        renderer = self.renderer
        render_thread = self.render_thread

        if renderer is not None:
            renderer.running = False

        if render_thread is not None and render_thread.is_alive():
            await asyncio.to_thread(render_thread.join, 5.0)
            if render_thread.is_alive():
                self.logger.warning(
                    "Live2D renderer thread did not stop within 5 seconds"
                )

        self.renderer = None
        self.render_thread = None
        self.logger.info("Live2D runtime stopped")

    async def dispatch(
        self,
        command: AvatarCommand,
        client_id: str = ControlPipeline.DEFAULT_CLIENT_ID,
    ) -> InteractionEvent | None:
        event = command.event
        payload = command.payload

        if event is AvatarEvent.PING:
            return InteractionEvent(
                event=AvatarInteraction.PONG,
                payload={"running": self.is_running},
                request_id=command.request_id,
            )

        if event is AvatarEvent.SHUTDOWN:
            await self.stop()
            return None

        if not self._started:
            raise ProtocolError("Live2D runtime is not started")

        if event is AvatarEvent.PREPARE_REPLY:
            raw_reply = payload.get("reply")
            if raw_reply is None:
                raw_reply = payload.get("text")
            if raw_reply is None:
                raw_reply = payload.get("raw_reply", "")
            prepared = self.control_pipeline.prepare_reply(
                raw_reply,
                command.request_id,
                client_id=client_id,
                question=str(payload.get("question") or ""),
                emotion=payload.get("emotion"),
                requested_action=payload.get("action"),
            )
            return InteractionEvent(
                event=AvatarInteraction.REPLY_PREPARED,
                payload=prepared.to_payload(),
                request_id=command.request_id,
            )

        if event is AvatarEvent.APPLY_CONTROL:
            control_id = self._required_string(payload, "control_id")
            outcome = self.control_pipeline.apply(
                control_id,
                client_id=client_id,
                apply_callback=self._apply_staged_control,
            )
            return InteractionEvent(
                event=AvatarInteraction.CONTROL_APPLIED,
                payload=outcome.to_payload(),
                request_id=command.request_id,
            )

        if event is AvatarEvent.STATE:
            self._enqueue("state", self._required_string(payload, "state"))

        elif event is AvatarEvent.SPEAKING:
            self._enqueue("speaking", bool(payload.get("speaking", False)))

        elif event is AvatarEvent.EMOTION:
            values = payload.get("values")
            if isinstance(values, dict):
                emotion: Any = dict(values)
            else:
                emotion = self._required_string(payload, "emotion")
            self._enqueue("emotion", emotion)

        elif event is AvatarEvent.ACTION:
            action_id = self._required_string(payload, "action_id").upper()
            motion_group = (
                self.model_adapter.resolve_action(action_id)
                if self.model_adapter is not None
                else self.config.resolve_action(action_id)
            )
            if not motion_group:
                raise ProtocolError(f"unmapped canonical action: {action_id}")
            self._enqueue("body_action", motion_group)

        elif event is AvatarEvent.MOTION:
            self._enqueue("motion", self._required_string(payload, "group"))

        elif event is AvatarEvent.RANDOM_MOTION:
            motion_group = self._required_string(payload, "group")
            priority = int(payload.get("priority", 3))
            self._enqueue(
                "random_motion",
                {"group": motion_group, "priority": priority},
            )

        elif event is AvatarEvent.GAZE:
            try:
                x = float(payload.get("x", 0.0))
                y = float(payload.get("y", 0.0))
            except (TypeError, ValueError) as exc:
                raise ProtocolError("gaze x and y must be numbers") from exc
            self._enqueue("auto_gaze", {"x": x, "y": y})

        elif event is AvatarEvent.PARAM_TWEEN:
            param = self._required_string(payload, "param")
            try:
                value = float(payload["value"])
                duration = float(payload.get("duration", 1.0))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProtocolError(
                    "param_tween requires numeric value and optional duration"
                ) from exc
            if duration <= 0:
                raise ProtocolError("param_tween duration must be positive")
            self._enqueue(
                "param_tween",
                {"param": param, "value": value, "duration": duration},
            )

        elif event is AvatarEvent.PLAY_AUDIO:
            self._enqueue("play_audio", self._decode_wav_payload(payload))

        elif event is AvatarEvent.QUEUE_AUDIO:
            self._enqueue(
                "queue_audio",
                {
                    "audio": self._decode_wav_payload(payload),
                    "reset": bool(payload.get("reset", False)),
                },
            )

        elif event is AvatarEvent.VOICE_STREAM:
            self._dispatch_voice_stream(payload, client_id)

        elif event is AvatarEvent.STOP_AUDIO:
            self._enqueue("stop_audio", None)

        else:
            raise ProtocolError(f"unsupported runtime event: {event.value}")

        return None

    def discard_client_controls(self, client_id: str) -> None:
        """Drop all staged state owned by a disconnected WebSocket client."""

        self.control_pipeline.discard_client(client_id)
        if self._voice_stream_client_id == client_id:
            if not self._voice_stream_ended:
                abort = self._voice_stream_event("abort")
                self._replace_queued_voice_events(abort)
            self._reset_voice_stream_state(clear_queue=False)

    def _apply_staged_control(self, staged: Any) -> None:
        """Resolve and enqueue a validated staged control atomically."""

        action_id = getattr(staged, "action_id", None)
        motion_group = None
        if action_id:
            motion_group = (
                self.model_adapter.resolve_action(action_id)
                if self.model_adapter is not None
                else self.config.resolve_action(action_id)
            )
            if not motion_group:
                raise ProtocolError(f"unmapped canonical action: {action_id}")

        emotion = getattr(staged, "emotion", None)
        if emotion:
            self._enqueue("emotion", emotion)
        if motion_group:
            self._enqueue("body_action", motion_group)

    def _run_renderer(self) -> None:
        assert self.renderer is not None
        try:
            self.renderer.run()
        except Exception as exc:
            self._renderer_start_error = exc
            self._renderer_ready.set()
            self.logger.exception("Live2D renderer thread crashed")
            self._emit_from_renderer_thread(
                InteractionEvent(
                    event=AvatarInteraction.ERROR,
                    payload={"message": "Live2D renderer thread crashed"},
                )
            )
            self._on_renderer_exit()
        finally:
            if not self._renderer_ready.is_set():
                self._renderer_start_error = RuntimeError(
                    "Live2D renderer exited before reporting ready"
                )
                self._renderer_ready.set()

    def _on_renderer_ready(self) -> None:
        self._renderer_ready.set()

    def _enqueue(self, command_type: str, content: Any) -> None:
        try:
            self.command_queue.put_nowait((command_type, content))
        except queue.Full as exc:
            raise RuntimeError("Live2D render command queue is full") from exc

    @staticmethod
    def _decode_wav_payload(payload: dict[str, Any]) -> bytes:
        if str(payload.get("format") or "").lower() != "wav":
            raise ProtocolError("play_audio only supports WAV payloads")

        encoded_audio = payload.get("audio_base64")
        if not isinstance(encoded_audio, str) or not encoded_audio:
            raise ProtocolError("play_audio requires a non-empty audio_base64 string")
        if len(encoded_audio) > MAX_REMOTE_AUDIO_BASE64_CHARS:
            raise ProtocolError("play_audio payload exceeds the 4 MiB limit")

        try:
            audio_data = base64.b64decode(encoded_audio, validate=True)
        except (ValueError, TypeError) as exc:
            raise ProtocolError("play_audio audio_base64 must be valid base64") from exc
        if not audio_data or len(audio_data) > MAX_REMOTE_AUDIO_BYTES:
            raise ProtocolError("play_audio payload exceeds the 4 MiB limit")
        return audio_data

    def _on_renderer_click(self, button: int) -> None:
        now = time.monotonic()
        self._emit_from_renderer_thread(
            InteractionEvent(
                event=AvatarInteraction.CLICK,
                payload={"button": int(button)},
            )
        )

        poke_buttons = (1, 6, 7) if self.config.desktop_pet.enabled else (6, 7)
        if button not in poke_buttons:
            return

        cooldown = self.config.renderer.poke_cooldown_seconds
        elapsed = now - self._last_poke_time
        if elapsed < cooldown:
            self.logger.debug(
                "Live2D poke ignored during cooldown: remaining=%.2fs",
                cooldown - elapsed,
            )
            return

        self._last_poke_time = now
        self._emit_from_renderer_thread(
            InteractionEvent(
                event=AvatarInteraction.POKE,
                payload={"button": int(button)},
            )
        )

    def _on_renderer_exit(self) -> None:
        sink = self._shutdown_sink
        loop = self._event_loop
        if not self._started or sink is None or loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(sink(), loop)

    def _emit_from_renderer_thread(self, event: InteractionEvent) -> None:
        loop = self._event_loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._emit(event), loop)

    async def _emit(self, event: InteractionEvent) -> None:
        sink = self._interaction_sink
        if sink is None:
            self.logger.debug(
                "Live2D interaction dropped because no sink is connected: {}",
                event.event.value,
            )
            return
        try:
            await sink(event)
        except Exception:
            self.logger.exception(
                "Failed to emit Live2D interaction: {}", event.event.value
            )

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise ProtocolError(f"payload.{key} is required")
        return value


    def _dispatch_voice_stream(
        self,
        payload: dict[str, Any],
        client_id: str,
    ) -> None:
        event = self._validate_voice_stream_payload(payload)
        stream_id = event["stream_id"]
        parent_id = event["parent_message_id"]
        stream_format = {
            key: event[key]
            for key in (
                "sample_rate",
                "channels",
                "sample_width",
                "codec",
            )
        }
        operation = event["event"]

        if operation == "start":
            if self.renderer is None or not self.renderer.pcm_audio_ready:
                raise ProtocolError("Live2D PCM output is not ready")
            # A new start is an explicit barge-in. Drop queued stale chunks;
            # the renderer's start handler stops the currently audible stream.
            self._reset_voice_stream_state(clear_queue=True)
            self._replace_queued_voice_events(event)
            self._voice_stream_id = stream_id
            self._voice_stream_parent_id = parent_id
            self._voice_stream_client_id = client_id
            self._voice_stream_format = stream_format
            self._voice_stream_expected_seq = 0
            self._voice_stream_total_bytes = 0
            self._voice_stream_ended = False
            return

        if (
            self._voice_stream_id != stream_id
            or self._voice_stream_parent_id != parent_id
            or self._voice_stream_client_id != client_id
            or self._voice_stream_format != stream_format
        ):
            raise ProtocolError("voice_stream event does not match the active stream")
        if self._voice_stream_ended:
            raise ProtocolError("voice_stream event arrived after end")

        if operation == "chunk":
            seq = event["seq"]
            pcm = event["pcm"]
            if seq != self._voice_stream_expected_seq:
                abort = self._voice_stream_event("abort")
                self._replace_queued_voice_events(abort)
                self._reset_voice_stream_state(clear_queue=False)
                raise ProtocolError("voice_stream chunk sequence is out of order")
            if self._voice_stream_total_bytes + len(pcm) > MAX_REMOTE_PCM_TOTAL_BYTES:
                abort = self._voice_stream_event("abort")
                self._replace_queued_voice_events(abort)
                self._reset_voice_stream_state(clear_queue=False)
                raise ProtocolError("voice_stream exceeded the utterance size limit")
            try:
                self.voice_stream_queue.put_nowait(event)
            except queue.Full as exc:
                abort = self._voice_stream_event("abort")
                self._replace_queued_voice_events(abort)
                self._reset_voice_stream_state(clear_queue=False)
                raise ProtocolError("voice_stream renderer queue is full") from exc
            self._voice_stream_expected_seq += 1
            self._voice_stream_total_bytes += len(pcm)
            return

        if operation == "end":
            # Preserve FIFO ordering so accepted chunks drain before end.
            try:
                self.voice_stream_queue.put_nowait(event)
            except queue.Full as exc:
                abort = self._voice_stream_event("abort")
                self._replace_queued_voice_events(abort)
                self._reset_voice_stream_state(clear_queue=False)
                raise ProtocolError("voice_stream renderer queue is full") from exc
            self._voice_stream_ended = True
            return

        self._replace_queued_voice_events(event)
        self._reset_voice_stream_state(clear_queue=False)


    def _replace_queued_voice_events(self, event: dict[str, Any]) -> None:
        while True:
            try:
                self.voice_stream_queue.get_nowait()
            except queue.Empty:
                break
        self.voice_stream_queue.put_nowait(event)


    def _reset_voice_stream_state(self, *, clear_queue: bool) -> None:
        if clear_queue:
            while True:
                try:
                    self.voice_stream_queue.get_nowait()
                except queue.Empty:
                    break
        self._voice_stream_id = None
        self._voice_stream_parent_id = None
        self._voice_stream_client_id = None
        self._voice_stream_format = None
        self._voice_stream_expected_seq = 0
        self._voice_stream_total_bytes = 0
        self._voice_stream_ended = False


    @staticmethod
    def _validate_voice_stream_payload(payload: dict[str, Any]) -> dict[str, Any]:
        event = payload.get("event")
        if not isinstance(event, str) or event not in ("start", "chunk", "end", "abort"):
            raise ProtocolError("voice_stream event must be start, chunk, end, or abort")
        stream_id = payload.get("stream_id")
        parent_id = payload.get("parent_message_id")
        if (
            not isinstance(stream_id, str)
            or not stream_id.strip()
            or len(stream_id) > 128
            or not isinstance(parent_id, str)
            or not parent_id.strip()
            or len(parent_id) > 256
        ):
            raise ProtocolError("voice_stream identity is invalid")
        sample_rate = payload.get("sample_rate")
        channels = payload.get("channels")
        sample_width = payload.get("sample_width")
        codec = payload.get("codec")
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or not 8000 <= sample_rate <= 96000
            or isinstance(channels, bool)
            or not isinstance(channels, int)
            or channels not in (1, 2)
            or isinstance(sample_width, bool)
            or not isinstance(sample_width, int)
            or sample_width != 2
            or codec != "pcm_s16le"
        ):
            raise ProtocolError("voice_stream PCM format is unsupported")
        result: dict[str, Any] = {
            "event": event,
            "stream_id": stream_id.strip(),
            "parent_message_id": parent_id.strip(),
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
            "codec": codec,
        }
        if event == "chunk":
            seq = payload.get("seq")
            encoded = payload.get("audio_base64")
            max_chars = ((MAX_REMOTE_PCM_CHUNK_BYTES + 2) // 3) * 4
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise ProtocolError("voice_stream chunk seq is invalid")
            if not isinstance(encoded, str) or not encoded or len(encoded) > max_chars:
                raise ProtocolError("voice_stream chunk is empty or exceeds the size limit")
            try:
                pcm = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise ProtocolError("voice_stream chunk is not valid base64") from exc
            if (
                not pcm
                or len(pcm) > MAX_REMOTE_PCM_CHUNK_BYTES
                or len(pcm) % (channels * sample_width)
            ):
                raise ProtocolError("voice_stream chunk does not contain PCM frames")
            result["seq"] = seq
            result["pcm"] = pcm
        return result


    def _voice_stream_event(self, event: str) -> dict[str, Any]:
        if self._voice_stream_id is None or self._voice_stream_format is None:
            raise ProtocolError("there is no active voice_stream")
        return {
            "event": event,
            "stream_id": self._voice_stream_id,
            "parent_message_id": self._voice_stream_parent_id,
            **self._voice_stream_format,
        }
