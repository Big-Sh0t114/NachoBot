"""Direct Core wire protocol for the desktop companion."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from websockets.sync.client import connect

from .chat import ChatReply, DesktopPetBackendError
from .config import DEFAULT_ACTION_MAPPINGS, DesktopChatConfig
from .voice import DesktopVoice


def incoming_message(text: str, message_id: str, model_group: str = "", desktop_context: dict | None = None) -> dict[str, Any]:
    platform = "local.live2d"
    return {
        "message_info": {
            "platform": platform,
            "message_id": message_id,
            "time": time.time(),
            "user_info": {"platform": platform, "user_id": "host", "user_nickname": "用户", "user_cardname": "用户"},
            "format_info": {"content_format": ["text"], "accept_format": ["structured_reply", "reply"]},
            "additional_config": {
                "source": "live2d-desktop",
                "conversation_id": "desktop-companion",
                **({"desktop_context": desktop_context} if desktop_context else {}),
                "runtime_capabilities": {
                    "schema_version": 1,
                    "planner_bypass": True,
                    "memory_retrieval_mode": "direct",
                    "prompt_profile": "desktop_companion",
                    "reply_delivery": "structured_reply",
                    "reply_controls": True,
                    "allow_silent_reply": False,
                    **({"reply_model_group": model_group} if model_group else {}),
                    "control_emotions": ["normal", "joy", "shy", "sorrow", "angry", "surprise"],
                    "control_actions": list(DEFAULT_ACTION_MAPPINGS),
                },
            },
        },
        "message_segment": {"type": "text", "data": text},
        "raw_message": text,
    }


def incoming_idle_event(message_id: str, idle_seconds: float, model_group: str = "", desktop_context: dict | None = None) -> dict[str, Any]:
    packet = incoming_message("", message_id, model_group, desktop_context)
    info = packet["message_info"]
    info.pop("user_info")
    info["additional_config"]["runtime_capabilities"]["allow_silent_reply"] = True
    info["additional_config"].update({
        "system_event": {"version": 1, "type": "desktop.idle", "actor": None, "target": None,
                         "data": {"idle_seconds": max(0, min(86400, int(idle_seconds)))}},
        "system_event_route": {"platform": "local.live2d", "kind": "private",
                               "peer": {"user_id": "host", "nickname": "用户"}},
    })
    return packet


_SILENT_REPLY = object()


def extract_reply(message: dict[str, Any], request_id: str) -> ChatReply | object | None:
    """Ignore expired, unattributed, and other users' replies instead of guessing."""
    if message.get("is_custom_message"):
        return None
    info = message.get("message_info") or {}
    if not isinstance(info, dict):
        return None
    additional = info.get("additional_config") or {}
    user = info.get("user_info") or {}
    if (
        not isinstance(additional, dict)
        or not isinstance(user, dict)
        or additional.get("reply_to_message_id") != request_id
        or str(user.get("user_id")) != "host"
    ):
        return None
    structured: list[dict[str, Any] | None] = []
    plain_text: list[str] = []
    has_non_text = False

    def visit(segment: Any) -> None:
        nonlocal has_non_text
        if not isinstance(segment, dict):
            has_non_text = True
            return
        if segment.get("type") == "seglist" and isinstance(segment.get("data"), list):
            for child in segment["data"]:
                visit(child)
        elif segment.get("type") == "structured_reply":
            data = segment.get("data")
            structured.append(data if isinstance(data, dict) else None)
        elif segment.get("type") == "text" and isinstance(segment.get("data"), str):
            plain_text.append(segment["data"])
        else:
            has_non_text = True

    root_segment = message.get("message_segment")
    if isinstance(root_segment, dict):
        visit(root_segment)
    else:
        has_non_text = True

    # A structured segment always owns this response, even if it is malformed,
    # correlated to another request, or accompanied by text siblings. Never
    # reinterpret those cases as legacy plain-text commands.
    for data in structured:
        if data is None:
            continue
        if data.get("request_id") != request_id:
            continue
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise DesktopPetBackendError("Core 桌宠消息协议版本不兼容，请同步更新 Core。")
        if data.get("status") == "error":
            raise DesktopPetBackendError("Core 回复格式解析失败，请重试。")
        if data.get("status") == "silent":
            if set(data) != {"schema_version", "request_id", "status"}:
                raise DesktopPetBackendError("Core 桌宠静默回复格式无效，请同步更新 Core。")
            return _SILENT_REPLY
        text = data.get("text")
        if data.get("status") != "ok" or not isinstance(text, str) or not text.strip():
            continue
        controls = data.get("controls") or {}
        if not isinstance(controls, dict):
            controls = {}
        emotion, action = controls.get("emotion"), controls.get("action")
        return ChatReply(text.strip()[:4000],
                         emotion=emotion if isinstance(emotion, str) and emotion in {"normal","joy","shy","sorrow","angry","surprise"} else None,
                         action=action if isinstance(action, str) and action in DEFAULT_ACTION_MAPPINGS else None)
    if structured or has_non_text or not plain_text:
        return None
    text = "".join(plain_text).strip()
    if not text:
        return None
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return ChatReply(text[:4000])
    return None


