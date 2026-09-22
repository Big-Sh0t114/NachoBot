"""Remote Live2D controller backed by the standalone adapter WebSocket API."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import websockets
from websockets.exceptions import ConnectionClosed


PROTOCOL_VERSION = "1.1"
COMMAND_MESSAGE_TYPE = "avatar.command"
INTERACTION_MESSAGE_TYPE = "avatar.interaction"

MAX_REMOTE_AUDIO_BYTES = 4 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 8.0


class Live2DRemoteError(RuntimeError):
    """A correlated Live2D request could not be completed."""


class Live2DCapabilityUnavailable(Live2DRemoteError):
    """The connected adapter does not advertise a requested capability."""


@dataclass(frozen=True, slots=True)
class PreparedReplyResult:
    """Normalized reply data returned to Bilibili without avatar semantics."""

    reply: str
    web_search: bool
    search_query: str
    control_id: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "web_search": self.web_search,
            "search_query": self.search_query,
            "control_id": self.control_id,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_payload()[key]


class RemoteLive2DController:
    """Lifecycle owner and transport facade for the extracted Live2D process."""

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
        self._ready = asyncio.Event()
        self._active_websocket: Any | None = None
        self._send_lock = asyncio.Lock()
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._capabilities: set[str] = set()
        self._degradation_logged: set[str] = set()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self._capabilities)

    def has_capability(self, capability: str) -> bool:
        return capability in self._capabilities

    async def start(self) -> None:
        if self.is_running:
            return
        self._loop = asyncio.get_running_loop()
        self.is_running = True
        self._runner_task = asyncio.create_task(
            self._connection_loop(),
            name="bilibili-live2d-remote-controller",
        )
        self.logger.info("Remote Live2D controller started")

    async def stop(self) -> None:
        self.is_running = False
        self._connected.clear()
        self._ready.clear()
        self._fail_pending(Live2DRemoteError("Live2D controller stopped"))
        task = self._runner_task
        self._runner_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._active_websocket = None
        self._capabilities.clear()
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
                "TTS audio segment is too large for Live2D playback ({} bytes); using local fallback",
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

    async def prepare_reply(self, raw_reply: str) -> PreparedReplyResult:
        """Prepare a reply through Live2D, with a deliberately narrow fallback."""

        if self.connected and not self._ready.is_set():
            try:
                await asyncio.wait_for(
                    self._ready.wait(),
                    timeout=min(REQUEST_TIMEOUT_SECONDS, 2.0),
                )
            except (asyncio.TimeoutError, Live2DRemoteError):
                pass
        if not self.has_capability("prepare_reply"):
            self._log_degradation_once("prepare_reply")
            return self._fallback_prepare_reply(raw_reply)

        try:
            payload = await self._request(
                "prepare_reply",
                {"reply": str(raw_reply or "")},
            )
            if not {"reply", "web_search", "search_query", "control_id"}.issubset(payload):
                raise Live2DRemoteError("Live2D prepare response is incomplete")
            return PreparedReplyResult(
                reply=str(payload.get("reply") or ""),
                web_search=bool(payload.get("web_search", False)),
                search_query=str(payload.get("search_query") or ""),
                control_id=str(payload.get("control_id") or "") or None,
            )
        except Exception as exc:
            self._log_degradation_once("prepare_reply", exc)
            return self._fallback_prepare_reply(raw_reply)

    async def apply_control(self, control_id: str | None) -> bool:
        """Apply one previously prepared control; never retries automatically."""

        if not control_id or not self.has_capability("apply_control"):
            if control_id:
                self._log_degradation_once("apply_control")
            return False
        try:
            payload = await self._request(
                "apply_control",
                {"control_id": str(control_id)},
            )
        except Exception as exc:
            self.logger.warning("Live2D control application unavailable: {}", type(exc).__name__)
            return False
        status = str(payload.get("status") or "")
        return status in {"applied", "already_applied"} or bool(
            payload.get("applied") or payload.get("already_applied")
        )

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

    async def _request(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_running:
            raise Live2DRemoteError("Live2D controller is not running")
        if not self.connected or not self._ready.is_set():
            raise Live2DCapabilityUnavailable("Live2D adapter is not ready")
        if event not in self._capabilities:
            raise Live2DCapabilityUnavailable(f"Live2D capability unavailable: {event}")

        websocket = self._active_websocket
        if websocket is None:
            raise Live2DCapabilityUnavailable("Live2D WebSocket is not active")
        request_id = uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        raw_message = json.dumps(
            {
                "type": COMMAND_MESSAGE_TYPE,
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "event": event,
                "payload": payload,
            },
            ensure_ascii=False,
        )

        # The future is registered before the serialized direct send so a fast
        # response cannot be lost.  Direct RPCs never enter the lossy queue.
        self._pending_requests[request_id] = future
        try:
            async with self._send_lock:
                if websocket is not self._active_websocket:
                    raise Live2DRemoteError("Live2D WebSocket changed before send")
                await websocket.send(raw_message)
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        finally:
            if not future.done():
                future.cancel()
            self._pending_requests.pop(request_id, None)

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
                    self._active_websocket = websocket
                    self._capabilities.clear()
                    self._ready.clear()
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
                    "Live2D adapter connection lost: {}; reconnecting in {:.1f}s",
                    type(exc).__name__,
                    self.reconnect_seconds,
                )
            finally:
                self._connected.clear()
                self._ready.clear()
                self._active_websocket = None
                self._capabilities.clear()
                self._fail_pending(Live2DRemoteError("Live2D connection closed"))

            if self.is_running:
                await asyncio.sleep(self.reconnect_seconds)

    async def _sender_loop(self, websocket: Any) -> None:
        while self.is_running:
            raw_message = await self._send_queue.get()
            try:
                async with self._send_lock:
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

        request_id = str(envelope.get("request_id") or "")
        if request_id:
            future = self._pending_requests.get(request_id)
            if future is not None and not future.done():
                if event == "error":
                    future.set_exception(
                        Live2DRemoteError(str(payload.get("message") or "remote error"))
                    )
                else:
                    future.set_result(payload)
                return

        if event == "ready":
            capabilities = payload.get("capabilities")
            if isinstance(capabilities, dict):
                self._capabilities = {
                    str(name)
                    for name, enabled in capabilities.items()
                    if enabled and str(name) in {"prepare_reply", "apply_control"}
                }
            else:
                advertised = payload.get("commands")
                self._capabilities = {
                    str(name)
                    for name in advertised or []
                    if str(name) in {"prepare_reply", "apply_control"}
                }
            self._ready.set()
            self.logger.info("Standalone Live2D renderer reported ready")
        elif event == "error":
            self.logger.error("Standalone Live2D adapter error: response_event={}", event)
        elif event == "poke":
            await self._handle_poke()
        elif event == "click":
            self.logger.debug("Live2D model clicked")

    async def _handle_poke(self) -> None:
        config = self.adapter.config
        room_id = getattr(config, "live_host_room_id", None)
        if room_id is None:
            self.logger.warning("Cannot route Live2D poke: host room is not configured")
            return

        user_id = str(getattr(config, "live_master_user_id", "1"))
        user_name = str(getattr(config, "live_master_user_name", "主人"))
        await self.adapter.handle_incoming_poke(int(room_id), user_id, user_name)

    def _fail_pending(self, exception: Exception) -> None:
        for future in tuple(self._pending_requests.values()):
            if not future.done():
                future.set_exception(exception)
        self._pending_requests.clear()

    def _log_degradation_once(self, operation: str, exception: Exception | None = None) -> None:
        if operation in self._degradation_logged:
            return
        self._degradation_logged.add(operation)
        suffix = f" ({type(exception).__name__})" if exception else ""
        self.logger.warning(
            "Live2D {} capability unavailable; using plain-text fallback{}",
            operation,
            suffix,
        )

    @staticmethod
    def _fallback_prepare_reply(raw_reply: str) -> PreparedReplyResult:
        """Limited compatibility fallback; never interprets control metadata."""

        text = str(raw_reply or "").strip()
        if not text:
            return PreparedReplyResult("", False, "", None)

        # A fenced block is always structured-output territory.  Only a JSON
        # object with a string reply is safe to expose; fenced arrays, scalars,
        # malformed JSON, and plain fenced prose must never leak through.
        fenced = re.fullmatch(
            r"\s*```(?:json)?\s*(.*?)\s*```\s*",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if fenced:
            candidate = fenced.group(1).strip()
            try:
                value = json.loads(candidate, strict=False)
            except (TypeError, json.JSONDecodeError):
                return PreparedReplyResult("", False, "", None)
            if not isinstance(value, dict):
                return PreparedReplyResult("", False, "", None)
            reply = value.get("reply")
            if not isinstance(reply, str):
                return PreparedReplyResult("", False, "", None)
            return PreparedReplyResult(reply.strip(), False, "", None)

        # Valid JSON scalars/arrays are structured output too, even when they
        # do not contain braces.  Ordinary prose remains untouched because it
        # is not valid JSON.
        try:
            parsed = json.loads(text, strict=False)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        else:
            if not isinstance(parsed, dict):
                return PreparedReplyResult("", False, "", None)

        if "{" not in text and "}" not in text:
            if text.startswith(("[", '"')):
                return PreparedReplyResult("", False, "", None)
            return PreparedReplyResult(text, False, "", None)

        # Decode an object from surrounding model prose without ever exposing
        # the raw structure.  Requiring exactly one identifiable object avoids
        # guessing when multiple JSON fragments are present.
        decoder = json.JSONDecoder(strict=False)
        objects: list[dict[str, Any]] = []
        search_from = 0
        while True:
            start = text.find("{", search_from)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(text[start:])
            except (TypeError, json.JSONDecodeError):
                search_from = start + 1
                continue
            if isinstance(value, dict):
                objects.append(value)
                # Skip nested objects belonging to this decoded object.
                search_from = start + end
            else:
                search_from = start + 1
        if len(objects) != 1:
            return PreparedReplyResult("", False, "", None)

        reply = objects[0].get("reply")
        if not isinstance(reply, str):
            return PreparedReplyResult("", False, "", None)
        return PreparedReplyResult(reply.strip(), False, "", None)

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


__all__ = [
    "Live2DCapabilityUnavailable",
    "Live2DRemoteError",
    "PreparedReplyResult",
    "RemoteLive2DController",
]
