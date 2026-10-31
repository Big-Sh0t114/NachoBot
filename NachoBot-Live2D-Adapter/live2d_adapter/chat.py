"""Hover-revealed desktop conversation UI and local NachoBot integration."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import random
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from http.client import HTTPResponse
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from .config import DesktopChatConfig
from .desktop_pet import clamp_window_position
from .ui_theme import PALETTE, ScrollThumb, popup_position, popup_shell, rounded_surface, px, scale_widgets, ui_scale

RendererCommand = Callable[[str, Any], None]

DOCK_WIDTH = 400
DOCK_HEIGHT = 60
HISTORY_WIDTH = 460
HISTORY_HEIGHT = 520
TOOLBAR_BUTTON_WIDTH = 44
TOOLBAR_BUTTON_HEIGHT = 44
TOOLBAR_BUTTON_GAP = 14
TOOLBAR_PET_GAP = 8
TOOLBAR_COLOR_KEY = "#ff00ff"
DOCK_GAP = 6
HOVER_HIDE_DELAY_SECONDS = 0.5
TOOLBAR_SLIDE_SECONDS = 0.18
INPUT_ACTIVITY_GRACE_SECONDS = 1.0
PROACTIVE_CHECK_SECONDS = 30.0
PROACTIVE_CHANCE = 0.25
_WH_CBT = 5
_HCBT_ACTIVATE = 5


def _set_ctypes_signature(function: Any, restype: Any, argtypes: list[Any]) -> None:
    """Set a Win32 function signature while allowing lightweight fake APIs in tests."""

    try:
        function.argtypes = argtypes
        function.restype = restype
    except (AttributeError, TypeError):
        pass


def _retain_failed_activation_hook(hook_handle: Any, callback_ref: Any) -> None:
    """Keep a failed-to-unhook callback alive for its owning thread's lifetime."""

    thread = threading.current_thread()
    retained = getattr(thread, "_nachobot_live2d_cbt_hooks", None)
    if retained is None:
        retained = []
        thread._nachobot_live2d_cbt_hooks = retained
    retained.append((hook_handle, callback_ref))


@contextmanager
def _suppress_thread_window_activation() -> Iterator[None]:
    """Block this UI thread's window activation while Tk creates and hides its root."""

    if sys.platform != "win32":
        yield
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer_sized_result = ctypes.c_ssize_t
    pointer_sized_unsigned = ctypes.c_size_t
    callback_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    hook_proc_type = callback_factory(
        pointer_sized_result,
        ctypes.c_int,
        pointer_sized_unsigned,
        pointer_sized_result,
    )
    _set_ctypes_signature(kernel32.GetCurrentThreadId, ctypes.c_uint32, [])
    _set_ctypes_signature(
        user32.SetWindowsHookExW,
        ctypes.c_void_p,
        [ctypes.c_int, hook_proc_type, ctypes.c_void_p, ctypes.c_uint32],
    )
    _set_ctypes_signature(
        user32.CallNextHookEx,
        pointer_sized_result,
        [
            ctypes.c_void_p,
            ctypes.c_int,
            pointer_sized_unsigned,
            pointer_sized_result,
        ],
    )
    _set_ctypes_signature(user32.UnhookWindowsHookEx, ctypes.c_int, [ctypes.c_void_p])

    hook_handle: Any = None

    def hook_callback(code: int, wparam: int, lparam: int) -> int:
        if code == _HCBT_ACTIVATE:
            return 1
        return int(user32.CallNextHookEx(hook_handle, code, wparam, lparam))

    # Keep the callback object alive through UnhookWindowsHookEx. A NULL module
    # handle and the current nonzero thread ID scope the hook to this UI thread.
    callback_ref = hook_proc_type(hook_callback)
    thread_id = int(kernel32.GetCurrentThreadId())
    if thread_id == 0:
        raise OSError("GetCurrentThreadId returned 0; refusing a process-wide CBT hook")
    hook_handle = user32.SetWindowsHookExW(_WH_CBT, callback_ref, None, thread_id)
    if not hook_handle:
        error = getattr(ctypes, "get_last_error", lambda: 0)()
        raise OSError(error, "SetWindowsHookExW(WH_CBT) failed")

    try:
        yield
    finally:
        try:
            unhooked = bool(user32.UnhookWindowsHookEx(hook_handle))
        except BaseException:
            _retain_failed_activation_hook(hook_handle, callback_ref)
            raise
        if not unhooked:
            # A failed unhook can still leave this callback registered. Retain
            # it on the thread object until Windows has torn down that thread.
            _retain_failed_activation_hook(hook_handle, callback_ref)
            error = getattr(ctypes, "get_last_error", lambda: 0)()
            raise OSError(error, "UnhookWindowsHookEx failed")
        # Retain an explicit reference until the hook has been removed.
        _ = callback_ref


class PetChatHoverState:
    """Pure hover/session visibility policy for the grouped desktop chat UI."""

    def __init__(self, grace_seconds: float = HOVER_HIDE_DELAY_SECONDS) -> None:
        self.grace_seconds = max(0.0, float(grace_seconds))
        self.visible = False
        self.suppressed_until_pet_leave = False
        self._hide_at: float | None = None

    def hide_until_pet_leave(self) -> None:
        self.suppressed_until_pet_leave = True
        self.visible = False
        self._hide_at = None

    def update(
        self,
        *,
        now: float,
        pet_available: bool,
        pet_hovered: bool,
        chat_hovered: bool = False,
        interaction_active: bool = False,
        open_from_pet: bool = False,
        open_allowed: bool = False,
    ) -> bool:
        if not pet_hovered:
            self.suppressed_until_pet_leave = False

        if self.suppressed_until_pet_leave:
            self._hide_at = None
            self.visible = False
            return False

        if open_from_pet and pet_available and pet_hovered:
            self.visible = True
            self._hide_at = None
            return True
        if open_allowed and pet_available and (pet_hovered or chat_hovered or interaction_active):
            self.visible = True
            self._hide_at = None
            return True

        should_retain = (
            pet_available and pet_hovered
        ) or chat_hovered or interaction_active
        if should_retain:
            self.visible = True
            self._hide_at = None
            return True

        if not self.visible:
            self._hide_at = None
            return False
        if self._hide_at is None:
            self._hide_at = float(now) + self.grace_seconds
        if float(now) >= self._hide_at:
            self.visible = False
            self._hide_at = None
        return self.visible


_CHAT_COLORS = PALETTE


def _rounded_rectangle(
    canvas: Any,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **options: Any,
) -> int:
    """Draw a smooth rounded polygon on a Tk Canvas and return its item id."""

    radius = max(0.0, min(float(radius), (x2 - x1) / 2, (y2 - y1) / 2))
    points = (
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    )
    return int(canvas.create_polygon(*points, smooth=True, splinesteps=16, **options))
def docked_window_position(
    pet_rect: tuple[int, int, int, int],
    work_area: tuple[int, int, int, int],
    window_size: tuple[int, int],
    gap: int = DOCK_GAP,
) -> tuple[int, int]:
    """Center the chat dock below the pet and keep it on the active monitor."""

    pet_left, pet_top, pet_right, pet_bottom = pet_rect
    work_left, work_top, work_right, work_bottom = work_area
    width, height = window_size
    x = pet_left + (pet_right - pet_left - width) // 2
    x = max(work_left, min(work_right - width, x))
    y = pet_bottom + max(0, int(gap))
    if y + height > work_bottom:
        # If there is no room below the pet, place the dock fully above it.
        # Using ``pet_bottom - height`` would overlap the lower part of a tall
        # desktop pet and make the visible character impossible to double-click.
        y = max(work_top, min(work_bottom - height, pet_top - height - gap))
    return int(x), int(y)


def composer_below_pet_position(
    pet_rect: tuple[int, int, int, int],
    work_area: tuple[int, int, int, int],
    window_size: tuple[int, int],
    gap: int = DOCK_GAP,
) -> tuple[int, int]:
    """Center a floating composer below the pet, clamping it at the screen edge."""

    pet_left, pet_top, pet_right, pet_bottom = pet_rect
    work_left, work_top, work_right, work_bottom = work_area
    width, height = window_size
    x = pet_left + (pet_right - pet_left - width) // 2
    x = max(work_left, min(work_right - width, x))
    y = pet_bottom + max(0, int(gap))
    y = max(work_top, min(work_bottom - height, y))
    return int(x), int(y)


def pet_side_button_positions(
    pet_rect: tuple[int, int, int, int],
    work_area: tuple[int, int, int, int],
    scale: float = 1.0,
    count: int = 2,
    kinds: tuple[str, ...] | None = None,
) -> dict[str, tuple[int, int]]:
    """Place separate settings/history buttons by the pet's left shoulder/waist."""

    kinds = kinds if kinds is not None else ("settings", "history", "speech", "proactive")[:count]
    count = len(kinds)
    pet_left, pet_top, _pet_right, pet_bottom = pet_rect
    work_left, work_top, work_right, work_bottom = work_area
    width = px(TOOLBAR_BUTTON_WIDTH, scale)
    height = px(TOOLBAR_BUTTON_HEIGHT, scale)
    group_height = height * count + px(TOOLBAR_BUTTON_GAP, scale) * (count - 1)
    x = pet_left - width - px(TOOLBAR_PET_GAP, scale)
    x = max(work_left, min(work_right - width, x))
    first_y = pet_top + int((pet_bottom - pet_top) * 0.25)
    first_y = max(work_top, min(work_bottom - group_height, first_y))
    return {
        kind: (int(x), int(first_y + index * (height + px(TOOLBAR_BUTTON_GAP, scale))))
        for index, kind in enumerate(kinds)
    }


def load_chat_window_position(path: Path | None) -> tuple[int, int] | None:
    """Load only the independent chat window position from its state file."""

    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    x = value.get("x")
    y = value.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return int(x), int(y)


def save_chat_window_position(path: Path | None, position: tuple[int, int] | None) -> None:
    """Persist the independent chat position atomically."""

    if path is None or position is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"x": int(position[0]), "y": int(position[1])}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class DesktopPetBackendError(RuntimeError):
    """A user-readable error returned by the local desktop chat backend."""


