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

from .config import AdapterConfig
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


MAX_REMOTE_AUDIO_BYTES = 4 * 1024 * 1024
MAX_REMOTE_AUDIO_BASE64_CHARS = ((MAX_REMOTE_AUDIO_BYTES + 2) // 3) * 4
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
        self._last_poke_time = 0.0
        self._started = False

    @property
    def is_running(self) -> bool:
        return bool(
            self._started
            and self.renderer is not None
            and self.render_thread is not None
            and self.render_thread.is_alive()
        )

    def set_interaction_sink(self, sink: InteractionSink | None) -> None:
        self._interaction_sink = sink

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
            model_adapter=self.model_adapter,
        )
        self.render_thread = threading.Thread(
            target=self._run_renderer,
            name="nachobot-live2d-renderer",
            daemon=True,
        )
        self._started = True
        self.render_thread.start()
        self.logger.info(
            "Live2D runtime started: model=%s window=%sx%s",
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
                    "adaptation": adaptation_report,
                },
            )
        )

    async def stop(self) -> None:
        if not self._started:
            return

        self._started = False
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

    async def dispatch(self, command: AvatarCommand) -> InteractionEvent | None:
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

        elif event is AvatarEvent.STOP_AUDIO:
            self._enqueue("stop_audio", None)

        else:
            raise ProtocolError(f"unsupported runtime event: {event.value}")

        return None

    def _run_renderer(self) -> None:
        assert self.renderer is not None
        try:
            self.renderer.run()
        except Exception:
            self.logger.exception("Live2D renderer thread crashed")
            self._emit_from_renderer_thread(
                InteractionEvent(
                    event=AvatarInteraction.ERROR,
                    payload={"message": "Live2D renderer thread crashed"},
                )
            )

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

        if button not in (6, 7):
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

    def _emit_from_renderer_thread(self, event: InteractionEvent) -> None:
        loop = self._event_loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._emit(event), loop)

    async def _emit(self, event: InteractionEvent) -> None:
        sink = self._interaction_sink
        if sink is None:
            self.logger.debug(
                "Live2D interaction dropped because no sink is connected: %s",
                event.event.value,
            )
            return
        try:
            await sink(event)
        except Exception:
            self.logger.exception(
                "Failed to emit Live2D interaction: %s", event.event.value
            )

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise ProtocolError(f"payload.{key} is required")
        return value
