"""Physical keyboard and mouse press counters for the Sprite desktop pet.

The module is intentionally inert at import time. A counter starts a Windows
Raw Input keyboard window and mouse hook only after :meth:`InputCounter.start`.
"""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .windows_raw_keyboard import (
    RawKeyboardTransition,
    WindowsRawKeyboard,
    WM_KEYDOWN as RAW_WM_KEYDOWN,
    WM_KEYUP as RAW_WM_KEYUP,
    WM_SYSKEYDOWN as RAW_WM_SYSKEYDOWN,
    WM_SYSKEYUP as RAW_WM_SYSKEYUP,
)

WH_MOUSE_LL = 14
WM_QUIT = 0x0012

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10
LLKHF_EXTENDED = 0x01
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02

_SAVE_INTERVAL_SECONDS = 3.0
_START_TIMEOUT_SECONDS = 5.0
_JOIN_TIMEOUT_SECONDS = 2.0

_ORPHANED_CALLBACKS: list[tuple[Any, ...]] = []
_ORPHANED_CALLBACKS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class InputCounts:
    """Numeric aggregates only; no key or pointer data is retained."""

    keyboard_presses: int = 0
    mouse_clicks: int = 0

    @property
    def total(self) -> int:
        return self.keyboard_presses + self.mouse_clicks


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt", _Point),
        ("lPrivate", ctypes.c_uint32),
    ]