class _DisabledDesktopPetBackend:
    """In-memory backend used when the model-controls panel is the only UI."""

    def ask(self, *_args: Any, **_kwargs: Any) -> ChatReply:
        raise DesktopPetBackendError("桌面聊天功能未启用。")

    def close(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class ChatReply:
    text: str
    audio: bytes = b""
    emotion: str | None = None
    action: str | None = None
    audio_pending: bool = False


def load_chat_history(path: Path | None, limit: int) -> list[dict[str, str]]:
    """Load a local transcript without letting a damaged file break the pet."""

    if path is None or not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    messages: list[dict[str, str]] = []
    for item in value[-limit:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        text = str(item.get("text") or "").strip()
        created_at = str(item.get("created_at") or "")
        if role in {"user", "bot"} and text:
            messages.append({"role": role, "text": text, "created_at": created_at})
    return messages


def save_chat_history(
    path: Path | None,
    messages: list[dict[str, str]],
    limit: int,
) -> None:
    """Atomically save only the newest chat messages."""

    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(messages[-limit:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


class DesktopPetBackendClient:
    """Talk to the existing NachoBot Local Host HTTP adapter."""

    def __init__(self, config: DesktopChatConfig) -> None:
        self.config = config
        parts = urlsplit(config.backend_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("desktop chat backend_url must be an HTTP(S) URL")

    def ask(
        self,
        text: str,
        *,
        include_audio: bool = True,
        tts_language: str = "auto",
    ) -> ChatReply:
        before = self.status()
        before_replies = int(before.get("replies", 0) or 0)
        before_text_replies = int(before.get("text_replies", before_replies) or 0)
        before_audio_version = int(before.get("neural_audio_version", 0) or 0)
        self._request_json(
            "/api/respond",
            method="POST",
            payload={
                "text": text,
                "tts_language": tts_language,
                "speak": include_audio,
            },
            timeout=self.config.request_timeout_seconds,
        )

        deadline = time.monotonic() + self.config.reply_timeout_seconds
        latest_status = before
        while time.monotonic() < deadline:
            time.sleep(self.config.poll_interval_seconds)
            latest_status = self.status()
            text_replies = int(
                latest_status.get("text_replies", latest_status.get("replies", 0))
                or 0
            )
            if text_replies > before_text_replies:
                reply = str(latest_status.get("latest_reply") or "").strip()
                if reply:
                    audio_pending = bool(
                        include_audio
                        and latest_status.get("tts_enabled", True)
                        and latest_status.get("tts_in_progress", False)
                    )
                    return ChatReply(
                        reply,
                        (
                            b""
                            if audio_pending
                            else (
                                self._fetch_audio(latest_status, before_audio_version)
                                if include_audio
                                else b""
                            )
                        ),
                        str(latest_status.get("latest_emotion") or "") or None,
                        str(latest_status.get("latest_action") or "") or None,
                        audio_pending,
                    )
            reply_error = str(latest_status.get("reply_error") or "").strip()
            if reply_error:
                raise DesktopPetBackendError(f"NachoBot 回答失败：{reply_error}")
            backend_error = str(latest_status.get("last_error") or "").strip()
            if backend_error and not latest_status.get("core_connected", True):
                raise DesktopPetBackendError(f"NachoBot 回答失败：{backend_error}")
        raise DesktopPetBackendError("等待 NachoBot 回答超时，请检查 Core 是否仍在运行。")

    def announce(
        self,
        text: str,
        *,
        include_audio: bool = True,
        tts_language: str = "auto",
    ) -> ChatReply:
        before = self.status()
        before_audio_version = int(before.get("neural_audio_version", 0) or 0)
        result = self._request_json(
            "/api/announce",
            method="POST",
            payload={
                "text": text,
                "tts_language": tts_language,
                "speak": include_audio,
            },
            timeout=self.config.reply_timeout_seconds,
        )
        reply = str(result.get("text") or text).strip()
        status = self.status()
        return ChatReply(
            reply,
            self._fetch_audio(status, before_audio_version) if include_audio else b"",
            str(status.get("latest_emotion") or "") or None,
            str(status.get("latest_action") or "") or None,
        )

    def status(self) -> dict[str, Any]:
        return self._request_json(
            "/api/status",
            timeout=self.config.request_timeout_seconds,
        )

    def _fetch_audio(self, status: dict[str, Any], before_version: int) -> bytes:
        if not status.get("neural_audio_ready", False):
            return b""
        current_version = int(status.get("neural_audio_version", 0) or 0)
        if current_version <= before_version:
            return b""
        streamed_version = int(status.get("live2d_streamed_audio_version", 0) or 0)
        if streamed_version == current_version:
            return b""
        response = self._open(
            "/api/latest-speech.mp3",
            timeout=self.config.request_timeout_seconds,
        )
        with response:
            audio = response.read(16 * 1024 * 1024 + 1)
        if len(audio) > 16 * 1024 * 1024:
            raise DesktopPetBackendError("生成的语音超过 16 MiB，已拒绝播放。")
        return audio

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        response = self._open(path, method=method, data=data, headers=headers, timeout=timeout)
        with response:
            raw = response.read().decode("utf-8")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DesktopPetBackendError("NachoBot 返回了无法识别的响应。") from exc
        if not isinstance(value, dict):
            raise DesktopPetBackendError("NachoBot 返回的数据格式不正确。")
        return value

    def _open(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> HTTPResponse:
        url = urljoin(self.config.backend_url.rstrip("/") + "/", path.lstrip("/"))
        request = Request(url, data=data, headers=headers or {}, method=method)
        try:
            return urlopen(request, timeout=timeout)
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = str(payload.get("detail") or payload.get("message") or exc.reason)
            except (json.JSONDecodeError, AttributeError, TypeError):
                detail = str(exc.reason)
            raise DesktopPetBackendError(f"NachoBot 请求失败：{detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DesktopPetBackendError(
                "无法连接本机问答服务，请先运行仓库根目录的 launch_local_host.bat。"
            ) from exc


class DesktopPetChat:
    """Own the hover-revealed Tk chat surface without blocking Live2D."""

    _TTS_LANGUAGE_LABELS = {
        "auto": "自动",
        "zh": "中文",
        "ja": "日语",
        "en": "英语",
    }
    _TTS_LANGUAGE_ORDER = ("auto", "zh", "ja", "en")

    def __init__(
        self,
        config: DesktopChatConfig,
        title: str,
        on_renderer_command: RendererCommand,
        logger: Any,
        backend: DesktopPetBackendClient | None = None,
        *,
        character_name: str = "日和",
        chat_header: str = "桃濑日和 · NachoBot",
    ) -> None:
        self.config = config
        self.title = title
        self.character_name = character_name
        self.chat_header = chat_header
        self.on_renderer_command = on_renderer_command
        self.logger = logger
        self._ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._perception = None
        self._pending_transcript: dict | None = None
        self._continuous_transcripts: deque[dict] = deque()
        self._mic_canvas = None
        self._mic_badge_id = None
        self._mic_glyph_ids: list[int] = []
        self._microphone_source: str | None = None
        from .push_to_talk import HoldToTalkKey
        self._ptt_key = HoldToTalkKey(config.perception.push_to_talk_key)
        self._ptt_mode = config.perception.push_to_talk_mode
        if config.enabled and config.transport == "core" and (config.perception.vision or config.perception.speech):
            from .perception import DesktopPerception
            self._perception = DesktopPerception(config, logger, lambda name, value: self._ui_queue.put((name, value)))
        if backend is not None:
            self.backend = backend
        elif not config.enabled:
            # Model controls can reuse this Tk host without constructing Core,
            # HTTP, or voice clients when chat itself is disabled.
            self.backend = _DisabledDesktopPetBackend()
        elif config.transport == "core":
            from .core_chat import CoreChatClient

            def voice_error(text: str) -> None:
                self._ui_queue.put(("message", ("bot", text)))
                self._ui_queue.put(("result", "语音生成失败 · 文字已保留"))

            def voice_command(command: str, value: Any) -> None:
                on_renderer_command(command, value)
                if (command == "queue_audio" and value.get("reset")) or (
                    command == "voice_stream"
                    and value.get("event") == "chunk"
                    and value.get("seq") == 0
                ):
                    self._ui_queue.put(("result", "语音已就绪"))

            self.backend = CoreChatClient(
                config, voice_command, logger, voice_error,
                context_provider=self._perception.context if self._perception else None,
            )
        else:
            self.backend = DesktopPetBackendClient(config)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._work_lock = threading.Lock()
        self._stopped = threading.Event()
        self._proactive_cancel = threading.Event()
        self._proactive_inflight = False
        self._proactive_enabled = bool(config.proactive and config.enabled and config.transport == "core")
        self._user_activity_epoch = 0
        self._last_user_activity_at = time.monotonic()
        self._last_bot_activity_at: float | None = None
        self._unanswered_proactive_count = 0
        self._proactive_interval = PROACTIVE_CHECK_SECONDS
        self._proactive_chance = PROACTIVE_CHANCE
        self._next_proactive_at = self._last_user_activity_at + config.proactive_idle_seconds
        self._last_proactive_poll = 0.0
        self._deferred_user_input: tuple[str, bool] | None = None
        self._output_speaking = False
        self._root = None
        self._window = None
        self._toolbar_window = None
        self._history_button_window = None
        self._speech_button_window = None
        self._proactive_button_window = None
        self._history_window = None
        self._parameter_panel = None
        self._parameter_specs: list[dict[str, Any]] = []
        self._input_variable = None
        self._placeholder_visible = False
        self._result_widget = None
        self._history_canvas = None
        self._history_content = None
        self._history_window_item = None
        self._history_placeholder = None
        self._entry_widget = None
        self._ui_scale = 1.0
        self._anchor: dict[str, Any] | None = None
        self._last_anchor_request: tuple[Any, ...] | None = None
        self._history_position = (
            load_chat_window_position(config.window_state_path)
            if config.remember_position
            else None
        )
        self._history_drag_start: tuple[int, int, int, int] | None = None
        self._last_input_edit_at: float | None = None
        self._toolbar_animations: dict[str, dict[str, Any]] = {}
        self._activation_bind_tag: str | None = None
        self._hover_state = PetChatHoverState()
        self._voice_enabled = bool(config.play_audio)
        self._tts_language = config.tts_language
        self._voice_button = None
        self._language_button = None
        self._voice_button_items: tuple[int, int] | None = None
        self._language_button_items: tuple[int, int] | None = None
        self._history_messages = load_chat_history(
            config.history_path,
            config.max_history_messages,
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run_ui,
            name="nachobot-live2d-chat",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def open(self, *, from_pet: bool = False) -> None:
        """Focus the chat for a pet double-click or an active chat session."""

        if not self.config.enabled:
            return
        self.start()
        self._ui_queue.put(("open", bool(from_pet)))

    def open_parameters(self) -> None:
        """Request model controls; requests outside a pet-hover session are ignored."""

        self.start()
        self._ui_queue.put(("parameters_open", None))

    def sync_parameter_specs(self, specs: list[dict[str, Any]]) -> None:
        """Store a copied render-thread snapshot and queue it only for a live Tk host."""

        copied = [dict(item) for item in specs if isinstance(item, dict)]
        self._parameter_specs = copied
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._ui_queue.put(("parameter_specs", copied))

    def sync_anchor(
        self,
        pet_rect: tuple[int, int, int, int],
        work_area: tuple[int, int, int, int],
        *,
        visible: bool,
        topmost: bool,
        click_through: bool,
        hovered: bool = False,
        model_scale: float = 1.0,
    ) -> None:
        """Queue fresh pet geometry and hit-tested hover state for the Tk thread."""

        request = (
            tuple(int(value) for value in pet_rect),
            tuple(int(value) for value in work_area),
            bool(visible),
            bool(topmost),
            bool(click_through),
            bool(hovered),
            ui_scale(model_scale),
        )
        if request == self._last_anchor_request:
            return
        self._last_anchor_request = request
        self._ui_queue.put(
            (
                "anchor",
                {
                    "pet_rect": request[0],
                    "work_area": request[1],
                    "visible": request[2],
                    "topmost": request[3],
                    "click_through": request[4],
                    "hovered": request[5],
                    "ui_scale": request[6],
                },
            )
        )

    def stop(self) -> None:
        self._stopped.set()
        self._proactive_cancel.set()
        if self._perception is not None:
            self._perception.close()
        close_backend = getattr(self.backend, "close", None)
        if close_backend is not None:
            close_backend()
        self._ui_queue.put(("stop", None))
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def execute_for_test(self, text: str) -> str:
        """Exercise plain-text backend routing without constructing Tk."""

        return self._execute(text).text

    def _run_ui(self) -> None:
        try:
            import tkinter as tk

            with _suppress_thread_window_activation():
                root = tk.Tk()
                self._prepare_hidden_root(root)
            self._root = root
            self._tk = tk
            self._ready.set()
            self.logger.info("[DesktopPetChat] chat UI ready")
            self._next_proactive_at = time.monotonic() + self.config.proactive_idle_seconds
            if self._perception is not None:
                self._perception.start()
            root.after(16, self._pump_ui_queue)
            root.mainloop()
        except Exception as exc:
            self._ready.set()
            self.logger.warning("Desktop chat window unavailable: {}", exc)
        finally:
            if self._perception is not None:
                self._perception.close()
            self._root = None
            self._window = None
            self._toolbar_window = None
            self._history_button_window = None
            self._speech_button_window = None
            self._proactive_button_window = None
            self._history_window = None

    def _prepare_hidden_root(self, root: Any) -> None:
        """Keep Tk's owner hidden and disabled before independent dialogs map."""

        root.withdraw()
        root.update_idletasks()
        self._set_window_no_activate(root, enabled=True)
        try:
            import win32con
            import win32gui

            root_hwnd = self._window_root_hwnd(root, win32con, win32gui)
            win32gui.EnableWindow(root_hwnd, False)
        except Exception as exc:
            self.logger.debug("Could not disable hidden Tk owner: {}", exc)

    def _pump_ui_queue(self) -> None:
        root = self._root
        if root is None:
            return
        open_requested = False
        open_from_pet = False
        open_parameters = False
        open_history = False
        latest_anchor = None
        try:
            while True:
                action, value = self._ui_queue.get_nowait()
                if action == "open":
                    open_requested = True
                    open_from_pet = open_from_pet or bool(value)
                elif action == "parameters_open":
                    open_parameters = True
                elif action == "history_open":
                    open_history = True
                elif action == "parameter_specs":
                    self._parameter_specs = list(value)
                    if self._parameter_panel is not None:
                        self._parameter_panel.update_specs(self._parameter_specs)
                elif action == "anchor":
                    latest_anchor = value
                elif action == "transcript":
                    if isinstance(value, dict) and value.get("continuous") is True:
                        if len(self._continuous_transcripts) >= 16:
                            self._cancel_microphone()
                            self._append_message("system", "待发送语音过多，已关闭持续录音，请稍后重新开启。")
                        else:
                            self._continuous_transcripts.append(value)
                    else:
                        self._pending_transcript = value
                elif action == "input_error":
                    self._append_message("system", str(value))
                    self._set_result(str(value))
                elif action == "recording_done":
                    if self._perception is None or not self._perception.recording:
                        self._microphone_source = None
                        self._release_microphone_grab()
                        self._paint_microphone(False)
                elif action == "result":
                    self._set_result(str(value))
                elif action == "message":
                    role, text = value
                    self._append_message(str(role), str(text))
                    if role == "bot":
                        self._note_bot_activity()
                elif action == "proactive_finished":
                    self._next_proactive_at = max(self._next_proactive_at, time.monotonic() + self._proactive_interval)
                elif action == "proactive_reply":
                    epoch, reply = value
                    last_speech, speech_active = self._perception.speech_activity() if self._perception is not None else (0, False)
                    if speech_active or last_speech > self._last_user_activity_at:
                        self._note_user_activity(responded=last_speech > self._last_user_activity_at)
                    if self._proactive_enabled and epoch == self._user_activity_epoch and not self._stopped.is_set() and (latest_anchor or self._anchor or {}).get("visible", False):
                        if self._voice_enabled:
                            voiced = self.backend.announce(reply.text, include_audio=True, tts_language=self._tts_language)
                            reply = replace(reply, audio_pending=voiced.audio_pending)
                        self._present_reply(reply)
                        self._append_message("bot", reply.text)
                        self._note_bot_activity(proactive=True)
                elif action == "voice_state":
                    self._refresh_voice_controls()
                elif action == "stop":
                    self._remember_history_position()
                    root.quit()
                    return
        except queue.Empty:
            pass
        if latest_anchor is not None:
            self._anchor = dict(latest_anchor)
            if not self._anchor.get("visible", False) and self._proactive_inflight:
                self._note_user_activity()
            self._apply_ui_scale(float(latest_anchor.get("ui_scale", 1.0)))
        self._poll_keyboard_microphone()
        self._poll_microphone_release()
        if self._deferred_user_input is not None and not self._work_lock.locked():
            text, clear_input = self._deferred_user_input
            self._deferred_user_input = None
            self._submit_text(text, clear_input=clear_input)
        if (self._pending_transcript is not None or self._continuous_transcripts) and not self._work_lock.locked():
            if self._continuous_transcripts:
                value = self._continuous_transcripts.popleft()
            else:
                value, self._pending_transcript = self._pending_transcript, None
            transcript = self._perception.transcript_text(value) if self._perception else None
            if transcript:
                self._submit_text(transcript)
        self._poll_proactive()
        if open_parameters:
            self._show_parameter_panel()
        if open_history:
            self._show_history_window()
        self._update_hover_visibility(
            open_requested=open_requested,
            open_from_pet=open_from_pet,
        )
        root.after(16, self._pump_ui_queue)

    def _apply_ui_scale(self, scale: float) -> None:
        area = (self._anchor or {}).get("work_area")
        if area:
            scale = min(scale, (area[2] - area[0]) / HISTORY_WIDTH, (area[3] - area[1]) / 620)
        if abs(scale - self._ui_scale) < 0.001:
            return
        self._ui_scale = scale
        for window in (self._window, self._toolbar_window, self._history_button_window, self._speech_button_window, self._proactive_button_window, self._history_window):
            if window is not None and window.winfo_exists():
                window._pet_scale = scale
                scale_widgets(window, scale)
        if self._parameter_panel is not None:
            self._parameter_panel.set_scale(scale, (self._anchor or {}).get("work_area"))
        if self._history_window is not None:
            self._position_history_window()

    @staticmethod
    def _window_root_hwnd(window: Any, win32con: Any, win32gui: Any) -> int:
        hwnd = int(window.winfo_id())
        try:
            return int(win32gui.GetAncestor(hwnd, win32con.GA_ROOT) or hwnd)
        except Exception:
            return hwnd

    def _cursor_over_chat(self) -> bool:
        windows = self._owned_interaction_windows()
        if not windows:
            return False
        try:
            import win32api
            import win32con
            import win32gui

            hit = int(win32gui.WindowFromPoint(tuple(win32api.GetCursorPos())))
            hit_root = int(win32gui.GetAncestor(hit, win32con.GA_ROOT) or hit)
            for window in windows:
                chat_root = self._window_root_hwnd(window, win32con, win32gui)
                if hit_root == chat_root or bool(win32gui.IsChild(chat_root, hit)):
                    return True
            return False
        except Exception:
            # Tk's geometry fallback is for non-Windows preview/test hosts only.
            for window in windows:
                try:
                    x = int(window.winfo_pointerx())
                    y = int(window.winfo_pointery())
                    left = int(window.winfo_rootx())
                    top = int(window.winfo_rooty())
                    if (
                        left <= x < left + int(window.winfo_width())
                        and top <= y < top + int(window.winfo_height())
                    ):
                        return True
                except Exception:
                    continue
            return False

    def _owned_interaction_windows(self) -> list[Any]:
        windows: list[Any] = []
        for window in (
            self._window,
            self._toolbar_window,
            self._history_button_window,
            self._speech_button_window,
            self._proactive_button_window,
            self._history_window,
        ):
            if window is not None and window.winfo_exists() and window.state() != "withdrawn":
                windows.append(window)
        if self._parameter_panel is not None:
            panel_window = getattr(self._parameter_panel, "window", None)
            if (
                panel_window is not None
                and panel_window.winfo_exists()
                and panel_window.state() != "withdrawn"
            ):
                windows.append(panel_window)
        return windows

    def _chat_interaction_active(self) -> bool:
        if self._perception is not None and self._perception.busy and self._ptt_mode == "hold":
            return True
        window = self._window
        if self._history_drag_start is not None:
            return True
        now = time.monotonic()
        if (
            self._last_input_edit_at is not None
            and now - self._last_input_edit_at <= INPUT_ACTIVITY_GRACE_SECONDS
        ):
            return True
        if (
            window is not None
            and window.winfo_exists()
            and window.state() != "withdrawn"
            and self._entry_widget is not None
        ):
            try:
                if (
                    window.focus_get() is self._entry_widget
                    and self._input_variable is not None
                    and not self._placeholder_visible
                    and self._input_variable.get().strip()
                ):
                    import win32con
                    import win32gui

                    foreground = int(win32gui.GetForegroundWindow())
                    composer_root = self._window_root_hwnd(
                        window, win32con, win32gui
                    )
                    foreground_root = int(
                        win32gui.GetAncestor(foreground, win32con.GA_ROOT)
                        or foreground
                    )
                    if foreground_root == composer_root or win32gui.IsChild(
                        composer_root, foreground
                    ):
                        return True
            except Exception:
                try:
                    if (
                        window.focus_get() is self._entry_widget
                        and self._input_variable is not None
                        and not self._placeholder_visible
                    and self._input_variable.get().strip()
                    ):
                        return True
                except Exception:
                    pass
        if self._parameter_panel is not None:
            try:
                if self._parameter_panel.is_interacting():
                    return True
            except Exception:
                pass
        return False

    def _update_hover_visibility(
        self,
        *,
        open_requested: bool = False,
        open_from_pet: bool = False,
    ) -> None:
        anchor = self._anchor or {}
        if anchor and not anchor.get("visible", False) and self._microphone_source is not None:
            self._cancel_microphone()
        pet_available = bool(
            anchor.get("visible", False)
            and not anchor.get("click_through", True)
        )
        pet_hovered = bool(pet_available and anchor.get("hovered", False))
        pointer_over_chat = self._cursor_over_chat()
        interaction_active = self._chat_interaction_active()
        visible = self._hover_state.update(
            now=time.monotonic(),
            pet_available=pet_available,
            pet_hovered=pet_hovered,
            chat_hovered=pointer_over_chat,
            interaction_active=interaction_active,
            open_from_pet=open_from_pet,
            open_allowed=open_requested,
        )
        focus = bool(open_requested and visible and not open_from_pet)

        if visible:
            if self.config.enabled:
                window = self._window
                if window is None or not window.winfo_exists():
                    self._show_window(focus=focus)
                else:
                    self._position_window()
                    if window.state() == "withdrawn":
                        window.update_idletasks()
                        self._set_window_no_activate(window, enabled=not focus)
                        window.deiconify()
                    if focus:
                        self._set_window_no_activate(window, enabled=False)
                    if focus and self._entry_widget is not None:
                        window.focus_force()
                        self._entry_widget.focus_force()
            self._show_toolbar()
            return
        self._hide_session_windows()

    def _show_parameter_panel(self) -> None:
        if not self._hover_state.visible:
            return
        try:
            from .parameter_controls import ParameterPanel

            newly_created = self._parameter_panel is None
            if newly_created:
                self._parameter_panel = ParameterPanel(
                    self._root,
                    self._on_parameter_change,
                    self._on_parameter_reset,
                    self.logger,
                )
            self._parameter_panel.set_scale(self._ui_scale, (self._anchor or {}).get("work_area"))
            self._set_window_no_activate(self._parameter_panel.window, enabled=True)
            self._parameter_panel.show(self._parameter_specs)
            if newly_created and self._anchor is not None:
                window = self._parameter_panel.window
                window.update_idletasks()
                x, y = popup_position(self._anchor["pet_rect"], self._anchor["work_area"],
                                      (window.winfo_width(), window.winfo_height()), self._ui_scale)
                window.geometry(f"+{x}+{y}")
        except Exception as exc:
            self.logger.warning("Could not open model controls: {}", exc)

    def _on_parameter_change(self, parameter_id: str, value: float) -> None:
        self.on_renderer_command(
            "desktop_parameter_set",
            {"id": str(parameter_id), "value": float(value)},
        )

    def _on_parameter_reset(self, group: str | None = None) -> None:
        self.on_renderer_command("desktop_parameters_reset", group)

    def _begin_history_drag(self, event: Any) -> None:
        window = self._history_window
        if window is None or not window.winfo_exists():
            return
        self._history_drag_start = (
            int(event.x_root),
            int(event.y_root),
            int(window.winfo_x()),
            int(window.winfo_y()),
        )

    def _drag_history_window(self, event: Any) -> None:
        window = self._history_window
        start = self._history_drag_start
        if window is None or not window.winfo_exists() or start is None:
            return
        start_x, start_y, window_x, window_y = start
        x = window_x + int(event.x_root) - start_x
        y = window_y + int(event.y_root) - start_y
        work_area = self._get_history_work_area()
        if work_area is not None:
            x, y = clamp_window_position(
                x,
                y,
                px(HISTORY_WIDTH, self._ui_scale),
                px(HISTORY_HEIGHT, self._ui_scale),
                work_area,
                0,
            )
        window.geometry(f"+{x}+{y}")
        self._history_position = (x, y)

    def _end_history_drag(self, _event: Any) -> None:
        self._history_drag_start = None
        self._remember_history_position()

    def _remember_history_position(self) -> None:
        if not self.config.remember_position:
            return
        window = self._history_window
        if window is not None and window.winfo_exists():
            self._history_position = (int(window.winfo_x()), int(window.winfo_y()))
        try:
            save_chat_window_position(
                self.config.window_state_path, self._history_position
            )
        except OSError as exc:
            self.logger.warning("Could not save desktop history position: {}", exc)

    def _get_history_work_area(self) -> tuple[int, int, int, int] | None:
        """Resolve the monitor under the independent history window."""

        if self._history_position is None:
            anchor = self._anchor or {}
            work_area = anchor.get("work_area")
            if isinstance(work_area, (tuple, list)) and len(work_area) == 4:
                return tuple(int(value) for value in work_area)
        try:
            import win32api
            import win32con

            x, y = self._history_position or (0, 0)
            monitor = win32api.MonitorFromPoint(
                (x + HISTORY_WIDTH // 2, y + HISTORY_HEIGHT // 2),
                win32con.MONITOR_DEFAULTTONEAREST,
            )
            return tuple(int(value) for value in win32api.GetMonitorInfo(monitor)["Work"])
        except Exception:
            pass
        anchor = self._anchor or {}
        work_area = anchor.get("work_area")
        if isinstance(work_area, (tuple, list)) and len(work_area) == 4:
            return tuple(int(value) for value in work_area)
        return None

    def _enable_rounded_window_corners(self, window: Any) -> None:
        """Ask DWM for rounded top-level corners when the Windows version supports them."""

        try:
            import ctypes

            preference = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(window.winfo_id()),
                33,  # DWMWA_WINDOW_CORNER_PREFERENCE
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )
        except Exception:
            pass

    def _set_window_no_activate(self, window: Any, enabled: bool) -> None:
        """Prevent hover reveal from changing foreground; allow activation on chat entry."""

        try:
            import win32con
            import win32gui

            hwnd = self._window_root_hwnd(window, win32con, win32gui)
            ex_style = int(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE))
            no_activate = int(getattr(win32con, "WS_EX_NOACTIVATE", 0x08000000))
            next_style = ex_style | no_activate if enabled else ex_style & ~no_activate
            if next_style != ex_style:
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, next_style)
                flags = (
                    win32con.SWP_NOMOVE
                    | win32con.SWP_NOSIZE
                    | win32con.SWP_NOZORDER
                    | win32con.SWP_NOACTIVATE
                    | win32con.SWP_FRAMECHANGED
                )
                win32gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0, flags)
        except Exception as exc:
            self.logger.debug("Could not update chat no-activate style: {}", exc)

    def _activate_chat_from_pointer(self, event: Any = None) -> None:
        """A real click may activate chat; passive hover never takes focus."""

        window = self._window
        if window is None or not window.winfo_exists():
            return
        widget = getattr(event, "widget", None)
        if widget is not self._entry_widget:
            return
        try:
            if widget is not None and widget.winfo_toplevel() is not window:
                return
        except Exception:
            return
        self._set_window_no_activate(window, enabled=False)
        try:
            window.focus_force()
        except self._tk.TclError:
            pass

    def _bind_chat_activation(self, window: Any) -> None:
        """Install a first-stage Tk bindtag so Canvas buttons can still activate on click."""

        if self._root is None:
            return
        if self._activation_bind_tag is None:
            self._activation_bind_tag = f"DesktopPetChatActivation_{id(self):x}"
            self._root.bind_class(
                self._activation_bind_tag,
                "<ButtonPress-1>",
                self._activate_chat_from_pointer,
            )

        pending = [window]
        while pending:
            widget = pending.pop()
            try:
                tags = tuple(widget.bindtags())
                if self._activation_bind_tag not in tags:
                    widget.bindtags((self._activation_bind_tag, *tags))
                pending.extend(widget.winfo_children())
            except self._tk.TclError:
                continue

    def _make_canvas_button(
        self,
        parent: Any,
        text: str,
        command: Callable[[], None],
        *,
        width: int,
        height: int = 30,
        fill: str = "#eef4f2",
        active_fill: str = "#e2f4ef",
        foreground: str = _CHAT_COLORS["secondary"],
        font: tuple[str, int, str] = ("Microsoft YaHei UI", 8, "bold"),
    ) -> tuple[Any, tuple[int, int]]:
        tk = self._tk
        canvas = tk.Canvas(
            parent,
            width=width,
            height=height,
            background=parent.cget("background"),
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
        )
        background_id = _rounded_rectangle(
            canvas,
            1,
            1,
            width - 1,
            height - 1,
            min(height // 2, 12),
            fill=fill,
            outline=fill,
        )
        text_id = canvas.create_text(
            width // 2,
            height // 2,
            text=text,
            fill=foreground,
            font=font,
        )
        canvas.bind("<Enter>", lambda _event: canvas.itemconfigure(background_id, fill=active_fill))
        canvas.bind("<Leave>", lambda _event: canvas.itemconfigure(background_id, fill=fill))

        def activate(_event: Any) -> str:
            command()
            return "break"

        canvas.bind("<Button-1>", activate)
        return canvas, (background_id, text_id)

    def _show_window(self, *, focus: bool) -> None:
        if not self.config.enabled:
            return
        tk = self._tk
        if self._window is not None and self._window.winfo_exists():
            self._position_window()
            self._window.update_idletasks()
            self._set_window_no_activate(self._window, enabled=not focus)
            self._window.deiconify()
            if focus and self._entry_widget is not None:
                self._window.focus_force()
                self._entry_widget.focus_force()
            return

        window = tk.Toplevel(self._root)
        window.withdraw()
        self._window = window
        window.title(f"和 {self.character_name} 聊天")
        window.overrideredirect(True)
        self._set_window_no_activate(window, enabled=True)
        window.attributes("-topmost", bool((self._anchor or {}).get("topmost", True)))
        try:
            window.attributes("-transparentcolor", TOOLBAR_COLOR_KEY)
            window.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        window.resizable(False, False)
        window.configure(background=TOOLBAR_COLOR_KEY)
        window.protocol("WM_DELETE_WINDOW", self._hide_window)
        window.bind("<Escape>", lambda _event: self._hide_window())
        self._enable_rounded_window_corners(window)
        self._build_composer_surface(window)
        self._paint_microphone(bool(self._perception and self._perception.recording))
        self._bind_chat_activation(window)
        window._pet_scale = self._ui_scale
        scale_widgets(window, self._ui_scale)
        self._position_window()
        window.update_idletasks()
        self._set_window_no_activate(window, enabled=not focus)
        window.deiconify()
        if focus:
            window.focus_force()
            self._entry_widget.focus_force()
        self.logger.info("[DesktopPetChat] composer window created (focus={})", focus)

    def _build_composer_surface(self, window: Any) -> None:
        """One borderless floating composer with a real icon-only send action."""
        self._result_widget = self._voice_button = self._language_button = None
        self._voice_button_items = self._language_button_items = None
        self._input_variable = self._tk.StringVar(master=window)
        self._placeholder_visible = False
        prompt = f"和{self.character_name}说点什么…"
        canvas = self._tk.Canvas(window, width=DOCK_WIDTH, height=DOCK_HEIGHT,
                                 background=TOOLBAR_COLOR_KEY, borderwidth=0, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        _rounded_rectangle(canvas, 3, 7, DOCK_WIDTH - 3, DOCK_HEIGHT - 1, 24,
                           fill=_CHAT_COLORS["shadow"], outline="")
        surface = _rounded_rectangle(canvas, 1, 1, DOCK_WIDTH - 1, DOCK_HEIGHT - 5, 24,
                                      fill=_CHAT_COLORS["card"], outline="")
        badge = canvas.create_oval(15, 14, 43, 42, fill=_CHAT_COLORS["history"], outline="")
        glyph = canvas.create_line(22, 28, 36, 28, 29, 21, 29, 35,
                                    fill=_CHAT_COLORS["accent"], width=1.6, capstyle="round")
        if self._perception is not None and self.config.perception.speech:
            canvas.itemconfigure(glyph, state="hidden")
            mic = self._tk.Canvas(canvas, width=32, height=32, background=_CHAT_COLORS["card"],
                                  borderwidth=0, highlightthickness=0, cursor="hand2")
            self._mic_canvas = mic
            self._mic_badge_id = mic.create_oval(1, 1, 31, 31,
                                                fill=_CHAT_COLORS["accent_bg"], outline="")
            self._mic_glyph_ids = [
                mic.create_rectangle(13, 7, 19, 19, outline=_CHAT_COLORS["accent"], width=1.5),
                mic.create_line(10, 15, 10, 19, 13, 22, 19, 22, 22, 19, 22, 15,
                                fill=_CHAT_COLORS["accent"], width=1.5, smooth=True),
                mic.create_line(16, 22, 16, 26, 12, 26, 20, 26, fill=_CHAT_COLORS["accent"], width=1.5),
            ]
            canvas.create_window(29, 29, width=32, height=32, window=mic)
            mic.bind("<ButtonPress-1>", self._begin_microphone)
            mic.bind("<ButtonRelease-1>", self._end_microphone)
            mic.bind("<Escape>", lambda _event: self._cancel_microphone())
            window.bind("<Unmap>", self._on_composer_unmap, add="+")
        send = canvas.create_oval(DOCK_WIDTH - 49, 11, DOCK_WIDTH - 15, 45,
                                 fill=_CHAT_COLORS["accent"], outline="", tags="send")
        enter = canvas.create_line(DOCK_WIDTH - 32, 36, DOCK_WIDTH - 32, 21,
                                    DOCK_WIDTH - 38, 27, DOCK_WIDTH - 32, 21, DOCK_WIDTH - 26, 27,
                                    fill="#ffffff", width=1.8, capstyle="round", joinstyle="round", tags="send")
        send_armed = False

        def press_send(_event: Any) -> None:
            nonlocal send_armed
            send_armed = True

        def release_send(event: Any) -> None:
            nonlocal send_armed
            was_armed, send_armed = send_armed, False
            x1, y1, x2, y2 = canvas.coords(send)
            if was_armed and ((event.x-(x1+x2)/2)/((x2-x1)/2))**2 + ((event.y-(y1+y2)/2)/((y2-y1)/2))**2 <= 1:
                self._submit()

        canvas.tag_bind("send", "<ButtonPress-1>", press_send)
        canvas.bind("<ButtonRelease-1>", release_send)
        canvas.tag_bind("send", "<Enter>", lambda _event: (canvas.configure(cursor="hand2"), canvas.itemconfigure(send, fill=_CHAT_COLORS["accent_dark"])))
        canvas.tag_bind("send", "<Leave>", lambda _event: (canvas.configure(cursor=""), canvas.itemconfigure(send, fill=_CHAT_COLORS["accent"])))
        entry = self._tk.Entry(canvas, textvariable=self._input_variable, width=1, relief="flat",
                               borderwidth=0, highlightthickness=0, background=_CHAT_COLORS["card"],
                               foreground=_CHAT_COLORS["text"], insertbackground=_CHAT_COLORS["accent"],
                               selectbackground=_CHAT_COLORS["accent_bg"], font=("Microsoft YaHei UI", 11))
        canvas.create_window(54, 28, anchor="w", width=DOCK_WIDTH - 115, height=30, window=entry)
        self._entry_widget = entry

        def changed(*_args: Any) -> None:
            if self._placeholder_visible and self._input_variable.get() != prompt:
                self._placeholder_visible = False
                entry.configure(foreground=_CHAT_COLORS["text"])

        def paint(active: bool) -> None:
            color = _CHAT_COLORS["card"]
            canvas.itemconfigure(surface, fill=color)
            canvas.itemconfigure(badge, fill=_CHAT_COLORS["accent_bg"] if active else _CHAT_COLORS["history"])
            canvas.itemconfigure(glyph, fill=_CHAT_COLORS["accent"])
            entry.configure(background=color)
            if active and self._placeholder_visible:
                self._placeholder_visible = False
                self._input_variable.set("")
                entry.configure(foreground=_CHAT_COLORS["text"])
            elif not active and not self._input_variable.get():
                self._input_variable.set(prompt)
                self._placeholder_visible = True
                entry.configure(foreground=_CHAT_COLORS["muted"])
            self._update_hover_visibility()

        entry.bind("<Return>", lambda _event: self._submit())
        entry.bind("<FocusIn>", lambda _event: paint(True))
        entry.bind("<FocusOut>", lambda _event: paint(False))
        entry.bind("<KeyRelease>", self._note_input_activity)
        if callable(getattr(self._input_variable, "trace_add", None)):
            self._input_variable.trace_add("write", changed)
            self._input_variable.set(prompt)
            self._placeholder_visible = True
            entry.configure(foreground=_CHAT_COLORS["muted"])

    def _toolbar_definitions(self) -> list:
        definitions = [
            ("settings", "设置", self._show_parameter_panel),
        ]
        if self.config.enabled:
            definitions.append(("history", "历史", self._show_history_window))
        if self.config.enabled and self.config.perception.speech:
            definitions.append(("speech", "语音模式", self._toggle_speech_mode))
        if self.config.enabled and self.config.transport == "core":
            definitions.append(("proactive", "主动对话", self._toggle_proactive))
        return definitions

    def _show_toolbar(self) -> None:
        if not self._hover_state.visible or self._anchor is None:
            return
        definitions = self._toolbar_definitions()
        for kind, label, command in definitions:
            window = self._toolbar_window_for(kind)
            if window is None or not window.winfo_exists():
                window = self._create_toolbar_button(kind, label, command)
            if window.state() == "withdrawn":
                self._position_toolbar(kind, animate=True)
                window.update_idletasks()
                self._set_window_no_activate(window, enabled=True)
                window.deiconify()
                window.after_idle(
                    lambda target=window: self._set_window_no_activate(
                        target, enabled=True
                    )
                )
            else:
                self._position_toolbar(kind)

    def _paint_microphone(self, recording: bool) -> None:
        self._paint_speech_mode()
        mic = self._mic_canvas
        if mic is None or not mic.winfo_exists():
            return
        mic.itemconfigure(self._mic_badge_id, fill="#dc6b65" if recording else _CHAT_COLORS["accent_bg"])
        for item in self._mic_glyph_ids:
            option = "outline" if mic.type(item) == "rectangle" else "fill"
            mic.itemconfigure(item, **{option: "#ffffff" if recording else _CHAT_COLORS["accent"]})

    def _release_microphone_grab(self) -> None:
        mic = self._mic_canvas
        if mic is not None and mic.winfo_exists():
            try:
                mic.grab_release()
            except self._tk.TclError:
                pass

    def _begin_microphone(self, _event: Any = None) -> str:
        self._microphone_press("mouse")
        return "break"

    def _microphone_press(self, source: str) -> None:
        if self._ptt_mode == "toggle" and self._microphone_source is not None:
            self._cancel_microphone()
        elif self._microphone_source is None:
            self._start_microphone(source)

    def _toggle_speech_mode(self) -> None:
        # A mode change never submits a partial recording or accepts a late ASR result.
        self._cancel_microphone()
        self._ptt_mode = "toggle" if self._ptt_mode == "hold" else "hold"
        # Re-arm from the current physical state, so switching while held cannot start recording.
        from .push_to_talk import HoldToTalkKey
        self._ptt_key = HoldToTalkKey(self.config.perception.push_to_talk_key)
        self._paint_speech_mode()

    def _start_microphone(self, source: str) -> None:
        self._note_user_activity()
        if self._perception is None or (self._work_lock.locked() and self._ptt_mode == "hold" and not self._proactive_inflight):
            return
        started = self._perception.start_recording(continuous=True) if self._ptt_mode == "toggle" else self._perception.start_recording()
        if started:
            self._microphone_source = source
            self.backend.set_voice_enabled(self._voice_enabled)
            self._paint_microphone(True)
            if source == "mouse" and self._ptt_mode == "hold":
                try:
                    self._mic_canvas.grab_set()
                except self._tk.TclError:
                    self._cancel_microphone()

    def _end_microphone(self, _event: Any = None, *, source: str = "mouse") -> str:
        if self._ptt_mode == "toggle":
            return "break"
        if self._microphone_source not in (None, source):
            return "break"
        if self._perception is not None:
            self._perception.stop_recording()
        self._microphone_source = None
        self._release_microphone_grab()
        self._paint_microphone(False)
        return "break"

    def _poll_keyboard_microphone(self) -> None:
        if self._perception is None or not self.config.perception.speech or not self._ptt_key.keys or os.name != "nt":
            return
        try:
            import win32api
            transition = self._ptt_key.poll(win32api.GetAsyncKeyState)
            if transition == "press":
                self._microphone_press("keyboard")
            elif transition == "release":
                self._end_microphone(source="keyboard")
        except Exception:
            if self._microphone_source == "keyboard":
                self._cancel_microphone()

    def _poll_microphone_release(self) -> None:
        """End a press even if another window steals Tk's mouse capture."""
        if self._perception is None or not self._perception.recording or os.name != "nt":
            return
        try:
            import win32api
            if win32api.GetAsyncKeyState(0x1B) & 0x8000:
                self._cancel_microphone()
            elif self._ptt_mode == "hold" and self._microphone_source != "keyboard" and not win32api.GetAsyncKeyState(0x01) & 0x8000:
                self._end_microphone()
        except Exception:
            self._cancel_microphone()

    def _cancel_microphone(self) -> None:
        self._microphone_source = None
        self._pending_transcript = None
        self._continuous_transcripts.clear()
        if self._perception is not None:
            self._perception.stop_recording(cancel=True)
        self._release_microphone_grab()
        self._paint_microphone(False)

    def _on_composer_unmap(self, _event: Any = None) -> None:
        if self._ptt_mode == "hold":
            self._cancel_microphone()

    def set_output_speaking(self, speaking: bool) -> None:
        self._output_speaking = speaking
        if self._perception is not None:
            self._perception.set_output_speaking(speaking)

    def _toolbar_window_for(self, kind: str) -> Any:
        return {"settings": self._toolbar_window, "history": self._history_button_window,
                "speech": self._speech_button_window, "proactive": self._proactive_button_window}[kind]

    def _toggle_proactive(self) -> None:
        if not self.config.enabled or self.config.transport != "core":
            return
        self._proactive_enabled = not self._proactive_enabled
        self._proactive_cancel.set()
        self._user_activity_epoch += 1  # Invalidate already queued replies even after re-enabling.
        if self._proactive_enabled:
            self._next_proactive_at = time.monotonic() + self.config.proactive_idle_seconds
        elif self._proactive_inflight:
            self.backend.set_voice_enabled(self._voice_enabled)
        self._paint_proactive()

    def _paint_proactive(self) -> None:
        window = self._proactive_button_window
        if window is None or not window.winfo_exists():
            return
        canvas = window._proactive_canvas
        canvas.itemconfigure("proactive-glyph", fill=_CHAT_COLORS["accent_dark"] if self._proactive_enabled else _CHAT_COLORS["muted"])
        canvas.itemconfigure("proactive-dot", fill=_CHAT_COLORS["accent"] if self._proactive_enabled else _CHAT_COLORS["muted"])
        canvas.itemconfigure("proactive-surface", fill=_CHAT_COLORS["accent_bg"] if self._proactive_enabled else _CHAT_COLORS["card"])
        window.title("主动对话：已开启" if self._proactive_enabled else "主动对话：已关闭")

    def _paint_speech_mode(self) -> None:
        window = self._speech_button_window
        if window is None or not window.winfo_exists():
            return
        canvas = window._mode_canvas
        # Filled toggle switch versus outlined hold button; no text labels on the pet.
        canvas.itemconfigure("mode-toggle", state="normal" if self._ptt_mode == "toggle" else "hidden")
        canvas.itemconfigure("mode-hold", state="normal" if self._ptt_mode == "hold" else "hidden")
        track = canvas.find_withtag("mode-toggle")[0]
        canvas.itemconfigure(track, fill="#dc6b65" if self._microphone_source is not None else _CHAT_COLORS["accent"])
        window.title("语音模式：持续录音，再次按下关闭" if self._ptt_mode == "toggle" else "语音模式：按住说话")

    def _create_toolbar_button(
        self,
        kind: str,
        label: str,
        command: Callable[[], None],
    ) -> Any:
        tk = self._tk
        window = tk.Toplevel(self._root)
        window.withdraw()
        window.title(f"桌宠{label}按钮")
        window.overrideredirect(True)
        window.resizable(False, False)
        window.configure(background=TOOLBAR_COLOR_KEY)
        window.attributes("-topmost", bool(self._anchor.get("topmost", True)))
        try:
            window.attributes("-transparentcolor", TOOLBAR_COLOR_KEY)
            window.attributes("-toolwindow", True)
        except tk.TclError:
            self.logger.warning("Transparent hover buttons are unavailable on this Tk build")
        self._enable_rounded_window_corners(window)

        canvas = tk.Canvas(
            window,
            width=TOOLBAR_BUTTON_WIDTH,
            height=TOOLBAR_BUTTON_HEIGHT,
            background=TOOLBAR_COLOR_KEY,
            borderwidth=0,
            highlightthickness=0,
        )
        canvas.pack(fill="both", expand=True)
        _rounded_rectangle(canvas, 3, 5, 41, 43, 14, fill=_CHAT_COLORS["shadow"], outline="")
        button_box = (1, 1, 43, 41)
        shape = _rounded_rectangle(
            canvas,
            *button_box,
            14,
            fill=_CHAT_COLORS["card"],
            outline="",
        )
        if kind == "settings":
            for y, knob_x in ((12, 13), (22, 28), (32, 19)):
                canvas.create_line(
                    9, y, 34, y, fill=_CHAT_COLORS["accent_dark"], width=2
                )
                canvas.create_oval(
                    knob_x - 3,
                    y - 3,
                    knob_x + 3,
                    y + 3,
                    fill=_CHAT_COLORS["card"],
                    outline=_CHAT_COLORS["accent_dark"],
                    width=2,
                )
        elif kind == "history":
            canvas.create_line(
                14, 10, 28, 10, 34, 16, 34, 34, 14, 34, 14, 10,
                fill=_CHAT_COLORS["accent_dark"],
                width=2,
                joinstyle="round",
            )
            canvas.create_line(
                28, 10, 28, 16, 34, 16,
                fill=_CHAT_COLORS["accent_dark"],
                width=2,
            )
            for y in (22, 27, 32):
                canvas.create_line(
                    18, y, 30, y, fill=_CHAT_COLORS["accent_dark"], width=1
                )
        elif kind == "speech":
            canvas.create_oval(12, 8, 30, 26, outline=_CHAT_COLORS["accent_dark"], width=2, tags="mode-hold")
            canvas.create_oval(17, 13, 25, 21, fill=_CHAT_COLORS["accent_dark"], outline="", tags="mode-hold")
            canvas.create_line(14, 31, 28, 31, fill=_CHAT_COLORS["accent_dark"], width=2, tags="mode-hold")
            canvas.create_oval(8, 13, 35, 30, fill=_CHAT_COLORS["accent"], outline="", tags="mode-toggle")
            canvas.create_oval(21, 16, 32, 27, fill="#ffffff", outline="", tags="mode-toggle")
            window._mode_canvas = canvas
        else:
            canvas.addtag_withtag("proactive-surface", shape)
            canvas.create_line(10, 11, 32, 11, 32, 27, 21, 27, 14, 33, 14, 27, 10, 27, 10, 11,
                               width=2, joinstyle="round", tags="proactive-glyph")
            for x in (16, 22, 28):
                canvas.create_oval(x - 1, 18, x + 1, 20, outline="", tags="proactive-glyph")
            canvas.create_oval(31, 31, 38, 38, outline="", tags="proactive-dot")
            window._proactive_canvas = canvas

        def hover(active: bool) -> None:
            if kind == "proactive" and not active:
                self._paint_proactive()
                return
            fill = _CHAT_COLORS["accent_bg"] if active else _CHAT_COLORS["card"]
            canvas.itemconfigure(shape, fill=fill)

        canvas.bind("<Enter>", lambda _event: hover(True))
        canvas.bind("<Leave>", lambda _event: hover(False))
        canvas.bind("<Button-1>", lambda _event: (command(), "break")[1])
        scale_widgets(window, self._ui_scale)
        if kind == "settings":
            self._toolbar_window = window
        elif kind == "history":
            self._history_button_window = window
        elif kind == "speech":
            self._speech_button_window = window
            self._paint_speech_mode()
        else:
            self._proactive_button_window = window
            self._paint_proactive()
        return window

    def _position_toolbar(self, kind: str, *, animate: bool = False) -> None:
        window = self._toolbar_window_for(kind)
        anchor = self._anchor
        if window is None or not window.winfo_exists() or anchor is None:
            return
        positions = pet_side_button_positions(
            tuple(int(value) for value in anchor["pet_rect"]),
            tuple(int(value) for value in anchor["work_area"]),
            self._ui_scale,
            kinds=tuple(item[0] for item in self._toolbar_definitions()),
        )
        x, y = positions[kind]
        animation = self._toolbar_animations.get(kind)
        if not animate:
            if animation is not None and animation.get("after_id") is not None:
                # Move the slide's whole trajectory with the pet, preserving
                # its progress instead of freezing it at the old anchor.
                animation["from_x"] += x - animation["to_x"]
                animation["to_x"], animation["y"] = x, y
            if animation is None or animation.get("after_id") is None:
                self._set_geometry_if_changed(window,
                    f"{px(TOOLBAR_BUTTON_WIDTH, self._ui_scale)}x{px(TOOLBAR_BUTTON_HEIGHT, self._ui_scale)}+{x}+{y}"
                )
            return
        if animation is not None and animation.get("after_id") is not None:
            try:
                window.after_cancel(animation["after_id"])
            except self._tk.TclError:
                pass
        work_left, _work_top, work_right, _work_bottom = tuple(
            int(value) for value in anchor["work_area"]
        )
        from_x = max(
            work_left,
            min(work_right - px(TOOLBAR_BUTTON_WIDTH, self._ui_scale), x + px(30, self._ui_scale)),
        )
        window.geometry(
            f"{px(TOOLBAR_BUTTON_WIDTH, self._ui_scale)}x{px(TOOLBAR_BUTTON_HEIGHT, self._ui_scale)}+{from_x}+{y}"
        )
        animation = {
            "started_at": time.monotonic(),
            "from_x": from_x,
            "to_x": x,
            "y": y,
            "after_id": None,
        }
        self._toolbar_animations[kind] = animation
        animation["after_id"] = window.after(
            16,
            lambda item=kind: self._animate_toolbar(item),
        )

    def _animate_toolbar(self, kind: str) -> None:
        window = self._toolbar_window_for(kind)
        animation = self._toolbar_animations.get(kind)
        if (
            window is None
            or not window.winfo_exists()
            or window.state() == "withdrawn"
            or animation is None
        ):
            self._toolbar_animations.pop(kind, None)
            return
        progress = min(
            1.0,
            max(
                0.0,
                (time.monotonic() - animation["started_at"])
                / TOOLBAR_SLIDE_SECONDS,
            ),
        )
        eased = 1.0 - (1.0 - progress) ** 3
        x = int(
            round(
                animation["from_x"]
                + (animation["to_x"] - animation["from_x"]) * eased
            )
        )
        window.geometry(
            f"{px(TOOLBAR_BUTTON_WIDTH, self._ui_scale)}x{px(TOOLBAR_BUTTON_HEIGHT, self._ui_scale)}+{x}+{animation['y']}"
        )
        if progress < 1.0:
            animation["after_id"] = window.after(
                16,
                lambda item=kind: self._animate_toolbar(item),
            )
        else:
            self._toolbar_animations.pop(kind, None)

    def _build_history_surface(self, window: Any) -> None:
        tk = self._tk
        frame = popup_shell(tk, window, _CHAT_COLORS["card"], HISTORY_WIDTH, HISTORY_HEIGHT)
        header = tk.Frame(frame, background=_CHAT_COLORS["card"], padx=9, pady=13)
        header.pack(fill="x")
        header.bind("<ButtonPress-1>", self._begin_history_drag)
        header.bind("<B1-Motion>", self._drag_history_window)
        header.bind("<ButtonRelease-1>", self._end_history_drag)
        badge = tk.Canvas(header, width=36, height=36, background=_CHAT_COLORS["card"],
                           borderwidth=0, highlightthickness=0)
        _rounded_rectangle(badge, 1, 1, 35, 35, 12, fill=_CHAT_COLORS["accent_bg"], outline="")
        badge.create_line(10, 11, 26, 11, 26, 22, 18, 22, 13, 27, 13, 22, 10, 22, 10, 11,
                          fill=_CHAT_COLORS["accent"], width=1.5, joinstyle="round")
        badge.pack(side="left", padx=(0, 10))
        title_area = tk.Frame(header, background=_CHAT_COLORS["card"])
        title_area.pack(side="left", fill="x", expand=True)
        title = tk.Label(title_area, text="对话", background=_CHAT_COLORS["card"],
                         foreground=_CHAT_COLORS["text"], font=("Microsoft YaHei UI", 14, "bold"), anchor="w")
        title.pack(fill="x")
        subtitle = tk.Label(title_area, text=self.character_name, background=_CHAT_COLORS["card"],
                            foreground=_CHAT_COLORS["muted"], font=("Microsoft YaHei UI", 8), anchor="w")
        subtitle.pack(fill="x", pady=(3, 0))
        for widget in (badge, title_area, title, subtitle):
            widget.bind("<ButtonPress-1>", self._begin_history_drag)
            widget.bind("<B1-Motion>", self._drag_history_window)
            widget.bind("<ButtonRelease-1>", self._end_history_drag)
        self._make_canvas_button(
            header,
            "×",
            self._hide_history_window,
            width=29,
            height=29,
            fill=_CHAT_COLORS["history"],
            active_fill=_CHAT_COLORS["accent_bg"],
            foreground=_CHAT_COLORS["secondary"],
            font=("Microsoft YaHei UI", 13, "normal"),
        )[0].pack(side="right")

        surface, history_frame = rounded_surface(tk, frame, _CHAT_COLORS["history"], _CHAT_COLORS["card"], stretch=True)
        surface.pack(fill="both", expand=True, padx=4, pady=(10, 4))
        history = tk.Canvas(
            history_frame,
            background=_CHAT_COLORS["history"],
            borderwidth=0,
            highlightthickness=0,
        )
        scrollbar = ScrollThumb(tk, history_frame, history.yview, _CHAT_COLORS["history"])
        history.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        history.pack(side="left", fill="both", expand=True)
        self._history_canvas = history
        content = tk.Frame(history, background=_CHAT_COLORS["history"])
        self._history_content = content
        self._history_window_item = history.create_window(
            0, 0, anchor="nw", window=content
        )
        history.bind("<Configure>", self._resize_history_content)
        history.bind("<MouseWheel>", self._scroll_history)
        content.bind("<Configure>", self._update_history_scrollregion)
        content.bind("<MouseWheel>", self._scroll_history)
        for item in self._history_messages:
            self._render_message(item["role"], item["text"], item.get("created_at", ""))
        if not self._history_messages:
            self._render_message("system", "这里还没有聊天记录", "")

    def _show_history_window(self) -> None:
        if not self.config.enabled or not self._hover_state.visible:
            return
        tk = self._tk
        window = self._history_window
        if window is not None and window.winfo_exists():
            self._position_history_window()
            self._set_window_no_activate(window, enabled=True)
            window.deiconify()
            window.lift()
            return
        window = tk.Toplevel(self._root)
        window.withdraw()
        self._history_window = window
        window.title(f"{self.character_name} · 聊天记录")
        window.overrideredirect(True)
        window.resizable(False, False)
        window.configure(background=_CHAT_COLORS["card"])
        window.attributes("-topmost", bool((self._anchor or {}).get("topmost", True)))
        try:
            window.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self._set_window_no_activate(window, enabled=True)
        window.protocol("WM_DELETE_WINDOW", self._hide_history_window)
        window.bind("<Escape>", lambda _event: self._hide_history_window())
        self._enable_rounded_window_corners(window)
        self._build_history_surface(window)
        window._pet_scale = self._ui_scale
        scale_widgets(window, self._ui_scale)
        self._position_history_window()
        window.deiconify()
        window.lift()

    def _position_history_window(self) -> None:
        window = self._history_window
        anchor = self._anchor
        if window is None or not window.winfo_exists() or anchor is None:
            return
        try:
            window.attributes("-topmost", bool(anchor["topmost"]))
        except self._tk.TclError:
            pass
        window.update_idletasks()
        work_area = self._get_history_work_area()
        if work_area is None:
            work_area = tuple(int(value) for value in anchor["work_area"])
        if self._history_position is None:
            x, y = popup_position(
                anchor["pet_rect"], work_area,
                (px(HISTORY_WIDTH, self._ui_scale), px(HISTORY_HEIGHT, self._ui_scale)), self._ui_scale,
            )
            self._history_position = (x, y)
        else:
            x, y = clamp_window_position(
                self._history_position[0],
                self._history_position[1],
                px(HISTORY_WIDTH, self._ui_scale),
                px(HISTORY_HEIGHT, self._ui_scale),
                work_area,
                0,
            )
            self._history_position = (x, y)
        window.geometry(f"{px(HISTORY_WIDTH, self._ui_scale)}x{px(HISTORY_HEIGHT, self._ui_scale)}+{x}+{y}")

    def _hide_history_window(self) -> None:
        self._remember_history_position()
        window = self._history_window
        if window is not None and window.winfo_exists() and window.state() != "withdrawn":
            self._set_window_no_activate(window, enabled=True)
            window.withdraw()

    def _hide_session_windows(self) -> None:
        window = self._window
        if window is not None and window.winfo_exists() and window.state() != "withdrawn":
            self._set_window_no_activate(window, enabled=True)
            window.withdraw()
        for kind, toolbar in (
            ("settings", self._toolbar_window),
            ("history", self._history_button_window),
            ("speech", self._speech_button_window),
            ("proactive", self._proactive_button_window),
        ):
            if toolbar is not None and toolbar.winfo_exists():
                animation = self._toolbar_animations.get(kind)
                if animation is not None and animation.get("after_id") is not None:
                    try:
                        toolbar.after_cancel(animation["after_id"])
                    except self._tk.TclError:
                        pass
                if toolbar.state() != "withdrawn":
                    self._set_window_no_activate(toolbar, enabled=True)
                    toolbar.withdraw()
        self._toolbar_animations.clear()
        history = self._history_window
        if history is not None and history.winfo_exists() and history.state() != "withdrawn":
            self._remember_history_position()
            self._set_window_no_activate(history, enabled=True)
            history.withdraw()
        if self._parameter_panel is not None:
            panel_window = getattr(self._parameter_panel, "window", None)
            if (
                panel_window is not None
                and panel_window.winfo_exists()
                and panel_window.state() != "withdrawn"
            ):
                self._parameter_panel.hide()
        self._history_drag_start = None
        self._last_input_edit_at = None

    def _hide_window(self) -> None:
        self._cancel_microphone()
        self._hover_state.hide_until_pet_leave()
        self._hide_session_windows()

    def _note_input_activity(self, _event: Any = None) -> None:
        self._last_input_edit_at = time.monotonic()
        self._note_user_activity()

    def _note_user_activity(self, *, responded: bool = False) -> None:
        if responded:
            self._unanswered_proactive_count = 0
            self._proactive_interval = PROACTIVE_CHECK_SECONDS
            self._proactive_chance = PROACTIVE_CHANCE
        self._last_user_activity_at = time.monotonic()
        self._next_proactive_at = self._last_user_activity_at + self.config.proactive_idle_seconds
        self._user_activity_epoch += 1
        if self._proactive_inflight:
            self._proactive_cancel.set()
            self.backend.set_voice_enabled(self._voice_enabled)

    def _note_bot_activity(self, *, proactive: bool = False) -> None:
        if proactive:
            self._unanswered_proactive_count += 1
            if self._unanswered_proactive_count >= 3:
                self._proactive_interval *= 3
                self._proactive_chance *= 2 / 3
        self._last_bot_activity_at = time.monotonic()
        # Bot output has its own cooldown/backoff; only the user can restart
        # the five-minute silence window.
        self._next_proactive_at = max(self._next_proactive_at, self._last_bot_activity_at + self._proactive_interval)

    def _poll_proactive(self) -> None:
        now = time.monotonic()
        if now - self._last_proactive_poll < 1:
            return
        self._last_proactive_poll = now
        if not self._proactive_enabled or not self.config.enabled or self.config.transport != "core" or self._stopped.is_set():
            return
        speech_active = False
        if self._perception is not None:
            last_speech, speech_active = self._perception.speech_activity()
            if last_speech > self._last_user_activity_at:
                self._note_user_activity(responded=True)
        bot_ready_at = self._last_bot_activity_at + self._proactive_interval if self._last_bot_activity_at is not None else 0
        if now < max(self._next_proactive_at, self._last_user_activity_at + self.config.proactive_idle_seconds, bot_ready_at):
            return
        # Consume each due slot once, including blocked ones. Never catch up
        # missed slots or retry the random draw on the next UI frame.
        self._next_proactive_at = now + self._proactive_interval
        if (self._proactive_inflight or self._output_speaking
                or speech_active or self._pending_transcript is not None or self._continuous_transcripts
                or self._deferred_user_input is not None or not (self._anchor or {}).get("visible", False)):
            return
        if self._input_variable is not None and not self._placeholder_visible and self._input_variable.get().strip():
            return
        if not self._work_lock.acquire(blocking=False):
            return
        if random.random() >= self._proactive_chance:
            self._work_lock.release()
            return
        self._proactive_inflight = True
        self._proactive_cancel = threading.Event()
        threading.Thread(target=self._process_proactive, args=(self._user_activity_epoch, now - self._last_user_activity_at),
                         name="desktop-proactive-chat", daemon=True).start()

    def _process_proactive(self, epoch: int, idle_seconds: float) -> None:
        def cancelled() -> bool:
            if not self._proactive_enabled or self._stopped.is_set() or self._proactive_cancel.is_set() or epoch != self._user_activity_epoch:
                return True
            last_speech, active = self._perception.speech_activity() if self._perception is not None else (0, False)
            return active or last_speech > self._last_user_activity_at
        try:
            reply = self.backend.proactive(idle_seconds, include_audio=False,
                                           tts_language=self._tts_language, is_cancelled=cancelled)
            if reply is not None and not cancelled():
                self._ui_queue.put(("proactive_reply", (epoch, reply)))
        except Exception as exc:
            if not cancelled():
                self.logger.warning("[DesktopPetChat] proactive conversation failed: {}", type(exc).__name__)
        finally:
            self._work_lock.release()
            self._proactive_inflight = False
            self._ui_queue.put(("proactive_finished", None))

    def _toggle_voice(self) -> None:
        self._set_voice_enabled(not self._voice_enabled)
        self._append_message(
            "bot",
            "语音回答已开启。" if self._voice_enabled else "语音已关闭，我会继续用文字回复。"
        )

    def _set_voice_enabled(self, enabled: bool) -> None:
        self._voice_enabled = bool(enabled)
        set_backend_voice = getattr(self.backend, "set_voice_enabled", None)
        if set_backend_voice is not None:
            set_backend_voice(self._voice_enabled)
        if not self._voice_enabled:
            self.on_renderer_command("stop_audio", None)
            self.on_renderer_command("state", "finish_reply")
        self._ui_queue.put(("voice_state", None))

    def _cycle_language(self) -> None:
        current_index = self._TTS_LANGUAGE_ORDER.index(self._tts_language)
        self._tts_language = self._TTS_LANGUAGE_ORDER[
            (current_index + 1) % len(self._TTS_LANGUAGE_ORDER)
        ]
        self._refresh_voice_controls()
        self._append_message(
            "bot",
            f"语音语言已切换为{self._TTS_LANGUAGE_LABELS[self._tts_language]}。"
        )

    def _refresh_voice_controls(self) -> None:
        if self._voice_button is not None and self._voice_button.winfo_exists():
            background_id, text_id = self._voice_button_items or (0, 0)
            self._voice_button.itemconfigure(
                background_id,
                fill=_CHAT_COLORS["accent_bg"] if self._voice_enabled else _CHAT_COLORS["history"],
            )
            self._voice_button.itemconfigure(
                text_id,
                text="声音 · 开" if self._voice_enabled else "声音 · 关",
                fill=(
                    _CHAT_COLORS["accent_dark"]
                    if self._voice_enabled
                    else _CHAT_COLORS["secondary"]
                ),
            )
        if self._language_button is not None and self._language_button.winfo_exists():
            _background_id, text_id = self._language_button_items or (0, 0)
            self._language_button.itemconfigure(
                text_id,
                text=f"语言 · {self._TTS_LANGUAGE_LABELS[self._tts_language]}",
            )

    def _position_window(self) -> None:
        window = self._window
        anchor = self._anchor
        if window is None or not window.winfo_exists() or anchor is None:
            return
        topmost = bool(anchor["topmost"])
        if getattr(window, "_pet_topmost", None) != topmost:
            try:
                window.attributes("-topmost", topmost)
                window._pet_topmost = topmost
            except self._tk.TclError:
                pass
        x, y = composer_below_pet_position(
            tuple(int(value) for value in anchor["pet_rect"]),
            tuple(int(value) for value in anchor["work_area"]),
            (px(DOCK_WIDTH, self._ui_scale), px(DOCK_HEIGHT, self._ui_scale)),
            gap=px(DOCK_GAP, self._ui_scale),
        )
        self._set_geometry_if_changed(window, f"{px(DOCK_WIDTH, self._ui_scale)}x{px(DOCK_HEIGHT, self._ui_scale)}+{x}+{y}")

    @staticmethod
    def _set_geometry_if_changed(window: Any, geometry: str) -> None:
        if getattr(window, "_pet_geometry", None) != geometry:
            window.geometry(geometry)
            window._pet_geometry = geometry

    def _resize_history_content(self, event: Any) -> None:
        canvas = self._history_canvas
        item = self._history_window_item
        if canvas is not None and item is not None:
            canvas.itemconfigure(item, width=max(1, int(event.width)))

    def _update_history_scrollregion(self, _event: Any = None) -> None:
        if self._history_canvas is not None:
            self._history_canvas.configure(
                scrollregion=self._history_canvas.bbox("all")
            )
            self._history_canvas.after_idle(
                lambda: self._history_canvas.yview_moveto(1.0)
                if self._history_canvas is not None
                else None
            )

    def _scroll_history(self, event: Any) -> str:
        if self._history_canvas is not None:
            delta = int(getattr(event, "delta", 0))
            if delta:
                self._history_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def _submit(self) -> None:
        if self._input_variable is None or self._placeholder_visible:
            return
        text = self._input_variable.get().strip()
        self._submit_text(text, clear_input=True)

    def _submit_text(self, text: str, *, clear_input: bool = False) -> None:
        """Typed and transcribed input share one normal conversation path."""
        text = text.strip()
        if not text:
            self._set_result("请先输入内容。")
            return
        if len(text) > self.config.max_input_chars:
            self._set_result(f"内容不能超过 {self.config.max_input_chars} 个字符。")
            return
        self._note_user_activity(responded=True)
        if not self._work_lock.acquire(blocking=False):
            if self._proactive_inflight and self._deferred_user_input is None:
                self._deferred_user_input = (text, clear_input)
                return
            self._set_result("上一条还在处理中，请稍等。")
            return
        if clear_input and self._input_variable is not None:
            self._input_variable.set("")
        self._append_message("user", text)
        self._set_result(f"{self.character_name}正在输入……")
        threading.Thread(
            target=self._process_input,
            args=(text,),
            name="nachobot-live2d-chat-request",
            daemon=True,
        ).start()

    def _process_input(self, text: str) -> None:
        started_at = time.monotonic()
        progress_stop = threading.Event()

        def report_progress() -> None:
            while not progress_stop.wait(1.0):
                elapsed = int(time.monotonic() - started_at)
                self._ui_queue.put(
                    ("result", f"{self.character_name}正在想……已等待 {elapsed} 秒")
                )

        progress_thread = threading.Thread(
            target=report_progress,
            name="nachobot-live2d-chat-progress",
            daemon=True,
        )
        progress_thread.start()
        try:
            result = self._execute(text)
            message = result.text
            elapsed = max(0.0, time.monotonic() - started_at)
            if result.audio or result.audio_pending:
                self._ui_queue.put(
                    (
                        "result",
                        f"已回答 · {elapsed:.1f} 秒 · "
                        f"{'语音正在播放' if result.audio else '语音准备中'}",
                    )
                )
            else:
                self._ui_queue.put(("result", f"已回答 · {elapsed:.1f} 秒"))
        except DesktopPetBackendError as exc:
            self.on_renderer_command("emotion", "sorrow")
            self.on_renderer_command("state", "finish_reply")
            message = str(exc)
            self._ui_queue.put(("result", "暂时没有收到回复"))
        except Exception as exc:
            self.logger.exception("Desktop chat request failed")
            message = f"处理失败：{exc}"
            self._ui_queue.put(("result", "处理失败"))
        finally:
            progress_stop.set()
            progress_thread.join(timeout=0.2)
            self._work_lock.release()
        self._ui_queue.put(("message", ("bot", message)))

    def _execute(self, text: str) -> ChatReply:
        """Send every submitted string to the normal conversation backend."""

        return self._ask(text)

    def _ask(self, text: str) -> ChatReply:
        self.on_renderer_command("state", "start_thinking")
        reply = self.backend.ask(
            text,
            include_audio=self._voice_enabled,
            tts_language=self._tts_language,
        )
        self._present_reply(reply)
        return reply

    def _present_reply(self, reply: ChatReply) -> None:
        self.on_renderer_command("emotion", reply.emotion or "joy")
        if reply.action:
            self.on_renderer_command("canonical_action", reply.action)
        if reply.audio and self._voice_enabled:
            self.on_renderer_command("play_chat_audio", reply.audio)
        elif not reply.audio_pending:
            self.on_renderer_command("state", "finish_reply")

    def _set_result(self, text: str) -> None:
        widget = self._result_widget
        if widget is None or not widget.winfo_exists():
            return
        display_text = text.strip() or "……"
        if len(display_text) > 360:
            display_text = display_text[:357].rstrip() + "…"
        widget.configure(text=display_text)

    def _append_message(self, role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        created_at = datetime.now().strftime("%H:%M")
        if role in {"user", "bot"}:
            self._history_messages.append(
                {"role": role, "text": text, "created_at": created_at}
            )
            self._history_messages = self._history_messages[
                -self.config.max_history_messages :
            ]
            try:
                save_chat_history(
                    self.config.history_path,
                    self._history_messages,
                    self.config.max_history_messages,
                )
            except OSError as exc:
                self.logger.warning("Could not save desktop chat history: {}", exc)
            if self._history_placeholder is not None:
                try:
                    self._history_placeholder.destroy()
                except self._tk.TclError:
                    pass
                self._history_placeholder = None
        self._render_message(role, text, created_at)

    def _render_message(self, role: str, text: str, created_at: str) -> None:
        content = self._history_content
        if content is None or not content.winfo_exists():
            return
        tk = self._tk
        if role not in {"user", "bot"}:
            placeholder = tk.Label(
                content,
                text=text,
                background=_CHAT_COLORS["history"],
                foreground=_CHAT_COLORS["muted"],
                font=("Microsoft YaHei UI", 8),
                wraplength=HISTORY_WIDTH - 70,
                justify="center",
            )
            placeholder.pack(fill="x", padx=8, pady=8)
            if role == "system":
                self._history_placeholder = placeholder
            self._update_history_scrollregion()
            return

        is_user = role == "user"
        row = tk.Frame(content, background=_CHAT_COLORS["history"])
        row.pack(fill="x", padx=14, pady=(12, 2))
        meta = f"我  {created_at}" if is_user else f"{self.character_name}  {created_at}"
        tk.Label(
            row,
            text=meta.rstrip(),
            background=_CHAT_COLORS["history"],
            foreground=_CHAT_COLORS["muted"],
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="e" if is_user else "w", padx=5, pady=(0, 6))

        bubble_color = (
            _CHAT_COLORS["user_bubble"] if is_user else _CHAT_COLORS["assistant_bubble"]
        )
        bubble_border = ""
        bubble = tk.Canvas(
            row,
            background=_CHAT_COLORS["history"],
            borderwidth=0,
            highlightthickness=0,
        )
        label = tk.Label(
            bubble,
            text=text,
            background=bubble_color,
            foreground=_CHAT_COLORS["text"],
            font=("Microsoft YaHei UI", 10),
            wraplength=HISTORY_WIDTH - 120,
            justify="left",
            anchor="w",
        )
        label.update_idletasks()
        width = min(HISTORY_WIDTH - 100, max(64, int(label.winfo_reqwidth()) + 28))
        label.configure(wraplength=width - 28)
        label.update_idletasks()
        height = max(42, int(label.winfo_reqheight()) + 24)
        bubble.configure(width=width, height=height)
        _rounded_rectangle(
            bubble,
            1,
            1,
            width - 1,
            height - 1,
            14,
            fill=bubble_color,
            outline=bubble_border,
            width=1,
        )
        bubble.create_window(
            width / 2,
            height / 2,
            window=label,
            width=width - 28,
            anchor="center",
        )
        bubble.pack(anchor="e" if is_user else "w", padx=2)
        scale_widgets(row, self._ui_scale)
        self._update_history_scrollregion()
