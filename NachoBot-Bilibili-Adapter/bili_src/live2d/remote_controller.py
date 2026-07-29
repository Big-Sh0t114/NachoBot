"""Remote Live2D controller backed by the standalone adapter WebSocket API."""

from __future__ import annotations

import asyncio
import base64
import json
from loguru import logger
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import websockets
from websockets.exceptions import ConnectionClosed

PROTOCOL_VERSION = "1.0"
COMMAND_MESSAGE_TYPE = "avatar.command"
INTERACTION_MESSAGE_TYPE = "avatar.interaction"


MAX_REMOTE_AUDIO_BYTES = 4 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE_BYTES = 8 * 1024 * 1024
class RemoteLive2DController:
    """Compatibility-oriented controller for the extracted Live2D process."""

    def __init__(self, adapter: Any, logger):
        self.adapter = adapter
        self.logger = logger
        self.url = str(
            getattr(adapter.config, "live_live2d_url", "ws://127.0.0.1:8766")
        ).strip()
        self.token = str(getattr(adapter.config, "live_live2d_token", "")).strip()
        self.reconnect_seconds = max(
            1.0,
            float(getattr(adapter.config, "live_live2d_reconnect_seconds", 3.0)),
        )

        self.current_mode = "idle"
        self.is_running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._send_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        if self.is_running:
            return
        self._loop = asyncio.get_running_loop()
        self.is_running = True
        self._runner_task = asyncio.create_task(
            self._connection_loop(),
            name="bilibili-live2d-remote-controller",
        )
        self.logger.info("Remote Live2D controller started: %s", self.url)

    async def stop(self) -> None:
        self.is_running = False
        self._connected.clear()
        task = self._runner_task
        self._runner_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.logger.info("Remote Live2D controller stopped")

    async def on_message_received(self, message: Any = None) -> None:
        del message
        await self.send_live2d_event("state", "start_viewing")

    async def on_reply_start(self) -> None:
        await self.send_live2d_event("state", "start_thinking")

    async def on_start_replying(self) -> None:
        self.current_mode = "busy"
        await self.send_live2d_event("state", "start_replying")

    async def on_reply_finished(self) -> None:
        self.current_mode = "idle"
        await self.send_live2d_event("state", "finish_reply")

    def set_speaking(self, speaking: bool) -> None:
        """Thread-safe synchronous callback used by AudioPlayer."""
        self._schedule(self.send_live2d_event("speaking", bool(speaking)))

    def notify_reply_finished(self) -> None:
        """Thread-safe synchronous wrapper for resetting avatar reply state."""
        self._schedule(self.on_reply_finished())

    async def play_audio(self, audio_data: bytes) -> bool:
        """Ask the standalone Live2D process to play a WAV audio segment."""
        if not self.connected:
            return False
        if len(audio_data) > MAX_REMOTE_AUDIO_BYTES:
            self.logger.warning(
                "TTS audio segment is too large for Live2D playback (%d bytes); using local fallback",
                len(audio_data),
            )
            return False

        await self.send_live2d_event("play_audio", audio_data)
        return True

    async def stop_audio(self) -> bool:
        """Stop renderer-owned audio when normal playback is interrupted."""
        if not self.connected:
            return False

        await self.send_live2d_event("stop_audio", None)
        return True

    async def send_live2d_event(self, event_type: str, content: Any) -> None:
        protocol_event, payload = self._translate_legacy_event(event_type, content)
        envelope = {
            "type": COMMAND_MESSAGE_TYPE,
            "version": PROTOCOL_VERSION,
            "request_id": uuid4().hex,
            "event": protocol_event,
            "payload": payload,
        }
        raw_message = json.dumps(envelope, ensure_ascii=False)

        try:
            self._send_queue.put_nowait(raw_message)
        except asyncio.QueueFull:
            try:
                self._send_queue.get_nowait()
                self._send_queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self._send_queue.put_nowait(raw_message)
            self.logger.warning("Live2D send queue full; discarded oldest command")

    async def send_canonical_action(self, action_id: str) -> None:
        await self.send_live2d_event(
            "action",
            {"action_id": str(action_id).strip().upper()},
        )

    def _schedule(self, coroutine: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            if hasattr(coroutine, "close"):
                coroutine.close()
            return

        def create_task() -> None:
            asyncio.create_task(coroutine)

        loop.call_soon_threadsafe(create_task)

    async def _connection_loop(self) -> None:
        while self.is_running:
            try:
                async with websockets.connect(
                    self._build_url(),
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
                ) as websocket:
                    self._connected.set()
                    self.logger.info("Connected to standalone Live2D adapter")
                    sender = asyncio.create_task(self._sender_loop(websocket))
                    receiver = asyncio.create_task(self._receiver_loop(websocket))
                    done, pending = await asyncio.wait(
                        {sender, receiver},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        if task.cancelled():
                            continue
                        exception = task.exception()
                        if exception is not None:
                            raise exception
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "Live2D adapter connection lost: %s; reconnecting in %.1fs",
                    exc,
                    self.reconnect_seconds,
                )
            finally:
                self._connected.clear()

            if self.is_running:
                await asyncio.sleep(self.reconnect_seconds)

    async def _sender_loop(self, websocket: Any) -> None:
        while self.is_running:
            raw_message = await self._send_queue.get()
            try:
                await websocket.send(raw_message)
            except Exception:
                try:
                    self._send_queue.put_nowait(raw_message)
                except asyncio.QueueFull:
                    self.logger.warning("Could not requeue unsent Live2D command")
                raise
            finally:
                self._send_queue.task_done()

    async def _receiver_loop(self, websocket: Any) -> None:
        try:
            async for raw_message in websocket:
                if not isinstance(raw_message, str):
                    continue
                await self._handle_interaction(raw_message)
        except ConnectionClosed:
            return

    async def _handle_interaction(self, raw_message: str) -> None:
        try:
            envelope = json.loads(raw_message)
        except json.JSONDecodeError:
            self.logger.warning("Ignored malformed Live2D adapter response")
            return

        if not isinstance(envelope, dict):
            return
        if envelope.get("type") != INTERACTION_MESSAGE_TYPE:
            return

        event = str(envelope.get("event") or "")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        if event == "ready":
            self.logger.info("Standalone Live2D renderer reported ready")
        elif event == "error":
            self.logger.error(
                "Standalone Live2D adapter error: %s",
                payload.get("message", "unknown error"),
            )
        elif event == "poke":
            await self._handle_poke()
        elif event == "click":
            self.logger.debug("Live2D model clicked: %s", payload)

    async def _handle_poke(self) -> None:
        config = self.adapter.config
        room_id = getattr(config, "live_host_room_id", None)
        if room_id is None:
            self.logger.warning("Cannot route Live2D poke: host room is not configured")
            return

        user_id = str(getattr(config, "live_master_user_id", "1"))
        user_name = str(getattr(config, "live_master_user_name", "主人"))
        await self.adapter.handle_incoming_poke(int(room_id), user_id, user_name)

    def _build_url(self) -> str:
        if not self.token:
            return self.url
        parts = urlsplit(self.url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["token"] = self.token
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    @staticmethod
    def _translate_legacy_event(event_type: str, content: Any) -> tuple[str, dict[str, Any]]:
        if event_type == "state":
            return "state", {"state": str(content)}
        if event_type == "speaking":
            return "speaking", {"speaking": bool(content)}
        if event_type == "emotion":
            if isinstance(content, dict):
                return "emotion", {"values": content}
            return "emotion", {"emotion": str(content)}
        if event_type == "action":
            if isinstance(content, dict):
                action_id = content.get("action_id", "")
            else:
                action_id = content
            return "action", {"action_id": str(action_id).strip().upper()}
        if event_type == "random_motion":
            data = content if isinstance(content, dict) else {"group": str(content)}
            return "random_motion", {
                "group": str(data.get("group", "Idle")),
                "priority": int(data.get("priority", 3)),
            }
        if event_type in {"body_action", "motion"}:
            return "motion", {"group": str(content)}
        if event_type in {"auto_gaze", "gaze"}:
            data = content if isinstance(content, dict) else {}
            return "gaze", {
                "x": float(data.get("x", 0.0)),
                "y": float(data.get("y", 0.0)),
            }
        if event_type == "param_tween":
            data = content if isinstance(content, dict) else {}
            return "param_tween", {
                "param": str(data.get("param", "")),
                "value": float(data.get("value", 0.0)),
                "duration": float(data.get("duration", 1.0)),
            }
        if event_type == "play_audio":
            if not isinstance(content, bytes):
                raise ValueError("Live2D audio payload must be bytes")
            return "play_audio", {
                "format": "wav",
                "audio_base64": base64.b64encode(content).decode("ascii"),
            }
        if event_type == "stop_audio":
            return "stop_audio", {}
        raise ValueError(f"Unsupported Live2D event type: {event_type}")