class _MouseHookData(ctypes.Structure):
    _fields_ = [
        ("pt", _Point),
        ("mouseData", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _WindowsHookApi:
    """Small, explicitly typed Win32 surface used on the counter worker."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows input listeners are available only on Windows")

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.CallNextHookEx.restype = ctypes.c_ssize_t
        self.user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        self.user32.UnhookWindowsHookEx.restype = ctypes.c_int
        self.user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self.user32.GetMessageW.restype = ctypes.c_int
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(_Message),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.user32.PeekMessageW.restype = ctypes.c_int
        self.user32.PeekMessageW.argtypes = [
            ctypes.POINTER(_Message),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.user32.PostThreadMessageW.restype = ctypes.c_int
        self.user32.PostThreadMessageW.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        self.user32.TranslateMessage.restype = ctypes.c_int
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(_Message)]
        self.user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(_Message)]
        self.kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        self.kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        self.user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self.user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]

        callback_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        self.callback_type = callback_factory(
            ctypes.c_ssize_t,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        self._module = self.kernel32.GetModuleHandleW(None)
        if not self._module:
            raise ctypes.WinError(ctypes.get_last_error())
        self.thread_id = int(self.kernel32.GetCurrentThreadId())
        self._raw_keyboard: WindowsRawKeyboard | None = None

    def prepare_message_queue(self) -> None:
        # GetCurrentThreadId does not create a queue. PeekMessage does, which
        # guarantees PostThreadMessageW can wake this thread during shutdown.
        message = _Message()
        self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)

    def make_callback(self, callback: Callable[..., int]) -> Any:
        return self.callback_type(callback)

    def set_mouse_hook(self, callback: Any) -> int:
        handle = self.user32.SetWindowsHookExW(
            WH_MOUSE_LL,
            ctypes.cast(callback, ctypes.c_void_p),
            self._module,
            0,
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    def call_next(self, hook: int | None, code: int, w_param: int, l_param: int) -> int:
        return int(self.user32.CallNextHookEx(hook, code, w_param, l_param))

    def start_raw_keyboard(
        self,
        event_handler: Callable[[RawKeyboardTransition], None],
        error_handler: Callable[[], None],
    ) -> None:
        listener = WindowsRawKeyboard()
        listener.start(event_handler, error_handler)
        self._raw_keyboard = listener

    def stop_raw_keyboard(self) -> None:
        listener = self._raw_keyboard
        if listener is None:
            return
        listener.close()
        self._raw_keyboard = None

    def message_loop(self) -> None:
        message = _Message()
        while True:
            status = int(self.user32.GetMessageW(ctypes.byref(message), None, 0, 0))
            if status == 0:
                return
            if status < 0:
                raise ctypes.WinError(ctypes.get_last_error())
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))

    def post_quit(self) -> bool:
        return bool(self.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0))

    def unhook(self, handle: int) -> bool:
        return bool(self.user32.UnhookWindowsHookEx(handle))


class InputCounter:
    """Count physical key presses and mouse button clicks without recording input."""

    def __init__(
        self,
        state_path: Path | None,
        logger: Any,
        *,
        api_factory: Callable[[], Any] | None = None,
        game_session_factory: Callable[[Any], Any] | None = None,
        save_interval_seconds: float = _SAVE_INTERVAL_SECONDS,
    ) -> None:
        self.state_path = (
            Path(state_path).expanduser().resolve().with_name("input_counts.json")
            if state_path is not None
            else None
        )
        self.logger = logger
        self._api_factory = api_factory
        self._game_session_factory = game_session_factory
        self._save_interval_seconds = max(0.0, float(save_interval_seconds))
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._game_api_lock = threading.Lock()
        self._game_transition_lock = threading.Lock()
        self._keyboard_down: set[tuple[int, int, bool]] = set()
        self._mouse_down: set[tuple[str, int]] = set()
        self._keyboard_presses = 0
        self._mouse_clicks = 0
        self._baseline_total = 0
        self._generation = 0
        self._dirty = False
        self._loaded = False
        self._last_save_at = 0.0
        self._thread: threading.Thread | None = None
        self._api: Any = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._active = False
        self._startup_error: BaseException | None = None
        self._cancel_start = threading.Event()
        self._callback_errors = 0
        self._keyboard_callback: Any = None
        self._mouse_callback: Any = None
        self._mouse_hook: int | None = None
        self._unhook_failed = False
        self._counter_closed = False
        self._counter_stopping = False
        self._game_session: Any = None
        self._game_generation = 0
        self._game_mode_state = "off"
        self._game_mode_error: str | None = None
        self._game_local_suspended = False
        self._game_helper_ready = False
        self._game_prepared_generation = -1
        self._game_failure_generation = -1
        self._game_message_session: str | None = None
        self._game_last_sequence = 0
        self._game_helper_totals = (0, 0)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active or bool(
                not self._counter_closed
                and self._game_local_suspended
                and self._game_helper_ready
                and self._game_mode_state in {"active", "stopping"}
            )

    def snapshot(self) -> InputCounts:
        with self._lock:
            return InputCounts(self._keyboard_presses, self._mouse_clicks)

    @property
    def total(self) -> int:
        return self.snapshot().total

    @property
    def baseline_total(self) -> int:
        """Aggregate loaded before listeners were enabled for this start."""

        with self._lock:
            return self._baseline_total

    @property
    def game_input_available(self) -> bool:
        """Whether this counter can offer the optional Windows game helper."""

        return self._game_session_factory is not None or os.name == "nt"

    @property
    def game_mode_state(self) -> str:
        with self._lock:
            return self._game_mode_state

    @property
    def game_mode_error(self) -> str | None:
        with self._lock:
            return self._game_mode_error

    def start(self) -> bool:
        """Load aggregate state and start both listeners on one bounded worker."""

        with self._lock:
            if self._counter_stopping:
                return False
            if self._counter_closed:
                self._counter_closed = False
                self._game_generation += 1
                self._game_session = None
                self._game_mode_state = "off"
                self._game_mode_error = None
                self._game_local_suspended = False
                self._game_helper_ready = False
        return self._start_local_listener(reset_baseline=True)

    def _start_local_listener(
        self,
        *,
        reset_baseline: bool,
        expected_generation: int | None = None,
    ) -> bool:
        """Start the ordinary listener without resetting state during a source switch."""

        with self._lifecycle_lock:
            with self._lock:
                if self._counter_closed or self._counter_stopping:
                    return False
                if (
                    expected_generation is not None
                    and expected_generation != self._game_generation
                ):
                    return False
                if expected_generation is None and (
                    self._game_session is not None
                    or self._game_mode_state in {"pending", "active", "stopping"}
                ):
                    return self._active
                thread = self._thread
                if thread is not None and thread.is_alive():
                    return self._active
            if self._unhook_failed:
                self.logger.warning(
                    "[InputCounter] prior listener cleanup failed; listener will remain disabled"
                )
                return False
            if os.name != "nt" and self._api_factory is None:
                self.logger.warning(
                    "[InputCounter] Windows input listeners are unavailable on this platform"
                )
                return False
            if reset_baseline:
                self._load_state()
                with self._lock:
                    self._baseline_total = self._keyboard_presses + self._mouse_clicks
            self._ready = threading.Event()
            self._startup_error = None
            self._cancel_start.clear()
            self._callback_errors = 0
            self._active = False
            self._api = None
            self._thread_id = None
            self._thread = threading.Thread(
                target=self._input_thread_main,
                name="NachoBotInputCounter",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception as exc:
                self._thread = None
                self.logger.warning("[InputCounter] input thread could not start: {}", exc)
                return False

            if not self._ready.wait(_START_TIMEOUT_SECONDS):
                self.logger.warning("[InputCounter] input listener startup timed out")
                self._cancel_start.set()
                self._request_thread_stop()
                self._thread.join(_JOIN_TIMEOUT_SECONDS)
                return False
            if self._startup_error is not None:
                self._thread.join(_JOIN_TIMEOUT_SECONDS)
                return False
            with self._lock:
                stale = self._counter_closed or self._counter_stopping or (
                    expected_generation is not None
                    and expected_generation != self._game_generation
                )
            if stale:
                self._cancel_start.set()
                self._request_thread_stop()
                self._thread.join(_JOIN_TIMEOUT_SECONDS)
                return False
            return self.active

    def pump_save(self, *, now: float | None = None) -> None:
        """Persist changed aggregates at a bounded interval from the render loop."""

        current = time.monotonic() if now is None else float(now)
        self._pump_game_session()
        with self._lock:
            due = current - self._last_save_at >= self._save_interval_seconds
        self._report_callback_errors()
        if due:
            self._save_state()

    def enable_game_mode(self) -> bool:
        """Request the isolated elevated helper without pausing ordinary capture."""

        with self._lock:
            if self._counter_closed or self._counter_stopping:
                return False
            if self._game_mode_state == "failed" and self._game_session is not None:
                return False
            if self._game_mode_state in {"pending", "active"}:
                return True
            if self._game_mode_state == "stopping":
                return False
            try:
                if self._game_session_factory is not None:
                    session = self._game_session_factory(self.logger)
                else:
                    from .game_input_helper import GameInputSession

                    session = GameInputSession(self.logger)
            except Exception as exc:
                self._game_mode_state = "failed"
                self._game_mode_error = self._sanitized_game_error(exc)
                self.logger.warning(
                    "[InputCounter] game input helper could not be created: {}",
                    self._game_mode_error,
                )
                return False
            self._game_generation += 1
            generation = self._game_generation
            self._game_session = session
            self._game_mode_state = "pending"
            self._game_mode_error = None
            self._game_local_suspended = False
            self._game_helper_ready = False
            self._game_prepared_generation = -1
            self._game_failure_generation = -1
            self._game_message_session = None
            self._game_last_sequence = 0
            self._game_helper_totals = (0, 0)
        try:
            started = bool(session.start())
        except Exception as exc:
            self._begin_game_failure(generation, session, self._sanitized_game_error(exc))
            return False
        if not started:
            self._begin_game_failure(
                generation,
                session,
                self._sanitized_game_error(getattr(session, "error", None) or "helper start failed"),
            )
            return False
        return True

    def disable_game_mode(self) -> None:
        """Stop the helper, drain its final totals, and resume ordinary capture."""

        with self._lock:
            if self._counter_closed or self._game_mode_state == "off":
                return
            if self._game_mode_state == "stopping":
                return
            self._game_generation += 1
            generation = self._game_generation
            session = self._game_session
            self._game_mode_state = "stopping"
            self._game_prepared_generation = -1
        if session is not None:
            try:
                session.request_stop()
            except Exception as exc:
                self.logger.warning(
                    "[InputCounter] game input helper stop request failed: {}",
                    self._sanitized_game_error(exc),
                )
        self._schedule_game_transition("disable", generation, session)

    def stop(self) -> None:
        """Request message-loop shutdown, join for a bounded time, and flush counts."""

        with self._lock:
            if self._counter_stopping:
                return
            self._game_generation += 1
            generation = self._game_generation
            self._counter_closed = True
            self._counter_stopping = True
            session = self._game_session
            if session is not None:
                self._game_mode_state = "stopping"
        if session is not None:
            try:
                session.request_stop()
            except Exception as exc:
                self.logger.warning(
                    "[InputCounter] game input helper stop request failed: {}",
                    self._sanitized_game_error(exc),
                )
        self._stop_local_listener()
        if session is not None:
            try:
                with self._game_api_lock:
                    messages = session.close(timeout=3.0)
                self._consume_game_messages(session, messages, generation)
            except Exception as exc:
                self.logger.warning(
                    "[InputCounter] game input helper cleanup failed: {}",
                    self._sanitized_game_error(exc),
                )
        self._report_callback_errors()
        self._save_state()
        with self._lock:
            if generation == self._game_generation:
                self._game_session = None
                self._game_mode_state = "off"
                self._game_mode_error = None
                self._game_local_suspended = False
                self._game_helper_ready = False
                self._counter_stopping = False

    def _stop_local_listener(self) -> bool:
        """Stop and join only the local listeners, preserving aggregate state."""

        with self._lifecycle_lock:
            self._cancel_start.set()
            self._request_thread_stop()
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(_JOIN_TIMEOUT_SECONDS)
                if thread.is_alive():
                    self.logger.warning(
                        "[InputCounter] input listener thread did not stop within {:.1f}s",
                        _JOIN_TIMEOUT_SECONDS,
                    )
                    return False
            return True

    def _pump_game_session(self) -> None:
        with self._lock:
            session = self._game_session
            generation = self._game_generation
            state = self._game_mode_state
        if session is None or state in {"off", "failed"}:
            return
        messages: tuple[Any, ...] = ()
        if self._game_api_lock.acquire(blocking=False):
            try:
                messages = tuple(session.poll())
            except Exception as exc:
                self._begin_game_failure(
                    generation,
                    session,
                    self._sanitized_game_error(exc),
                )
                return
            finally:
                self._game_api_lock.release()
        self._consume_game_messages(session, messages, generation)
        with self._lock:
            if session is not self._game_session:
                return
            current_state = self._game_mode_state
        helper_state = getattr(session, "state", None)
        if helper_state == "failed":
            reason = getattr(session, "error", None) or "helper failed"
            self._begin_game_failure(
                generation,
                session,
                self._sanitized_game_error(reason),
            )
        elif helper_state == "prepared":
            self._handle_game_prepared(generation, session)
        elif helper_state == "off" and current_state not in {"off", "stopping"}:
            self._begin_game_failure(generation, session, "helper stopped unexpectedly")

    def _consume_game_messages(
        self,
        session: Any,
        messages: tuple[Any, ...] | list[Any],
        generation: int,
    ) -> None:
        should_switch = False
        failure: str | None = None
        failure_message = False
        with self._lock:
            if session is not self._game_session:
                return
            for message in messages:
                version = getattr(message, "version", None)
                session_id = getattr(message, "session", None)
                sequence = getattr(message, "sequence", None)
                status = getattr(message, "status", None)
                keyboard = getattr(message, "keyboard", None)
                mouse = getattr(message, "mouse", None)
                if (
                    type(version) is not int
                    or version != 1
                    or type(session_id) is not str
                    or not session_id
                    or len(session_id) > 256
                    or type(sequence) is not int
                    or sequence <= 0
                    or type(status) is not str
                    or status not in {"prepared", "ready", "snapshot", "final", "failed"}
                    or type(keyboard) is not int
                    or keyboard < 0
                    or type(mouse) is not int
                    or mouse < 0
                ):
                    failure = "invalid helper update"
                    break
                if self._game_message_session is None:
                    self._game_message_session = session_id
                elif self._game_message_session != session_id:
                    failure = "helper session changed"
                    break
                if sequence <= self._game_last_sequence:
                    continue
                old_keyboard, old_mouse = self._game_helper_totals
                if keyboard < old_keyboard or mouse < old_mouse:
                    failure = "helper totals moved backwards"
                    break
                self._game_last_sequence = sequence
                self._game_helper_totals = (keyboard, mouse)
                local_suspended = self._game_local_suspended
                if status == "prepared":
                    if keyboard != 0 or mouse != 0:
                        failure = "helper counted before source handoff"
                        break
                    if (
                        generation == self._game_generation
                        and self._game_mode_state == "pending"
                    ):
                        should_switch = True
                elif status == "ready":
                    if not local_suspended:
                        failure = "helper became ready before local capture stopped"
                        break
                    self._game_helper_ready = True
                    self._add_game_deltas_locked(
                        keyboard - old_keyboard,
                        mouse - old_mouse,
                    )
                    if self._game_mode_state == "pending":
                        self._game_mode_state = "active"
                elif status in {"snapshot", "final", "failed"}:
                    if not self._game_helper_ready:
                        if keyboard != 0 or mouse != 0:
                            failure = "helper counted before ready"
                            break
                    elif local_suspended:
                        self._add_game_deltas_locked(
                            keyboard - old_keyboard,
                            mouse - old_mouse,
                        )
                    else:
                        failure = "helper and local capture overlapped"
                        break
                    if status == "failed":
                        failure_message = True
            if failure is None and should_switch:
                if self._game_prepared_generation != generation:
                    self._game_prepared_generation = generation
                    should_switch = True
                else:
                    should_switch = False
        if failure is not None:
            self._begin_game_failure(generation, session, failure)
            return
        if failure_message:
            reason = getattr(session, "error", None) or "helper reported failure"
            self._begin_game_failure(
                generation,
                session,
                self._sanitized_game_error(reason),
            )
        elif should_switch:
            self._schedule_game_transition("switch", generation, session)

    def _add_game_deltas_locked(self, keyboard: int, mouse: int) -> None:
        if keyboard == 0 and mouse == 0:
            return
        self._keyboard_presses += keyboard
        self._mouse_clicks += mouse
        self._generation += 1
        self._dirty = True

    def _handle_game_prepared(self, generation: int, session: Any) -> None:
        with self._lock:
            if (
                self._counter_closed
                or generation != self._game_generation
                or session is not self._game_session
                or self._game_mode_state != "pending"
                or self._game_prepared_generation == generation
            ):
                return
            self._game_prepared_generation = generation
        self._schedule_game_transition("switch", generation, session)

    def _begin_game_failure(self, generation: int, session: Any, reason: str) -> None:
        with self._lock:
            if (
                self._counter_closed
                or generation != self._game_generation
                or session is not self._game_session
                or self._game_mode_state in {"off", "stopping"}
                or self._game_failure_generation == generation
            ):
                return
            self._game_failure_generation = generation
            self._game_mode_state = "stopping"
            self._game_mode_error = self._sanitized_game_error(reason)
        if session is not None:
            try:
                session.request_stop()
            except Exception as exc:
                self.logger.warning(
                    "[InputCounter] game input helper failure stop request failed: {}",
                    self._sanitized_game_error(exc),
                )
        self._schedule_game_transition("failure", generation, session)

    def _schedule_game_transition(self, action: str, generation: int, session: Any) -> None:
        thread = threading.Thread(
            target=self._run_game_transition,
            args=(action, generation, session),
            name=f"NachoBotGameInput-{action}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            with self._lock:
                if generation == self._game_generation and session is self._game_session:
                    self._game_mode_state = "failed"
                    self._game_mode_error = self._sanitized_game_error(exc)
            self.logger.warning(
                "[InputCounter] game input transition could not start: {}",
                self._sanitized_game_error(exc),
            )

    def _run_game_transition(self, action: str, generation: int, session: Any) -> None:
        with self._game_transition_lock:
            if action == "switch":
                with self._lock:
                    if (
                        self._counter_closed
                        or generation != self._game_generation
                        or session is not self._game_session
                        or self._game_mode_state != "pending"
                    ):
                        return
                stopped = self._stop_local_listener()
                activation_error: str | None = None
                with self._lock:
                    if stopped and session is self._game_session:
                        self._game_local_suspended = True
                    current = (
                        stopped
                        and not self._counter_closed
                        and generation == self._game_generation
                        and session is self._game_session
                        and self._game_mode_state == "pending"
                    )
                    if current:
                        try:
                            # Linearize START against disable/stop so a stale
                            # prepared handoff cannot activate after cancellation.
                            session.activate()
                        except Exception as exc:
                            activation_error = self._sanitized_game_error(exc)
                if not stopped:
                    self._begin_game_failure(
                        generation,
                        session,
                        "ordinary listener did not stop",
                    )
                    return
                if not current:
                    return
                if activation_error is not None:
                    self._begin_game_failure(
                        generation,
                        session,
                        activation_error,
                    )
                return

            cleanup_error: str | None = None
            if session is not None:
                try:
                    session.request_stop()
                    with self._game_api_lock:
                        messages = session.close(timeout=3.0)
                    self._consume_game_messages(session, messages, generation)
                except Exception as exc:
                    cleanup_error = self._sanitized_game_error(exc)
                    self.logger.warning(
                        "[InputCounter] game input helper cleanup failed: {}",
                        cleanup_error,
                    )
                    # A failed close must not strand the ordinary counter. Make
                    # one bounded best-effort STOP/close attempt, then detach
                    # this session before restoring local capture.
                    try:
                        session.request_stop()
                        with self._game_api_lock:
                            messages = session.close(timeout=0.25)
                        self._consume_game_messages(session, messages, generation)
                    except Exception as retry_exc:
                        self.logger.warning(
                            "[InputCounter] game input helper retry cleanup failed: {}",
                            self._sanitized_game_error(retry_exc),
                        )
            with self._lock:
                current = (
                    not self._counter_closed
                    and generation == self._game_generation
                    and session is self._game_session
                )
                if current:
                    self._game_session = None
            if not current:
                return
            resumed = self._resume_local_listener(generation)
            with self._lock:
                if (
                    self._counter_closed
                    or generation != self._game_generation
                ):
                    return
                self._game_local_suspended = False
                self._game_helper_ready = False
                if action == "disable" and resumed and cleanup_error is None:
                    self._game_mode_state = "off"
                    self._game_mode_error = None
                else:
                    self._game_mode_state = "failed"
                    if cleanup_error is not None:
                        self._game_mode_error = cleanup_error
                    if not resumed and self._game_mode_error is None:
                        self._game_mode_error = "ordinary listener could not be resumed"

    def _resume_local_listener(self, generation: int) -> bool:
        resumed = self._start_local_listener(
            reset_baseline=False,
            expected_generation=generation,
        )
        if resumed:
            return True
        with self._lock:
            thread = self._thread
            may_retry = (
                not self._counter_closed
                and not self._counter_stopping
                and generation == self._game_generation
                and not self._active
            )
        if not may_retry or thread is None or not thread.is_alive():
            return False
        thread.join(_JOIN_TIMEOUT_SECONDS)
        return self._start_local_listener(
            reset_baseline=False,
            expected_generation=generation,
        )

    @staticmethod
    def _sanitized_game_error(error: Any) -> str:
        value = str(error or "game input helper unavailable").strip()
        printable = "".join(character for character in value if character.isprintable())
        return printable[:160] or "game input helper unavailable"

    def record_keyboard_event(
        self,
        vk_code: int,
        message: int,
        flags: int = 0,
        scan_code: int = 0,
    ) -> None:
        """Record a physical key down edge; injected transitions do not alter edges."""

        if flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED):
            return
        key_code = int(vk_code)
        if key_code < 0:
            return
        key = (key_code, int(scan_code), bool(flags & LLKHF_EXTENDED))
        with self._lock:
            if message in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if key not in self._keyboard_down:
                    self._keyboard_down.add(key)
                    self._keyboard_presses += 1
                    self._generation += 1
                    self._dirty = True
            elif message in (WM_KEYUP, WM_SYSKEYUP):
                self._keyboard_down.discard(key)

    def record_mouse_event(self, message: int, flags: int = 0, mouse_data: int = 0) -> None:
        """Record one physical mouse button down edge; movement and wheel are ignored."""

        if flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED):
            return
        transitions = {
            WM_LBUTTONDOWN: ("left", 0, True),
            WM_LBUTTONUP: ("left", 0, False),
            WM_RBUTTONDOWN: ("right", 0, True),
            WM_RBUTTONUP: ("right", 0, False),
            WM_MBUTTONDOWN: ("middle", 0, True),
            WM_MBUTTONUP: ("middle", 0, False),
            WM_XBUTTONDOWN: ("x", (int(mouse_data) >> 16) & 0xFFFF, True),
            WM_XBUTTONUP: ("x", (int(mouse_data) >> 16) & 0xFFFF, False),
        }
        transition = transitions.get(int(message))
        if transition is None:
            return
        kind, detail, is_down = transition
        button = (kind, detail)
        with self._lock:
            if is_down:
                if button not in self._mouse_down:
                    self._mouse_down.add(button)
                    self._mouse_clicks += 1
                    self._generation += 1
                    self._dirty = True
            else:
                self._mouse_down.discard(button)

    def _input_thread_main(self) -> None:
        cleanup_failed = False
        try:
            self._api = (self._api_factory or _WindowsHookApi)()
            if self._cancel_start.is_set():
                return
            self._thread_id = int(getattr(self._api, "thread_id", threading.get_ident()))
            self._api.prepare_message_queue()
            if self._cancel_start.is_set():
                return
            self._keyboard_callback = self._on_raw_keyboard
            self._api.start_raw_keyboard(self._keyboard_callback, self._note_callback_error)
            if self._cancel_start.is_set():
                return
            self._mouse_callback = self._api.make_callback(self._on_mouse_hook)
            if self._cancel_start.is_set():
                return
            self._mouse_hook = self._api.set_mouse_hook(self._mouse_callback)
            if self._cancel_start.is_set():
                return
            with self._lock:
                self._active = True
            self._ready.set()
            self._api.message_loop()
        except BaseException as exc:
            self._startup_error = exc
            self.logger.warning("[InputCounter] Raw Input/mouse listener unavailable: {}", exc)
            self._ready.set()
        finally:
            api = self._api
            if api is not None:
                try:
                    api.stop_raw_keyboard()
                except Exception as exc:
                    cleanup_failed = True
                    self.logger.warning(
                        "[InputCounter] could not stop Raw Input keyboard listener: {}", exc
                    )
                mouse_hook = self._mouse_hook
                if mouse_hook is not None:
                    try:
                        if not api.unhook(mouse_hook):
                            cleanup_failed = True
                            self.logger.warning("[InputCounter] could not unhook mouse listener")
                    except Exception as exc:
                        cleanup_failed = True
                        self.logger.warning("[InputCounter] could not unhook mouse listener: {}", exc)
            self._mouse_hook = None
            if cleanup_failed:
                self._unhook_failed = True
                callbacks = tuple(
                    callback
                    for callback in (self._keyboard_callback, self._mouse_callback)
                    if callback is not None
                )
                with _ORPHANED_CALLBACKS_LOCK:
                    _ORPHANED_CALLBACKS.append(callbacks)
            else:
                self._keyboard_callback = None
                self._mouse_callback = None
            with self._lock:
                self._active = False
                self._keyboard_down.clear()
                self._mouse_down.clear()
            self._ready.set()

    def _on_raw_keyboard(self, event: RawKeyboardTransition) -> None:
        message_map = {
            RAW_WM_KEYDOWN: WM_KEYDOWN,
            RAW_WM_KEYUP: WM_KEYUP,
            RAW_WM_SYSKEYDOWN: WM_SYSKEYDOWN,
            RAW_WM_SYSKEYUP: WM_SYSKEYUP,
        }
        message = message_map.get(int(event.message))
        if message is None:
            self._note_callback_error()
            return
        self.record_keyboard_event(
            int(event.vk_code), message, int(event.flags), int(event.scan_code)
        )
        # Pause commonly has no matching break packet. Release its in-memory
        # edge immediately so its next press can be counted independently.
        if int(event.vk_code) == 0x13 and message in (WM_KEYDOWN, WM_SYSKEYDOWN):
            release = WM_SYSKEYUP if message == WM_SYSKEYDOWN else WM_KEYUP
            self.record_keyboard_event(
                int(event.vk_code), release, int(event.flags), int(event.scan_code)
            )

    def _on_mouse_hook(self, code: int, w_param: int, l_param: int) -> int:
        api = self._api
        if api is None:
            return 0
        result = api.call_next(self._mouse_hook, code, w_param, l_param)
        if code >= 0:
            try:
                data = ctypes.cast(
                    ctypes.c_void_p(int(l_param)),
                    ctypes.POINTER(_MouseHookData),
                ).contents
                self.record_mouse_event(int(w_param), int(data.flags), int(data.mouseData))
            except Exception:
                self._note_callback_error()
        return result

    def _request_thread_stop(self) -> None:
        thread = self._thread
        api = self._api
        if thread is None or not thread.is_alive() or api is None:
            return
        try:
            if not api.post_quit():
                self.logger.warning("[InputCounter] could not post WM_QUIT to input thread")
        except Exception as exc:
            self.logger.warning("[InputCounter] could not signal input thread: {}", exc)

    def _note_callback_error(self) -> None:
        with self._lock:
            self._callback_errors += 1

    def _report_callback_errors(self) -> None:
        with self._lock:
            errors = self._callback_errors
            self._callback_errors = 0
        if errors:
            self.logger.warning(
                "[InputCounter] {} input callbacks could not be decoded",
                errors,
            )

    def _load_state(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self.state_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or type(payload.get("version")) is not int
                or payload.get("version") != 1
                or type(payload.get("keyboard_presses")) is not int
                or type(payload.get("mouse_clicks")) is not int
                or payload["keyboard_presses"] < 0
                or payload["mouse_clicks"] < 0
                or set(payload) != {"version", "keyboard_presses", "mouse_clicks"}
            ):
                raise ValueError(
                    "input count state must contain only version and non-negative aggregates"
                )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.logger.warning(
                "[InputCounter] saved aggregate counts could not be loaded: {}",
                exc,
            )
            return
        with self._lock:
            self._keyboard_presses = payload["keyboard_presses"]
            self._mouse_clicks = payload["mouse_clicks"]
            self._generation += 1
            self._dirty = False

    def _save_state(self) -> None:
        path = self.state_path
        if path is None:
            return
        with self._save_lock:
            with self._lock:
                if not self._dirty:
                    self._last_save_at = time.monotonic()
                    return
                generation = self._generation
                payload = {
                    "version": 1,
                    "keyboard_presses": self._keyboard_presses,
                    "mouse_clicks": self._mouse_clicks,
                }
            temp_path: Path | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix=f".{path.stem}.",
                    suffix=".tmp",
                    dir=path.parent,
                    delete=False,
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                    json.dump(payload, temp_file, separators=(",", ":"))
                    temp_file.write("\n")
                    temp_file.flush()
                os.replace(temp_path, path)
                with self._lock:
                    if self._generation == generation:
                        self._dirty = False
                    self._last_save_at = time.monotonic()
            except (OSError, TypeError, ValueError) as exc:
                self.logger.warning("[InputCounter] aggregate counts could not be saved: {}", exc)
            finally:
                with self._lock:
                    self._last_save_at = time.monotonic()
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