def _is_silent_reply_opted_in(packet: dict[str, Any]) -> bool:
    info = packet.get("message_info")
    additional = info.get("additional_config") if isinstance(info, dict) else None
    capabilities = additional.get("runtime_capabilities") if isinstance(additional, dict) else None
    event = additional.get("system_event") if isinstance(additional, dict) else None
    route = additional.get("system_event_route") if isinstance(additional, dict) else None
    if not isinstance(info, dict) or info.get("platform") != "local.live2d" or "user_info" in info:
        return False
    if not isinstance(capabilities, dict) or capabilities.get("allow_silent_reply") is not True:
        return False
    if (
        capabilities.get("prompt_profile") != "desktop_companion"
        or capabilities.get("reply_delivery") != "structured_reply"
        or capabilities.get("reply_controls") is not True
    ):
        return False
    if (
        not isinstance(event, dict)
        or type(event.get("version")) is not int
        or event.get("version") != 1
        or event.get("type") != "desktop.idle"
        or event.get("actor") is not None
        or event.get("target") is not None
    ):
        return False
    data = event.get("data")
    seconds = data.get("idle_seconds") if isinstance(data, dict) else None
    peer = route.get("peer") if isinstance(route, dict) else None
    return (
        type(seconds) is int
        and 0 <= seconds <= 86400
        and isinstance(route, dict)
        and route.get("platform") == "local.live2d"
        and route.get("kind") == "private"
        and isinstance(peer, dict)
        and peer.get("user_id") == "host"
    )


@dataclass(slots=True)
class _ReplyWaiter:
    event: threading.Event = field(default_factory=threading.Event)
    result: ChatReply | object | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _ManualRequest:
    request_id: str
    created_at: float
    eligible: bool = False
    first_delivered: bool = False
    handoff_ready: bool = False
    pending_overflow_logged: bool = False
    pending_followups: deque[str] = field(default_factory=deque)


