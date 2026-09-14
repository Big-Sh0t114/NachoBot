"""Double-click chat window and local NachoBot backend integration."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPResponse
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from .config import DesktopChatConfig

RendererCommand = Callable[[str, Any], None]

DOCK_WIDTH = 440
DOCK_GAP = 6
# The renderer uses physical pixels after enabling Windows DPI awareness while
# Tk reports logical sizes during startup. Reserve enough room for the dock at
# common 125%-175% display scaling values.
DOCK_RESERVED_HEIGHT = 430


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
        y = max(work_top, min(work_bottom - height, pet_bottom - height))
    return int(x), int(y)


class DesktopPetBackendError(RuntimeError):
    """A user-readable error returned by the local desktop chat backend."""


@dataclass(frozen=True, slots=True)
class ChatReply:
    text: str
    audio: bytes = b""
    emotion: str | None = None
    action: str | None = None


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
            if int(latest_status.get("replies", 0) or 0) > before_replies:
                reply = str(latest_status.get("latest_reply") or "").strip()
                if reply:
                    return ChatReply(
                        reply,
                        (
                            self._fetch_audio(latest_status, before_audio_version)
                            if include_audio
                            else b""
                        ),
                        str(latest_status.get("latest_emotion") or "") or None,
                        str(latest_status.get("latest_action") or "") or None,
                    )
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
    """Own a docked Tk input bar without blocking the Live2D render loop."""

    HELP_TEXT = (
        "普通文字：交给 NachoBot 回答\n"
        "/说 内容：直接朗读内容\n"
        "/闭嘴：只显示文字；/开口：恢复 TTS 和口型\n"
        "/语言 自动|中文|日语|英语：设置 TTS 语言\n"
        "/动作 开心|点头|摇头|挥手|害羞\n"
        "/表情 开心|害羞|生气|惊讶|悲伤|正常\n"
        "/打开 记事本|计算器|文件管理器\n"
        "/置顶  /穿透  /隐藏  /复位  /帮助"
    )

    _MOTIONS = {
        "开心": "FlickUp",
        "点头": "FlickDown",
        "摇头": "Flick",
        "挥手": "Tap",
        "害羞": "Tap@Body",
    }
    _EMOTIONS = {
        "开心": "joy",
        "高兴": "joy",
        "害羞": "shy",
        "生气": "angry",
        "惊讶": "fear",
        "悲伤": "sorrow",
        "难过": "sorrow",
        "正常": "normal",
    }
    _APPLICATIONS = {
        "记事本": ["notepad.exe"],
        "计算器": ["calc.exe"],
        "文件管理器": ["explorer.exe"],
    }
    _TTS_LANGUAGES = {
        "自动": "auto",
        "auto": "auto",
        "中文": "zh",
        "汉语": "zh",
        "zh": "zh",
        "日语": "ja",
        "日文": "ja",
        "ja": "ja",
        "英语": "en",
        "英文": "en",
        "en": "en",
    }
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
    ) -> None:
        self.config = config
        self.title = title
        self.on_renderer_command = on_renderer_command
        self.logger = logger
        self.backend = backend or DesktopPetBackendClient(config)
        self._ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._work_lock = threading.Lock()
        self._root = None
        self._window = None
        self._input_variable = None
        self._result_widget = None
        self._history_widget = None
        self._entry_widget = None
        self._anchor: dict[str, Any] | None = None
        self._last_anchor_request: tuple[Any, ...] | None = None
        self._user_hidden = False
        self._voice_enabled = bool(config.play_audio)
        self._tts_language = config.tts_language
        self._voice_button = None
        self._language_button = None
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

    def open(self) -> None:
        self.start()
        self._ui_queue.put(("show", True))

    def show(self) -> None:
        """Show the dock without stealing keyboard focus during startup."""

        self.start()
        self._ui_queue.put(("show", False))

    def sync_anchor(
        self,
        pet_rect: tuple[int, int, int, int],
        work_area: tuple[int, int, int, int],
        *,
        visible: bool,
        topmost: bool,
        click_through: bool,
    ) -> None:
        """Move the input bar with the Live2D window from the renderer thread."""

        request = (
            tuple(int(value) for value in pet_rect),
            tuple(int(value) for value in work_area),
            bool(visible),
            bool(topmost),
            bool(click_through),
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
                },
            )
        )

    def stop(self) -> None:
        self._ui_queue.put(("stop", None))
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def execute_for_test(self, text: str, *, direct: bool = False) -> str:
        """Exercise command routing without constructing a Tk window."""

        return self._execute(text, direct=direct).text

    def _run_ui(self) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            root.withdraw()
            self._root = root
            self._tk = tk
            self._ttk = ttk
            self._ready.set()
            root.after(50, self._pump_ui_queue)
            root.mainloop()
        except Exception as exc:
            self._ready.set()
            self.logger.warning("Desktop chat window unavailable: {}", exc)
        finally:
            self._root = None
            self._window = None

    def _pump_ui_queue(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                action, value = self._ui_queue.get_nowait()
                if action == "show":
                    self._user_hidden = False
                    self._show_window(focus=bool(value))
                elif action == "anchor":
                    self._anchor = dict(value)
                    self._apply_anchor()
                elif action == "result":
                    self._set_result(str(value))
                elif action == "message":
                    role, text = value
                    self._append_message(str(role), str(text))
                elif action == "voice_state":
                    self._refresh_voice_controls()
                elif action == "stop":
                    root.quit()
                    return
        except queue.Empty:
            pass
        root.after(50, self._pump_ui_queue)

    def _show_window(self, *, focus: bool) -> None:
        tk = self._tk
        if self._window is not None and self._window.winfo_exists():
            self._window.deiconify()
            self._window.lift()
            self._apply_anchor()
            if focus and self._entry_widget is not None:
                self._entry_widget.focus_force()
            return

        window = tk.Toplevel(self._root)
        self._window = window
        window.title(f"和 {self.title} 对话")
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        try:
            window.attributes("-alpha", 0.97)
            window.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        window.resizable(False, False)
        window.configure(background="#26334d")
        window.protocol("WM_DELETE_WINDOW", self._hide_window)
        window.bind("<Escape>", lambda _event: self._hide_window())

        frame = tk.Frame(
            window,
            background="#fff8eb",
            highlightbackground="#26334d",
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        frame.pack(fill="both", expand=True)

        header = tk.Frame(frame, background="#fff8eb")
        header.pack(fill="x")
        tk.Label(
            header,
            text="桃濑日和  ·  NachoBot",
            background="#fff8eb",
            foreground="#34435f",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        tk.Button(
            header,
            text="—",
            command=self._hide_window,
            background="#fff8eb",
            foreground="#7f6570",
            activebackground="#f4dfdc",
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="right")

        status = tk.Label(
            frame,
            text="",
            anchor="w",
            justify="left",
            wraplength=DOCK_WIDTH - 38,
            background="#fff8eb",
            foreground="#5b4a52",
            font=("Microsoft YaHei UI", 9),
            padx=1,
            pady=3,
        )
        status.pack(fill="x")
        self._result_widget = status

        history_frame = tk.Frame(frame, background="#fff8eb")
        history_frame.pack(fill="both", expand=True, pady=(1, 7))
        history = tk.Text(
            history_frame,
            height=9,
            width=1,
            wrap="word",
            state="disabled",
            relief="flat",
            borderwidth=0,
            background="#fff8eb",
            foreground="#34435f",
            font=("Microsoft YaHei UI", 9),
            padx=5,
            pady=4,
            cursor="arrow",
        )
        scrollbar = tk.Scrollbar(
            history_frame,
            orient="vertical",
            command=history.yview,
            relief="flat",
            borderwidth=0,
        )
        history.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        history.pack(side="left", fill="both", expand=True)
        history.tag_configure(
            "bot_meta", justify="left", foreground="#98818a", spacing1=3
        )
        history.tag_configure(
            "bot_bubble",
            justify="left",
            foreground="#34435f",
            background="#f2dfd8",
            lmargin1=3,
            lmargin2=3,
            rmargin=48,
            spacing3=7,
        )
        history.tag_configure(
            "user_meta", justify="right", foreground="#98818a", spacing1=3
        )
        history.tag_configure(
            "user_bubble",
            justify="right",
            foreground="#ffffff",
            background="#65728c",
            lmargin1=48,
            lmargin2=48,
            rmargin=3,
            spacing3=7,
        )
        history.tag_configure(
            "system", justify="center", foreground="#9a7b86", spacing1=3, spacing3=5
        )
        self._history_widget = history
        if self._history_messages:
            for item in self._history_messages:
                self._render_message(
                    item["role"], item["text"], item.get("created_at", "")
                )
        else:
            self._render_message(
                "bot",
                "我在呢。普通文字会交给 NachoBot Core 回答，也会记住这段对话。",
                "",
            )

        input_row = tk.Frame(
            frame,
            background="#f5e4dc",
            highlightbackground="#d996a7",
            highlightthickness=1,
            padx=7,
            pady=5,
        )
        input_row.pack(fill="x")
        self._input_variable = tk.StringVar()
        entry = tk.Entry(
            input_row,
            textvariable=self._input_variable,
            width=1,
            relief="flat",
            borderwidth=0,
            background="#fffaf4",
            foreground="#26334d",
            insertbackground="#26334d",
            font=("Microsoft YaHei UI", 10),
        )
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        entry.bind("<Return>", lambda _event: self._submit(False))
        entry.bind("<Control-Return>", lambda _event: self._submit(True))
        self._entry_widget = entry
        tk.Button(
            input_row,
            text="发送",
            command=lambda: self._submit(False),
            background="#34435f",
            foreground="#ffffff",
            activebackground="#d9869e",
            activeforeground="#ffffff",
            relief="flat",
            borderwidth=0,
            padx=13,
            pady=4,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="right", padx=(7, 0))
        controls = tk.Frame(frame, background="#fff8eb")
        controls.pack(fill="x", pady=(3, 0))
        tk.Label(
            controls,
            text="回车聊天 · Ctrl+回车直接说",
            background="#fff8eb",
            foreground="#98818a",
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")
        self._language_button = tk.Button(
            controls,
            command=self._cycle_language,
            background="#fff8eb",
            foreground="#5f6f8e",
            activebackground="#f4dfdc",
            relief="flat",
            borderwidth=0,
            padx=4,
            font=("Microsoft YaHei UI", 8),
            cursor="hand2",
        )
        self._language_button.pack(side="right")
        self._voice_button = tk.Button(
            controls,
            command=self._toggle_voice,
            background="#fff8eb",
            foreground="#5f6f8e",
            activebackground="#f4dfdc",
            relief="flat",
            borderwidth=0,
            padx=4,
            font=("Microsoft YaHei UI", 8, "bold"),
            cursor="hand2",
        )
        self._voice_button.pack(side="right")
        tk.Button(
            controls,
            text="说明",
            command=lambda: self._append_message("bot", self.HELP_TEXT),
            background="#fff8eb",
            foreground="#5f6f8e",
            activebackground="#f4dfdc",
            relief="flat",
            borderwidth=0,
            padx=4,
            font=("Microsoft YaHei UI", 8),
            cursor="hand2",
        ).pack(side="right")

        self._refresh_voice_controls()
        self._set_result("NachoBot Core 聊天 · 就绪")
        self._apply_anchor()
        if focus:
            entry.focus_force()

    def _hide_window(self) -> None:
        self._user_hidden = True
        if self._window is not None and self._window.winfo_exists():
            self._window.withdraw()

    def _toggle_voice(self) -> None:
        self._set_voice_enabled(not self._voice_enabled)
        self._append_message(
            "bot",
            "TTS 语音和口型已恢复。" if self._voice_enabled else "闭嘴模式：继续文字回答，不播放语音。"
        )

    def _set_voice_enabled(self, enabled: bool) -> None:
        self._voice_enabled = bool(enabled)
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
            f"TTS 语言：{self._TTS_LANGUAGE_LABELS[self._tts_language]}。"
        )

    def _refresh_voice_controls(self) -> None:
        if self._voice_button is not None and self._voice_button.winfo_exists():
            self._voice_button.configure(
                text="声音：开" if self._voice_enabled else "闭嘴中",
                foreground="#5f6f8e" if self._voice_enabled else "#b35e78",
            )
        if self._language_button is not None and self._language_button.winfo_exists():
            self._language_button.configure(
                text=f"语言：{self._TTS_LANGUAGE_LABELS[self._tts_language]}"
            )

    def _apply_anchor(self) -> None:
        window = self._window
        anchor = self._anchor
        if window is None or not window.winfo_exists() or anchor is None:
            return
        should_show = bool(anchor["visible"] and not anchor["click_through"])
        if not should_show or self._user_hidden:
            window.withdraw()
            return
        try:
            window.attributes("-topmost", bool(anchor["topmost"]))
        except self._tk.TclError:
            pass
        window.update_idletasks()
        height = max(102, window.winfo_reqheight())
        x, y = docked_window_position(
            anchor["pet_rect"],
            anchor["work_area"],
            (DOCK_WIDTH, height),
        )
        window.geometry(f"{DOCK_WIDTH}x{height}+{x}+{y}")
        window.deiconify()

    def _submit(self, direct: bool) -> None:
        if self._input_variable is None:
            return
        text = self._input_variable.get().strip()
        if not text:
            self._set_result("请先输入内容。")
            return
        if len(text) > self.config.max_input_chars:
            self._set_result(f"内容不能超过 {self.config.max_input_chars} 个字符。")
            return
        if not self._work_lock.acquire(blocking=False):
            self._set_result("上一条还在处理中，请稍等。")
            return
        self._input_variable.set("")
        self._append_message("user", text)
        self._set_result("日和正在输入……")
        threading.Thread(
            target=self._process_input,
            args=(text, direct),
            name="nachobot-live2d-chat-request",
            daemon=True,
        ).start()

    def _process_input(self, text: str, direct: bool) -> None:
        try:
            result = self._execute(text, direct=direct)
            message = result.text
            if result.audio:
                self._ui_queue.put(("result", "已回答 · 语音正在播放"))
            else:
                self._ui_queue.put(("result", "已回答"))
        except DesktopPetBackendError as exc:
            self.on_renderer_command("emotion", "sorrow")
            self.on_renderer_command("state", "finish_reply")
            message = str(exc)
            self._ui_queue.put(("result", "NachoBot Core 回答失败"))
        except Exception as exc:
            self.logger.exception("Desktop chat request failed")
            message = f"处理失败：{exc}"
            self._ui_queue.put(("result", "处理失败"))
        finally:
            self._work_lock.release()
        self._ui_queue.put(("message", ("bot", message)))

    def _execute(self, text: str, *, direct: bool) -> ChatReply:
        if direct:
            return self._speak(text)
        if not text.startswith("/"):
            return self._ask(text)

        command, _, argument = text[1:].partition(" ")
        command = command.strip().casefold()
        argument = argument.strip()
        if command in {"帮助", "help", "?"}:
            return ChatReply(self.HELP_TEXT)
        if command in {"问", "ask"}:
            if not argument:
                raise DesktopPetBackendError("用法：/问 你的问题")
            return self._ask(argument)
        if command in {"说", "speak"}:
            if not argument:
                raise DesktopPetBackendError("用法：/说 要朗读的内容")
            return self._speak(argument)
        if command in {"闭嘴", "mute"}:
            self._set_voice_enabled(False)
            return ChatReply("闭嘴模式已开启：我会继续用文字回答。")
        if command in {"开口", "unmute"}:
            self._set_voice_enabled(True)
            return ChatReply("TTS 语音和口型已恢复。")
        if command in {"语言", "language", "lang"}:
            language = self._TTS_LANGUAGES.get(argument.casefold())
            if language is None:
                raise DesktopPetBackendError("用法：/语言 自动|中文|日语|英语")
            self._tts_language = language
            self._ui_queue.put(("voice_state", None))
            return ChatReply(f"TTS 语言已设为：{self._TTS_LANGUAGE_LABELS[language]}")
        if command in {"动作", "motion"}:
            if not argument:
                raise DesktopPetBackendError("用法：/动作 开心|点头|摇头|挥手|害羞")
            motion = self._MOTIONS.get(argument, argument)
            self.on_renderer_command("desktop_motion", motion)
            return ChatReply(f"已执行动作：{argument}")
        if command in {"表情", "emotion"}:
            if not argument:
                raise DesktopPetBackendError("用法：/表情 开心|害羞|生气|惊讶|悲伤|正常")
            emotion = self._EMOTIONS.get(argument, argument)
            self.on_renderer_command("emotion", emotion)
            return ChatReply(f"已切换表情：{argument}")
        renderer_commands = {
            "置顶": ("desktop_toggle_topmost", "已切换置顶状态。"),
            "穿透": ("desktop_toggle_click_through", "已切换鼠标穿透，可从托盘恢复。"),
            "隐藏": ("desktop_toggle_visibility", "桌宠已隐藏，可从托盘重新显示。"),
            "复位": ("desktop_reset_position", "桌宠已回到默认位置。"),
        }
        if command in renderer_commands:
            renderer_command, reply = renderer_commands[command]
            self.on_renderer_command(renderer_command, None)
            return ChatReply(reply)
        if command in {"打开", "open"}:
            application = self._APPLICATIONS.get(argument)
            if application is None:
                raise DesktopPetBackendError("只允许打开：记事本、计算器、文件管理器。")
            subprocess.Popen(application)
            return ChatReply(f"已打开：{argument}")
        raise DesktopPetBackendError(f"未知命令：/{command}。输入 /帮助 查看可用命令。")

    def _ask(self, text: str) -> ChatReply:
        self.on_renderer_command("state", "start_thinking")
        reply = self.backend.ask(
            text,
            include_audio=self._voice_enabled,
            tts_language=self._tts_language,
        )
        self._present_reply(reply)
        return reply

    def _speak(self, text: str) -> ChatReply:
        if not self._voice_enabled:
            raise DesktopPetBackendError("当前是闭嘴模式；点“闭嘴中”或输入 /开口 后再朗读。")
        self.on_renderer_command("state", "start_replying")
        reply = self.backend.announce(
            text,
            include_audio=True,
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
        else:
            self.on_renderer_command("state", "finish_reply")

    def _set_result(self, text: str) -> None:
        widget = self._result_widget
        if widget is None or not widget.winfo_exists():
            return
        display_text = text.strip() or "……"
        if len(display_text) > 360:
            display_text = display_text[:357].rstrip() + "…"
        widget.configure(text=display_text)
        self._apply_anchor()

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
        self._render_message(role, text, created_at)
        self._apply_anchor()

    def _render_message(self, role: str, text: str, created_at: str) -> None:
        widget = self._history_widget
        if widget is None or not widget.winfo_exists():
            return
        widget.configure(state="normal")
        if role == "user":
            label = f"我  {created_at}".rstrip()
            widget.insert("end", label + "\n", "user_meta")
            widget.insert("end", text + "\n", "user_bubble")
        elif role == "bot":
            label = f"日和  {created_at}".rstrip()
            widget.insert("end", label + "\n", "bot_meta")
            widget.insert("end", text + "\n", "bot_bubble")
        else:
            widget.insert("end", text + "\n", "system")
        widget.configure(state="disabled")
        widget.see("end")