class _CoreReplyTransport:
    """One lazy Core WebSocket with a single reader and bounded reply ledgers."""

    MAX_ACTIVE_WAITERS = 128
    MAX_MANUAL_RECORDS = 128
    MAX_PENDING_FOLLOWUPS_PER_REQUEST = 32
    MAX_OUTBOUND_IDS = 512
    MANUAL_RECORD_TTL_SECONDS = 3600.0
    OUTBOUND_ID_TTL_SECONDS = 3600.0
    RECONNECT_INITIAL_BACKOFF_SECONDS = 0.1
    RECONNECT_MAX_BACKOFF_SECONDS = 5.0

    def __init__(self, config: DesktopChatConfig, on_background_reply, logger) -> None:
        self.config = config
        self.on_background_reply = on_background_reply
        self.logger = logger
        self.headers = {"platform": "local.live2d"}
        auth = os.environ.get("NACHOBOT_CORE_TOKEN", "")
        if auth:
            self.headers["Authorization"] = auth
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._socket = None
        self._reader: threading.Thread | None = None
        self._maintenance: threading.Thread | None = None
        self._closed = False
        self._closing = threading.Event()
        self._active: OrderedDict[str, _ReplyWaiter] = OrderedDict()
        self._manual: OrderedDict[str, _ManualRequest] = OrderedDict()
        self._outbound_ids: OrderedDict[str, float] = OrderedDict()

    def submit(self, packet: dict[str, Any], request_id: str, *, manual: bool) -> _ReplyWaiter:
        payload = json.dumps(packet, ensure_ascii=False)
        waiter = _ReplyWaiter()
        failed_socket = None
        with self._lock:
            self._prune_locked()
            if self._closed or self._closing.is_set():
                raise DesktopPetBackendError("Core 桌面聊天通道已关闭。")
            if len(self._active) >= self.MAX_ACTIVE_WAITERS:
                raise DesktopPetBackendError("Core 桌面回复通道繁忙，请稍后重试。")
            if request_id in self._active or request_id in self._manual:
                raise DesktopPetBackendError("Core 桌面请求标识重复，请重试。")
            if manual:
                self._make_manual_room_locked()
                self._manual[request_id] = _ManualRequest(request_id, time.monotonic())
            self._active[request_id] = waiter

        try:
            # Connect and writes share one lock so simultaneous asks use one
            # connection and their request frames cannot interleave.
            with self._send_lock:
                with self._lock:
                    if self._closed or self._closing.is_set():
                        raise DesktopPetBackendError("Core 桌面聊天通道已关闭。")
                    if self._active.get(request_id) is not waiter:
                        self._waiter_outcome(waiter)
                        raise DesktopPetBackendError("Core 桌面请求已取消。")
                try:
                    socket = self._ensure_socket()
                except Exception as exc:
                    with self._lock:
                        error = DesktopPetBackendError(f"无法连接 Core 聊天通道：{exc}")
                        pending_ids = tuple(self._active)
                        self._settle_active_locked(error)
                        for pending_id in pending_ids:
                            record = self._manual.get(pending_id)
                            if record is not None and not record.eligible:
                                self._manual.pop(pending_id, None)
                    raise error from exc
                with self._lock:
                    if self._closed or self._closing.is_set():
                        raise DesktopPetBackendError("Core 桌面聊天通道已关闭。")
                    if self._active.get(request_id) is not waiter:
                        self._waiter_outcome(waiter)
                        raise DesktopPetBackendError("Core 桌面请求已取消。")
                if manual:
                    # Sending can fail after the peer accepted the full frame.
                    # Retain the anchor, but never retry or replay this packet.
                    with self._lock:
                        record = self._manual.get(request_id)
                        if record is not None:
                            record.eligible = True
                try:
                    socket.send(payload)
                except Exception as exc:
                    with self._lock:
                        failed_socket = self._disconnect_locked(
                            socket,
                            DesktopPetBackendError("Core 聊天通道已断开；请重试。"),
                        )
                    raise DesktopPetBackendError("Core 聊天通道已断开；请重试。") from exc
        except Exception:
            with self._lock:
                if self._active.get(request_id) is waiter:
                    self._active.pop(request_id, None)
                record = self._manual.get(request_id)
                if record is not None and not record.eligible:
                    self._manual.pop(request_id, None)
            if failed_socket is not None:
                self._close_socket(failed_socket)
            raise
        return waiter

    def wait(
        self,
        request_id: str,
        waiter: _ReplyWaiter,
        timeout_seconds: float,
        is_cancelled=None,
    ) -> ChatReply | object:
        deadline = time.monotonic() + timeout_seconds

        def check_cancelled() -> bool:
            if is_cancelled is None:
                return False
            try:
                return bool(is_cancelled())
            except Exception:
                self.cancel(request_id)
                raise

        while True:
            if check_cancelled():
                self.cancel(request_id)
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = self._finish_wait(request_id, waiter, timed_out=True)
                if outcome is not None:
                    return outcome
                raise TimeoutError
            if waiter.event.wait(min(remaining, 0.2 if is_cancelled is not None else remaining)):
                if check_cancelled():
                    self.cancel(request_id)
                    return None
                return self._waiter_outcome(waiter)

    def acknowledge(self, request_id: str) -> None:
        callbacks: list[str] = []
        with self._lock:
            self._prune_locked()
            record = self._manual.get(request_id)
            if record is None:
                return
            record.handoff_ready = True
            while record.pending_followups:
                callbacks.append(record.pending_followups.popleft())
        self._dispatch_callbacks(request_id, callbacks)

    def cancel(self, request_id: str) -> None:
        with self._lock:
            waiter = self._active.pop(request_id, None)
            if waiter is not None:
                waiter.error = DesktopPetBackendError("Core 桌面请求已取消。")
                waiter.event.set()
            self._manual.pop(request_id, None)

    def close(self) -> None:
        socket = None
        reader = None
        maintenance = None
        self._closing.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            socket = self._socket
            self._socket = None
            reader = self._reader
            self._reader = None
            maintenance = self._maintenance
            self._maintenance = None
            self._settle_active_locked(
                DesktopPetBackendError("Core 桌面聊天通道已关闭。")
            )
            self._manual.clear()
            self._outbound_ids.clear()
        if socket is not None:
            self._close_socket(socket)
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.75)
        if maintenance is not None and maintenance is not threading.current_thread():
            maintenance.join(timeout=0.75)

    def _ensure_socket(self):
        with self._lock:
            if self._socket is not None:
                return self._socket
            if self._closing.is_set() or self._closed:
                raise DesktopPetBackendError("Core 桌面聊天通道已关闭。")
        new_socket = connect(
            self.config.core_url,
            additional_headers=self.headers,
            open_timeout=self.config.request_timeout_seconds,
            close_timeout=0.5,
            max_size=1048576,
        )
        selected_socket = None
        close_new_socket = False
        start_error: Exception | None = None
        with self._lock:
            if self._closing.is_set() or self._closed:
                close_new_socket = True
            elif self._socket is not None:
                # The send lock normally prevents this. Keep the invariant if
                # a future caller adds another connection path.
                selected_socket = self._socket
                close_new_socket = True
            else:
                selected_socket = new_socket
                self._socket = new_socket
                reader = threading.Thread(
                    target=self._read_loop,
                    args=(new_socket,),
                    name="desktop-core-reply-reader",
                    daemon=True,
                )
                self._reader = reader
                try:
                    reader.start()
                except Exception as exc:
                    self._socket = None
                    self._reader = None
                    start_error = exc
                    close_new_socket = True
        if close_new_socket:
            self._close_socket(new_socket)
            if start_error is not None:
                raise start_error
            if self._closing.is_set() or self._closed:
                raise DesktopPetBackendError("Core 桌面聊天通道已关闭。")
        return selected_socket

    def _read_loop(self, socket) -> None:
        while not self._closing.is_set():
            try:
                raw_message = socket.recv(timeout=0.25)
            except TimeoutError:
                continue
            except Exception as exc:
                self._connection_failed(socket, exc)
                return
            try:
                message = json.loads(raw_message)
            except (ValueError, UnicodeError, TypeError):
                continue
            if isinstance(message, dict):
                self._route_message(message)

    def _route_message(self, message: dict[str, Any]) -> None:
        request_id = _response_request_id(message)
        if request_id is None:
            return
        try:
            reply = extract_reply(message, request_id)
            error = None
        except DesktopPetBackendError as exc:
            reply = None
            error = exc
        if reply is None and error is None:
            return
        callbacks: list[str] = []
        overflowed = False
        with self._lock:
            self._prune_locked()
            if self._closed or self._closing.is_set():
                return
            if request_id not in self._active and request_id not in self._manual:
                return
            message_id = _outbound_message_id(message)
            if message_id is not None and self._remember_outbound_id_locked(message_id):
                return
            waiter = self._active.pop(request_id, None)
            if waiter is not None:
                if error is not None:
                    waiter.error = error
                elif reply is _SILENT_REPLY:
                    # The client enforces whether a silent result was opted into.
                    waiter.result = _SILENT_REPLY
                else:
                    waiter.result = reply
                record = self._manual.get(request_id)
                if record is not None and reply is not _SILENT_REPLY and error is None:
                    record.first_delivered = True
                waiter.event.set()
                return
            record = self._manual.get(request_id)
            if record is None or not record.eligible or error is not None or reply is _SILENT_REPLY:
                return
            if not isinstance(reply, ChatReply):
                return
            if record.handoff_ready:
                callbacks.append(reply.text)
            elif len(record.pending_followups) < self.MAX_PENDING_FOLLOWUPS_PER_REQUEST:
                record.pending_followups.append(reply.text)
            elif not record.pending_overflow_logged:
                record.pending_overflow_logged = True
                overflowed = True
        if overflowed:
            try:
                self.logger.warning(
                    "Dropped excess pending Core follow-ups for request {}",
                    request_id,
                )
            except Exception:
                pass
        self._dispatch_callbacks(request_id, callbacks)

    def _finish_wait(
        self, request_id: str, waiter: _ReplyWaiter, *, timed_out: bool
    ) -> ChatReply | object | None:
        with self._lock:
            current = self._active.get(request_id)
            if current is not waiter:
                if waiter.event.is_set():
                    return self._waiter_outcome(waiter)
                return None
            if timed_out:
                self._active.pop(request_id, None)
                # A manual anchor remains eligible for validated late replies.
                record = self._manual.get(request_id)
                if record is not None and not record.eligible:
                    self._manual.pop(request_id, None)
                return None
            return None

    @staticmethod
    def _waiter_outcome(waiter: _ReplyWaiter) -> ChatReply | object:
        if waiter.error is not None:
            if isinstance(waiter.error, DesktopPetBackendError):
                raise waiter.error
            raise DesktopPetBackendError(f"Core 聊天通道失败：{waiter.error}") from waiter.error
        return waiter.result if waiter.result is not None else None

    def _connection_failed(self, socket, exc: BaseException) -> None:
        failed_socket = None
        with self._lock:
            failed_socket = self._disconnect_locked(
                socket,
                DesktopPetBackendError(f"Core 聊天通道已断开：{exc}"),
            )
        if failed_socket is not None:
            self._close_socket(failed_socket)

    def _disconnect_locked(self, socket, error: BaseException):
        if self._socket is not socket:
            return None
        self._socket = None
        self._reader = None
        self._settle_active_locked(error)
        self._schedule_maintenance_locked()
        return socket

    def _schedule_maintenance_locked(self) -> None:
        self._prune_locked()
        if (
            self._closed
            or self._closing.is_set()
            or not any(record.eligible for record in self._manual.values())
        ):
            return
        if self._maintenance is not None and self._maintenance.is_alive():
            return
        maintenance = threading.Thread(
            target=self._maintain_connection,
            name="desktop-core-reconnect",
            daemon=True,
        )
        self._maintenance = maintenance
        try:
            maintenance.start()
        except Exception:
            self._maintenance = None
            try:
                self.logger.exception("Could not start Core desktop reconnect maintenance")
            except Exception:
                pass

    def _maintain_connection(self) -> None:
        current_thread = threading.current_thread()
        backoff = self.RECONNECT_INITIAL_BACKOFF_SECONDS
        while not self._closing.is_set():
            with self._lock:
                self._prune_locked()
                if self._closed or not self._connection_is_authorized_locked():
                    self._finish_maintenance_locked(current_thread)
                    return
                if self._socket is not None:
                    self._finish_maintenance_locked(current_thread)
                    return
            if self._closing.wait(backoff):
                break
            with self._lock:
                self._prune_locked()
                unauthorized = not self._connection_is_authorized_locked()
                if self._closed or self._closing.is_set() or unauthorized:
                    self._finish_maintenance_locked(current_thread)
                    return
                if self._socket is not None:
                    self._finish_maintenance_locked(current_thread)
                    return
            try:
                with self._send_lock:
                    with self._lock:
                        if self._closed or self._closing.is_set():
                            self._finish_maintenance_locked(current_thread)
                            return
                        if self._socket is not None:
                            self._finish_maintenance_locked(current_thread)
                            return
                    self._ensure_socket()
            except Exception:
                if self._closing.is_set():
                    break
                with self._lock:
                    if self._active:
                        # Let a waiting request perform its own single send
                        # attempt rather than keeping it behind maintenance.
                        self._finish_maintenance_locked(current_thread)
                        return
                backoff = min(backoff * 2, self.RECONNECT_MAX_BACKOFF_SECONDS)
                continue

            socket = None
            reader = None
            retry = False
            with self._lock:
                self._prune_locked()
                if self._closed or self._closing.is_set():
                    self._finish_maintenance_locked(current_thread)
                    return
                if not self._connection_is_authorized_locked():
                    # The ledger may have expired during connect. Drop this
                    # otherwise-idle recovery socket instead of extending it.
                    socket, reader = self._detach_idle_connection_locked()
                    self._finish_maintenance_locked(current_thread)
                elif self._socket is not None:
                    self._finish_maintenance_locked(current_thread)
                    return
                else:
                    # A newly connected socket failed before the reader could
                    # report success. Keep this one worker and back off again.
                    retry = True
            if retry:
                backoff = min(backoff * 2, self.RECONNECT_MAX_BACKOFF_SECONDS)
                continue
            if socket is not None:
                self._close_socket(socket)
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=0.5)
            return
        with self._lock:
            self._finish_maintenance_locked(current_thread)

    def _connection_is_authorized_locked(self) -> bool:
        return bool(self._active) or any(record.eligible for record in self._manual.values())

    def _finish_maintenance_locked(self, maintenance: threading.Thread) -> None:
        if self._maintenance is maintenance:
            self._maintenance = None

    def _detach_idle_connection_locked(self):
        socket = self._socket
        reader = self._reader
        self._socket = None
        self._reader = None
        return socket, reader

    def _settle_active_locked(self, error: BaseException) -> None:
        active = tuple(self._active.values())
        self._active.clear()
        for waiter in active:
            waiter.error = error
            waiter.event.set()

    def _make_manual_room_locked(self) -> None:
        while len(self._manual) >= self.MAX_MANUAL_RECORDS:
            evictable = next(
                (request_id for request_id in self._manual if request_id not in self._active),
                None,
            )
            if evictable is None:
                raise DesktopPetBackendError(
                    "Core 桌面待回复记录已满，请关闭后重新打开桌宠。"
                )
            self._manual.pop(evictable, None)

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for request_id, record in tuple(self._manual.items()):
            expired = now - record.created_at > self.MANUAL_RECORD_TTL_SECONDS
            if request_id not in self._active and expired:
                self._manual.pop(request_id, None)
        for message_id, created_at in tuple(self._outbound_ids.items()):
            if now - created_at > self.OUTBOUND_ID_TTL_SECONDS:
                self._outbound_ids.pop(message_id, None)

    def _remember_outbound_id_locked(self, message_id: str) -> bool:
        if message_id in self._outbound_ids:
            return True
        self._outbound_ids[message_id] = time.monotonic()
        while len(self._outbound_ids) > self.MAX_OUTBOUND_IDS:
            self._outbound_ids.popitem(last=False)
        return False

    def _dispatch_callbacks(self, request_id: str, texts: list[str]) -> None:
        callback = self.on_background_reply
        if callback is None or self._closing.is_set():
            return
        for text in texts:
            if self._closing.is_set():
                return
            try:
                callback(request_id, text)
            except Exception:
                try:
                    self.logger.exception("Could not queue a late Core desktop reply")
                except Exception:
                    pass

    @staticmethod
    def _close_socket(socket) -> None:
        try:
            socket.close(timeout=0.5)
        except TypeError:
            try:
                socket.close()
            except Exception:
                pass
        except Exception:
            pass


def _response_request_id(message: dict[str, Any]) -> str | None:
    info = message.get("message_info")
    if not isinstance(info, dict) or message.get("is_custom_message"):
        return None
    user = info.get("user_info")
    additional = info.get("additional_config")
    if not isinstance(user, dict) or str(user.get("user_id")) != "host":
        return None
    if not isinstance(additional, dict):
        return None
    request_id = additional.get("reply_to_message_id")
    return request_id if isinstance(request_id, str) and request_id else None


def _outbound_message_id(message: dict[str, Any]) -> str | None:
    info = message.get("message_info")
    message_id = info.get("message_id") if isinstance(info, dict) else None
    return message_id if isinstance(message_id, str) and message_id else None


class CoreChatClient:
    def __init__(
        self,
        config: DesktopChatConfig,
        emit,
        logger,
        on_error,
        context_provider=None,
        on_background_reply=None,
    ) -> None:
        self.config = config
        self.context_provider = context_provider
        self.voice = DesktopVoice(config, emit, logger, on_error)
        self._request_context = threading.local()
        self._transport = _CoreReplyTransport(config, on_background_reply, logger)

    def ask(
        self, text: str, *, include_audio: bool = True, tts_language: str = "auto"
    ) -> ChatReply:
        request_id = uuid4().hex
        packet = incoming_message(text, request_id, self.config.core_reply_model_group,
                                  self.context_provider() if self.context_provider else None)
        return self._request(packet, include_audio=include_audio, tts_language=tts_language)

    def proactive(self, idle_seconds: float, *, include_audio: bool = True,
                  tts_language: str = "auto", is_cancelled=lambda: False) -> ChatReply | None:
        packet = incoming_idle_event(uuid4().hex, idle_seconds, self.config.core_reply_model_group,
                                     self.context_provider() if self.context_provider else None)
        return self._request(packet, include_audio=include_audio, tts_language=tts_language,
                             is_cancelled=is_cancelled)

    def _request(self, packet: dict, *, include_audio: bool, tts_language: str,
                 is_cancelled=None) -> ChatReply | None:
        token = self.voice.token()
        request_id = packet["message_info"]["message_id"]
        allow_silent = is_cancelled is not None and _is_silent_reply_opted_in(packet)
        if is_cancelled and is_cancelled():
            return None
        manual = is_cancelled is None
        if manual:
            self._request_context.request_id = request_id
        try:
            waiter = self._transport.submit(packet, request_id, manual=manual)
            reply = self._transport.wait(
                request_id,
                waiter,
                self.config.reply_timeout_seconds,
                is_cancelled=is_cancelled,
            )
        except DesktopPetBackendError:
            raise
        except TimeoutError as exc:
            raise DesktopPetBackendError("Core 回答超时，请稍后重试。") from exc
        except Exception as exc:
            raise DesktopPetBackendError(f"无法连接 Core 聊天通道：{exc}") from exc
        if is_cancelled and is_cancelled():
            self._transport.cancel(request_id)
            return None
        if reply is _SILENT_REPLY:
            if not allow_silent:
                raise DesktopPetBackendError("Core 在普通桌面消息中返回了静默回复。")
            return None
        if is_cancelled and is_cancelled():
            return None
        if not isinstance(reply, ChatReply):
            raise DesktopPetBackendError("Core 没有返回可识别的桌面回复。")
        pending = include_audio and self.voice.speak(reply.text, tts_language, token)
        return replace(reply, audio_pending=bool(pending))

    def announce(
        self, text: str, *, include_audio: bool = True, tts_language: str = "auto"
    ) -> ChatReply:
        pending = include_audio and self.voice.speak(text, tts_language, self.voice.token())
        return ChatReply(text, audio_pending=bool(pending))

    def set_voice_enabled(self, enabled: bool) -> None:
        self.voice.set_enabled(enabled)

    def get_tts_policy(self) -> dict[str, Any]:
        """Return the last policy observed from Core without doing network I/O."""
        return self.voice.get_tts_policy()

    def refresh_tts_policy(self, callback) -> bool:
        """Refresh Core-owned policy on the voice worker, never on the Tk thread."""
        return self.voice.refresh_tts_policy(callback)

    def acknowledge_current_request(self) -> None:
        """Release queued follow-ups after the caller has handed off its first result."""
        request_id = getattr(self._request_context, "request_id", None)
        if request_id is None:
            return
        self._request_context.request_id = None
        self._transport.acknowledge(request_id)

    def close(self) -> None:
        self._transport.close()
        self.voice.close()
