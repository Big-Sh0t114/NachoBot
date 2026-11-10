"""Read and cautiously manipulate the native Windows desktop scene.

This module is deliberately importable on non-Windows hosts.  It uses pywin32
for ordinary HWND operations (already a Windows adapter dependency) and calls
the documented Shell COM interfaces through ``ctypes`` for desktop icon access.
No COM object or HWND is touched at import time.

All methods are synchronous.  The adapter's scene worker should call them from
one dedicated thread; each COM operation initializes and uninitializes that
thread's apartment within the operation.
"""

from __future__ import annotations

import ctypes
import hashlib
import math
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeAlias

Rect: TypeAlias = tuple[int, int, int, int]
Point: TypeAlias = tuple[int, int]


@dataclass(frozen=True, slots=True)
class SceneWindow:
    hwnd: int
    pid: int
    process_name: str
    title: str
    rect: Rect
    z_order: int
    maximized: bool = False
    foreground: bool = False
    movable: bool = False


@dataclass(frozen=True, slots=True)
class DesktopIcon:
    """A desktop icon at its image-center in physical screen coordinates."""

    identity: str
    name: str
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class SceneSnapshot:
    captured_at: float
    windows: tuple[SceneWindow, ...]
    icons: tuple[DesktopIcon, ...]
    foreground_hwnd: int | None
    cursor: Point
    work_area: Rect
    system_idle_seconds: float
    fullscreen: bool


@dataclass(frozen=True, slots=True)
class SceneUnavailable:
    """Typed result for an unavailable platform or native scene facility."""

    reason: str
    detail: str


SceneSnapshotResult: TypeAlias = SceneSnapshot | SceneUnavailable


@dataclass(frozen=True, slots=True)
class WindowMoveReceipt:
    hwnd: int
    pid: int
    previous_rect: Rect
    requested_rect: Rect
    actual_rect: Rect
    moved_dx: int
    moved_dy: int


@dataclass(frozen=True, slots=True)
class IconMoveReceipt:
    identity: str
    name: str
    previous_position: Point
    requested_position: Point
    actual_position: Point
    moved_dx: int
    moved_dy: int


@dataclass(frozen=True, slots=True)
class _IconCache:
    name: str
    position: Point
    anchor: Point
    can_move: bool


@dataclass(frozen=True, slots=True)
class _IconRecovery:
    name: str
    original_anchor: Point
    last_anchor: Point


@dataclass(frozen=True, slots=True)
class _WindowRecovery:
    pid: int
    process_name: str
    title: str
    original_rect: Rect
    last_rect: Rect


class _NativeCallError(RuntimeError):
    def __init__(self, operation: str, code: int | None = None) -> None:
        self.operation = operation
        self.code = code
        suffix = f" (HRESULT 0x{code & 0xFFFFFFFF:08X})" if code is not None else ""
        super().__init__(f"{operation} failed{suffix}")


def _is_windows() -> bool:
    return sys.platform == "win32"


def _clamp_delta(dx: int, dy: int, max_delta_px: int) -> tuple[int, int] | None:
    """Bound the requested movement distance, with a hard cap against bad callers."""
    try:
        requested_cap = int(max_delta_px)
        requested_dx = int(dx)
        requested_dy = int(dy)
    except (TypeError, ValueError, OverflowError):
        return None
    if requested_cap <= 0:
        return None
    cap = min(requested_cap, 16)
    bounded_dx = max(-cap, min(cap, requested_dx))
    bounded_dy = max(-cap, min(cap, requested_dy))
    if bounded_dx * bounded_dx + bounded_dy * bounded_dy > cap * cap:
        scale = cap / math.hypot(bounded_dx, bounded_dy)
        bounded_dx = int(round(bounded_dx * scale))
        bounded_dy = int(round(bounded_dy * scale))
        while bounded_dx * bounded_dx + bounded_dy * bounded_dy > cap * cap:
            if abs(bounded_dx) >= abs(bounded_dy):
                bounded_dx -= 1 if bounded_dx > 0 else -1
            else:
                bounded_dy -= 1 if bounded_dy > 0 else -1
    return bounded_dx, bounded_dy


class DesktopSceneProvider:
    """Capture a desktop scene and make small, revalidated native adjustments.

    The caller should dedicate one daemon worker thread to this object.  No
    method activates, focuses, resizes, or reorders a target window. Recovery
    state is in-memory and records the original position for each uninterrupted
    sequence of provider moves. If the user or Shell relocates a target between
    moves, the next validated move starts a new restore sequence at that point.
    """

    def __init__(
        self,
        own_hwnd: int | None = None,
        own_pid: int | None = None,
        include_icons: bool = True,
    ) -> None:
        self.own_hwnd = int(own_hwnd) if own_hwnd else None
        self.own_pid = os.getpid() if own_pid is None else int(own_pid)
        self.include_icons = bool(include_icons)
        self.last_unavailable: SceneUnavailable | None = None
        self._closed = False
        self._icon_cache: dict[str, _IconCache] = {}
        self._icon_recovery: dict[str, _IconRecovery] = {}
        self._window_recovery: dict[int, _WindowRecovery] = {}

    def snapshot(self) -> SceneSnapshotResult:
        """Capture visible application windows and, optionally, desktop icons."""
        if self._closed:
            return self._unavailable("closed", "The desktop scene provider is closed.")
        if not _is_windows():
            return self._unavailable(
                "platform", "Native desktop scenes are available on Windows only."
            )
        try:
            with _physical_dpi_context():
                windows, foreground_hwnd = _capture_windows(self.own_hwnd, self.own_pid)
                cursor, work_area = _capture_cursor_and_work_area()
                idle_seconds = _system_idle_seconds()
                fullscreen = _is_fullscreen(windows)
                icons: tuple[DesktopIcon, ...] = ()
                next_cache: dict[str, _IconCache] = {}
                if self.include_icons:
                    with _com_initialized():
                        with _desktop_folder_view() as (view, folder):
                            icons, next_cache = _capture_icons(view, folder)
                self._icon_cache = next_cache
            self.last_unavailable = None
            return SceneSnapshot(
                captured_at=time.monotonic(),
                windows=windows,
                icons=icons,
                foreground_hwnd=foreground_hwnd,
                cursor=cursor,
                work_area=work_area,
                system_idle_seconds=idle_seconds,
                fullscreen=fullscreen,
            )
        except Exception as exc:  # native calls can fail due to session/access state
            return self._unavailable(_failure_reason(exc), _safe_detail(exc))

    def move_window(
        self,
        hwnd: int,
        pid: int,
        process_name: str,
        title: str,
        expected_rect: Rect,
        dx: int,
        dy: int,
        max_delta_px: int,
        action_allowed: Callable[[], bool] | None = None,
    ) -> WindowMoveReceipt | None:
        """Move one previously observed ordinary window by a bounded offset."""
        if not self._ready_for_mutation():
            return None
        bounded = _clamp_delta(dx, dy, max_delta_px)
        if bounded is None or bounded == (0, 0):
            return None
        try:
            with _physical_dpi_context():
                import win32con
                import win32gui
                import win32process

                hwnd = int(hwnd)
                pid = int(pid)
                if hwnd == self.own_hwnd or pid == self.own_pid:
                    return None
                expected = _rect(expected_rect)
                current_identity = _window_identity(hwnd, win32gui, win32process)
                if current_identity is None:
                    return None
                (
                    current_pid,
                    current_process_name,
                    current_title,
                    current_rect,
                    native_rect,
                    movable,
                ) = current_identity
                if (
                    current_pid != pid
                    or not _same_name(current_process_name, process_name)
                    or current_title != title
                    or current_rect != expected
                    or not movable
                ):
                    return None
                move_dx, move_dy = bounded
                requested = (
                    expected[0] + move_dx,
                    expected[1] + move_dy,
                    expected[2] + move_dx,
                    expected[3] + move_dy,
                )
                flags = (
                    win32con.SWP_NOSIZE
                    | win32con.SWP_NOZORDER
                    | win32con.SWP_NOACTIVATE
                    | getattr(win32con, "SWP_NOOWNERZORDER", 0x0200)
                )
                if not _action_is_allowed(action_allowed):
                    return None
                win32gui.SetWindowPos(
                    hwnd, 0, native_rect[0] + move_dx, native_rect[1] + move_dy, 0, 0, flags
                )
                actual = _window_rect(hwnd, win32gui)
                actual_native = _window_native_rect(hwnd, win32gui)
                if actual is None or actual_native is None:
                    return None
                cap = min(int(max_delta_px), 16)
                if (
                    actual[2] - actual[0] != expected[2] - expected[0]
                    or actual[3] - actual[1] != expected[3] - expected[1]
                    or math.hypot(actual[0] - expected[0], actual[1] - expected[1]) > cap
                ):
                    # A race or window-manager constraint must not turn the bounded
                    # request into a larger move. Restore the validated origin.
                    win32gui.SetWindowPos(hwnd, 0, native_rect[0], native_rect[1], 0, 0, flags)
                    return None
                recovery = self._window_recovery.get(hwnd)
                same_identity = bool(
                    recovery is not None
                    and recovery.pid == pid
                    and _same_name(recovery.process_name, current_process_name)
                    and recovery.title == current_title
                )
                original_rect = (
                    recovery.original_rect
                    if same_identity and recovery.last_rect == native_rect
                    else native_rect
                )
                recovery = _WindowRecovery(
                    pid=pid,
                    process_name=current_process_name,
                    title=current_title,
                    original_rect=original_rect,
                    last_rect=actual_native,
                )
                self._window_recovery[hwnd] = recovery
                self.last_unavailable = None
                return WindowMoveReceipt(
                    hwnd=hwnd,
                    pid=pid,
                    previous_rect=expected,
                    requested_rect=requested,
                    actual_rect=actual,
                    moved_dx=actual[0] - expected[0],
                    moved_dy=actual[1] - expected[1],
                )
        except Exception as exc:
            self._set_failure(exc)
            return None

    def move_icon(
        self,
        identity: str,
        expected_name: str,
        x: int,
        y: int,
        max_delta_px: int,
        action_allowed: Callable[[], bool] | None = None,
    ) -> IconMoveReceipt | None:
        """Move a cached desktop icon only when its identity/name/point still match."""
        if not self._ready_for_mutation():
            return None
        try:
            target = (int(x), int(y))
        except (TypeError, ValueError, OverflowError):
            return None
        identity = str(identity)
        cached = self._icon_cache.get(identity)
        if cached is None or not cached.can_move or not _same_name(cached.name, expected_name):
            return None
        dx, dy = target[0] - cached.position[0], target[1] - cached.position[1]
        bounded = _clamp_delta(dx, dy, max_delta_px)
        if bounded is None or bounded == (0, 0):
            return None
        try:
            with _physical_dpi_context(), _com_initialized():
                with _desktop_folder_view() as (view, folder):
                    for pidl in _iter_folder_items(view):
                        pidl_bytes = _pidl_bytes(pidl)
                        if _icon_identity(pidl_bytes) != identity:
                            continue
                        name = _item_name(folder, pidl, pidl_bytes)
                        if not _same_name(name, expected_name) or not _same_name(name, cached.name):
                            return None
                        anchor = _item_position(view, pidl)
                        current_center = _icon_center(anchor)
                        if current_center != cached.position:
                            return None
                        if _is_recycle_bin(pidl_bytes, name):
                            return None
                        if _auto_arrange_enabled(view):
                            # Auto-arranged views own item positions; do not fight
                            # the Shell or risk an unexpected grid relocation.
                            return None
                        move_dx, move_dy = bounded
                        requested_center = (
                            current_center[0] + move_dx,
                            current_center[1] + move_dy,
                        )
                        anchor_offset = (
                            current_center[0] - anchor[0],
                            current_center[1] - anchor[1],
                        )
                        requested_anchor = (
                            requested_center[0] - anchor_offset[0],
                            requested_center[1] - anchor_offset[1],
                        )
                        if not _action_is_allowed(action_allowed):
                            return None
                        _select_and_position(view, pidl, requested_anchor)
                        actual_anchor = _item_position(view, pidl)
                        actual_center = _icon_center(actual_anchor)
                        actual_dx = actual_center[0] - current_center[0]
                        actual_dy = actual_center[1] - current_center[1]
                        cap = min(int(max_delta_px), 16)
                        if math.hypot(actual_dx, actual_dy) > cap:
                            # Shell grid snapping can turn a tiny request into a large jump.
                            # Put the icon back before reporting that as a failed move.
                            _select_and_position(view, pidl, anchor)
                            return None
                        if actual_center == current_center:
                            return None
                        recovery = self._icon_recovery.get(identity)
                        same_identity = bool(
                            recovery is not None
                            and _same_name(recovery.name, name)
                        )
                        original_anchor = (
                            recovery.original_anchor
                            if same_identity and recovery.last_anchor == anchor
                            else anchor
                        )
                        recovery = _IconRecovery(name, original_anchor, actual_anchor)
                        self._icon_recovery[identity] = recovery
                        self._icon_cache[identity] = _IconCache(
                            name=name,
                            position=actual_center,
                            anchor=actual_anchor,
                            can_move=True,
                        )
                        self.last_unavailable = None
                        return IconMoveReceipt(
                            identity=identity,
                            name=name,
                            previous_position=current_center,
                            requested_position=requested_center,
                            actual_position=actual_center,
                            moved_dx=actual_dx,
                            moved_dy=actual_dy,
                        )
                    return None
        except Exception as exc:
            self._set_failure(exc)
            return None

    def restore_windows(self) -> int:
        """Restore windows this provider moved, skipping identities changed since."""
        if not self._ready_for_mutation() or not self._window_recovery:
            return 0
        try:
            with _physical_dpi_context():
                import win32con
                import win32gui
                import win32process

                restored = 0
                for hwnd, recovery in tuple(self._window_recovery.items()):
                    identity = _window_identity(hwnd, win32gui, win32process)
                    if identity is None:
                        continue
                    pid, process_name, title, _visual_rect, native_rect, _movable = identity
                    if (
                        pid != recovery.pid
                        or not _same_name(process_name, recovery.process_name)
                        or title != recovery.title
                        or native_rect != recovery.last_rect
                    ):
                        continue
                    flags = (
                        win32con.SWP_NOSIZE
                        | win32con.SWP_NOZORDER
                        | win32con.SWP_NOACTIVATE
                        | getattr(win32con, "SWP_NOOWNERZORDER", 0x0200)
                    )
                    win32gui.SetWindowPos(
                        hwnd, 0, recovery.original_rect[0], recovery.original_rect[1], 0, 0, flags
                    )
                    actual = _window_native_rect(hwnd, win32gui)
                    if actual == recovery.original_rect:
                        restored += 1
                        self._window_recovery.pop(hwnd, None)
                self.last_unavailable = None
                return restored
        except Exception as exc:
            self._set_failure(exc)
            return 0

    def restore_icons(self) -> int:
        """Restore desktop icons moved by this provider, preserving user changes."""
        if not self._ready_for_mutation() or not self._icon_recovery:
            return 0
        try:
            with _physical_dpi_context(), _com_initialized():
                with _desktop_folder_view() as (view, folder):
                    restored_ids: list[str] = []
                    restored_anchors: dict[str, Point] = {}
                    for pidl in _iter_folder_items(view):
                        pidl_bytes = _pidl_bytes(pidl)
                        identity = _icon_identity(pidl_bytes)
                        recovery = self._icon_recovery.get(identity)
                        if recovery is None:
                            continue
                        name = _item_name(folder, pidl, pidl_bytes)
                        if not _same_name(name, recovery.name):
                            continue
                        # Use the last observed point as the optimistic lock: don't
                        # overwrite a location change made by the user or the shell.
                        current_anchor = _item_position(view, pidl)
                        if current_anchor != recovery.last_anchor:
                            continue
                        _select_and_position(view, pidl, recovery.original_anchor)
                        if _item_position(view, pidl) == recovery.original_anchor:
                            restored_ids.append(identity)
                            restored_anchors[identity] = recovery.original_anchor
                    for identity in restored_ids:
                        self._icon_recovery.pop(identity, None)
                        cached = self._icon_cache.get(identity)
                        if cached is not None:
                            restored_anchor = restored_anchors[identity]
                            self._icon_cache[identity] = _IconCache(
                                name=cached.name,
                                position=_icon_center(restored_anchor),
                                anchor=restored_anchor,
                                can_move=cached.can_move,
                            )
                    self.last_unavailable = None
                    return len(restored_ids)
        except Exception as exc:
            self._set_failure(exc)
            return 0

    def close(self) -> None:
        """Restore our remaining moves, then close and discard recovery state."""
        if self._closed:
            return
        # The worker normally queues an explicit restore first. This is the
        # shutdown fallback if that queued operation was lost or skipped.
        self.restore_windows()
        self.restore_icons()
        self._closed = True
        self._icon_cache.clear()
        self._icon_recovery.clear()
        self._window_recovery.clear()

    def _ready_for_mutation(self) -> bool:
        if self._closed:
            self.last_unavailable = SceneUnavailable(
                "closed", "The desktop scene provider is closed."
            )
            return False
        if not _is_windows():
            self.last_unavailable = SceneUnavailable(
                "platform", "Native desktop scenes are available on Windows only."
            )
            return False
        return True

    def _unavailable(self, reason: str, detail: str) -> SceneUnavailable:
        result = SceneUnavailable(reason, detail)
        self.last_unavailable = result
        return result

    def _set_failure(self, exc: Exception) -> None:
        self.last_unavailable = SceneUnavailable(_failure_reason(exc), _safe_detail(exc))


def _safe_detail(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:300] if text else type(exc).__name__


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, _NativeCallError):
        return "native_call"
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "dependency"
    if isinstance(exc, OSError):
        return "windows_api"
    return "native_scene"


def _same_name(left: str, right: str) -> bool:
    return str(left).strip().casefold() == str(right).strip().casefold()


def _action_is_allowed(gate: Callable[[], bool] | None) -> bool:
    if gate is None:
        return True
    try:
        return bool(gate())
    except Exception:
        return False


def _rect(value: Rect) -> Rect:
    if len(value) != 4:
        raise ValueError("A window rectangle must have four coordinates.")
    return tuple(int(part) for part in value)  # type: ignore[return-value]


@contextmanager
def _physical_dpi_context() -> Iterator[None]:
    """Use physical coordinates on this worker thread, then restore its context."""
    if not _is_windows():
        yield
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    previous = None
    if setter is not None:
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        # PER_MONITOR_AWARE_V2, with the first per-monitor context as fallback.
        previous = setter(ctypes.c_void_p(-4))
        if not previous:
            previous = setter(ctypes.c_void_p(-3))
    try:
        yield
    finally:
        if setter is not None and previous:
            setter(previous)


def _capture_windows(
    own_hwnd: int | None, own_pid: int
) -> tuple[tuple[SceneWindow, ...], int | None]:
    import win32con
    import win32gui
    import win32process

    foreground_hwnd = int(win32gui.GetForegroundWindow() or 0) or None
    windows: list[SceneWindow] = []
    ordinal = 0

    def callback(hwnd: int, _context: object) -> bool:
        nonlocal ordinal
        z_order = ordinal
        ordinal += 1
        if own_hwnd and int(hwnd) == own_hwnd:
            return True
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        if _is_cloaked(hwnd):
            return True
        try:
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
            pid = int(pid)
            if pid == own_pid:
                return True
            title = str(win32gui.GetWindowText(hwnd) or "").strip()
            rect = _window_rect(hwnd, win32gui)
            if rect is None or rect[2] <= rect[0] or rect[3] <= rect[1]:
                return True
            class_name = str(win32gui.GetClassName(hwnd) or "")
            process_name = _process_name(pid)
            if _is_shell_window(class_name, process_name):
                return True
            maximized = _is_window_maximized(hwnd)
            ex_style = int(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE))
            movable = (
                bool(title)
                and bool(process_name)
                and not maximized
                and not (ex_style & win32con.WS_EX_TOOLWINDOW)
            )
            windows.append(
                SceneWindow(
                    hwnd=int(hwnd),
                    pid=pid,
                    process_name=process_name,
                    title=title,
                    rect=rect,
                    z_order=z_order,
                    maximized=maximized,
                    foreground=int(hwnd) == foreground_hwnd,
                    movable=movable,
                )
            )
        except Exception:
            # A window can disappear between enumeration and querying; skip it.
            return True
        return True

    win32gui.EnumWindows(callback, None)
    return tuple(windows), foreground_hwnd


def _window_native_rect(hwnd: int, win32gui: object) -> Rect | None:
    try:
        return tuple(int(value) for value in win32gui.GetWindowRect(hwnd))  # type: ignore[attr-defined,return-value]
    except Exception:
        return None


def _is_window_maximized(hwnd: int) -> bool:
    """Use user32 directly because some pywin32 builds omit IsZoomed."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    is_zoomed = user32.IsZoomed
    is_zoomed.argtypes = [ctypes.c_void_p]
    is_zoomed.restype = ctypes.c_int
    return bool(is_zoomed(ctypes.c_void_p(int(hwnd))))


def _window_rect(hwnd: int, win32gui: object) -> Rect | None:
    """Return the visible DWM frame, falling back to the native HWND rectangle."""
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        get_attribute = dwmapi.DwmGetWindowAttribute
        get_attribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        get_attribute.restype = ctypes.c_long
        rect = (ctypes.c_long * 4)()
        hr = int(get_attribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)))
        if hr >= 0:
            visual = tuple(int(value) for value in rect)
            if visual[2] > visual[0] and visual[3] > visual[1]:
                return visual  # type: ignore[return-value]
    except Exception:
        pass
    return _window_native_rect(hwnd, win32gui)


def _window_identity(
    hwnd: int,
    win32gui: object,
    win32process: object,
) -> tuple[int, str, str, Rect, Rect, bool] | None:
    if not win32gui.IsWindow(hwnd):  # type: ignore[attr-defined]
        return None
    _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)  # type: ignore[attr-defined]
    pid = int(pid)
    title = str(win32gui.GetWindowText(hwnd) or "").strip()  # type: ignore[attr-defined]
    visual_rect = _window_rect(hwnd, win32gui)
    native_rect = _window_native_rect(hwnd, win32gui)
    if (
        visual_rect is None
        or visual_rect[2] <= visual_rect[0]
        or visual_rect[3] <= visual_rect[1]
        or native_rect is None
        or native_rect[2] <= native_rect[0]
        or native_rect[3] <= native_rect[1]
    ):
        return None
    if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):  # type: ignore[attr-defined]
        return None
    if _is_cloaked(hwnd):
        return None
    process_name = _process_name(pid)
    class_name = str(win32gui.GetClassName(hwnd) or "")  # type: ignore[attr-defined]
    if _is_shell_window(class_name, process_name):
        return None
    ex_style = int(win32gui.GetWindowLong(hwnd, -20))  # GWL_EXSTYLE
    movable = (
        bool(title)
        and bool(process_name)
        and not _is_window_maximized(hwnd)
        and not (ex_style & 0x00000080)
    )
    return pid, process_name, title, visual_rect, native_rect, movable


def _process_name(pid: int) -> str:
    """Resolve an executable basename without requiring access to its memory."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    query_name = getattr(kernel32, "QueryFullProcessImageNameW", None)
    if query_name is None:
        return ""
    query_name.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    query_name.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    process = open_process(0x1000, 0, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint32(len(buffer))
        if not query_name(process, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value).casefold()
    finally:
        close_handle(process)


def _is_shell_window(class_name: str, process_name: str) -> bool:
    if process_name.casefold() == "explorer.exe":
        return True
    return class_name.casefold() in {
        "progman",
        "workerw",
        "shell_traywnd",
        "shell_secondarytraywnd",
        "dv2controlhost",
        "tasklistthumbnailwnd",
    }


def _is_cloaked(hwnd: int) -> bool:
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        get_attribute = dwmapi.DwmGetWindowAttribute
        get_attribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        get_attribute.restype = ctypes.c_long
        cloaked = ctypes.c_uint32(0)
        hr = int(get_attribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)))
        return hr < 0 or bool(cloaked.value)
    except Exception:
        return True


def _capture_cursor_and_work_area() -> tuple[Point, Rect]:
    import win32api
    import win32con

    cursor = tuple(int(value) for value in win32api.GetCursorPos())
    monitor = win32api.MonitorFromPoint(cursor, win32con.MONITOR_DEFAULTTONEAREST)
    info = win32api.GetMonitorInfo(monitor)
    work = tuple(int(value) for value in info["Work"])
    return cursor, work  # type: ignore[return-value]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


def _system_idle_seconds() -> float:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_last_input = user32.GetLastInputInfo
    get_last_input.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    get_last_input.restype = ctypes.c_int
    info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
    if not get_last_input(ctypes.byref(info)):
        return 0.0
    get_tick_count = kernel32.GetTickCount64
    get_tick_count.argtypes = []
    get_tick_count.restype = ctypes.c_uint64
    ticks = int(get_tick_count()) & 0xFFFFFFFF
    delta = (ticks - int(info.dwTime)) & 0xFFFFFFFF
    return delta / 1000.0


def _is_fullscreen(windows: tuple[SceneWindow, ...]) -> bool:
    import win32api
    import win32con

    for window in windows:
        monitor = win32api.MonitorFromWindow(window.hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(monitor)
        left, top, right, bottom = tuple(int(value) for value in info["Monitor"])
        wl, wt, wr, wb = window.rect
        if wl <= left + 1 and wt <= top + 1 and wr >= right - 1 and wb >= bottom - 1:
            return True
    return False


@contextmanager
def _com_initialized() -> Iterator[None]:
    if not _is_windows():
        raise _NativeCallError("COM is not available on this platform")
    ole32 = ctypes.OleDLL("ole32", use_last_error=True)
    initialize = ole32.CoInitializeEx
    initialize.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    initialize.restype = ctypes.c_long
    uninitialize = ole32.CoUninitialize
    uninitialize.argtypes = []
    uninitialize.restype = None
    # STA is required by the desktop Shell view. Every successful call, including
    # S_FALSE for an already initialized STA, must be balanced with CoUninitialize.
    hr = int(initialize(None, 0x2))  # COINIT_APARTMENTTHREADED
    if hr < 0:
        raise _NativeCallError("CoInitializeEx", hr)
    try:
        yield
    finally:
        uninitialize()


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    value = uuid.UUID(text)
    data1 = int.from_bytes(value.bytes_le[0:4], "little")
    data2 = int.from_bytes(value.bytes_le[4:6], "little")
    data3 = int.from_bytes(value.bytes_le[6:8], "little")
    return _GUID(data1, data2, data3, (ctypes.c_ubyte * 8).from_buffer_copy(value.bytes_le[8:16]))


_CLSID_SHELL_WINDOWS = "9BA05972-F6A8-11CF-A442-00A0C90A8F39"
_IID_ISHELL_WINDOWS = "85CB6900-4D95-11CF-960C-0080C7F4EE85"
_IID_IDISPATCH = "00020400-0000-0000-C000-000000000046"
_IID_ISERVICE_PROVIDER = "6D5140C1-7436-11CE-8034-00AA006009FA"
_SID_TOP_LEVEL_BROWSER = "4C96BE40-915C-11CF-99D3-00AA004AE837"
_IID_ISHELL_BROWSER = "000214E2-0000-0000-C000-000000000046"
_IID_ISHELL_VIEW = "000214E3-0000-0000-C000-000000000046"
_IID_IFOLDER_VIEW = "CDE725B0-CCC9-4519-917E-325D72FAB4CE"
_IID_ISHELL_FOLDER = "000214E6-0000-0000-C000-000000000046"
_IID_IENUM_IDLIST = "000214F2-0000-0000-C000-000000000046"


class _BRECORD(ctypes.Structure):
    """The VARIANT record arm fixes its native size/alignment on x64 Windows."""

    _fields_ = [("pvRecord", ctypes.c_void_p), ("pRecInfo", ctypes.c_void_p)]


class _VARIANT_UNION(ctypes.Union):
    _fields_ = [
        ("llVal", ctypes.c_int64),
        ("lVal", ctypes.c_int32),
        ("bstrVal", ctypes.c_void_p),
        ("punkVal", ctypes.c_void_p),
        ("pdispVal", ctypes.c_void_p),
        ("record", _BRECORD),
    ]


class _VARIANT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("reserved1", ctypes.c_uint16),
        ("reserved2", ctypes.c_uint16),
        ("reserved3", ctypes.c_uint16),
        ("data", _VARIANT_UNION),
    ]


class _STRRET_UNION(ctypes.Union):
    _fields_ = [
        ("pOleStr", ctypes.c_void_p),
        ("uOffset", ctypes.c_uint32),
        ("cStr", ctypes.c_char * 260),
    ]


class _STRRET(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("uType", ctypes.c_uint32), ("data", _STRRET_UNION)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


def _invoke(
    pointer: int | ctypes.c_void_p,
    slot: int,
    result_type: object,
    arg_types: list[object],
    *args: object,
) -> int:
    address = pointer.value if isinstance(pointer, ctypes.c_void_p) else int(pointer)
    table = ctypes.cast(
        ctypes.c_void_p(address), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    function_type = _FUNCTYPE(result_type, ctypes.c_void_p, *arg_types)
    function = function_type(table[slot])
    return function(ctypes.c_void_p(address), *args)


def _check_hr(hr: int, operation: str) -> None:
    if int(hr) < 0:
        raise _NativeCallError(operation, int(hr))


def _query_interface(pointer: int, iid_text: str, operation: str) -> int:
    iid = _guid(iid_text)
    result = ctypes.c_void_p()
    hr = _invoke(
        pointer,
        0,
        ctypes.c_long,
        [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
        ctypes.byref(iid),
        ctypes.byref(result),
    )
    _check_hr(hr, operation)
    if not result.value:
        raise _NativeCallError(operation)
    return int(result.value)


def _query_service(pointer: int, service_text: str, iid_text: str, operation: str) -> int:
    service = _guid(service_text)
    iid = _guid(iid_text)
    result = ctypes.c_void_p()
    hr = _invoke(
        pointer,
        3,
        ctypes.c_long,
        [ctypes.POINTER(_GUID), ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
        ctypes.byref(service),
        ctypes.byref(iid),
        ctypes.byref(result),
    )
    _check_hr(hr, operation)
    if not result.value:
        raise _NativeCallError(operation)
    return int(result.value)


def _release(pointer: int | None) -> None:
    if not pointer:
        return
    try:
        _invoke(pointer, 2, ctypes.c_uint32, [])
    except Exception:
        pass


@contextmanager
def _desktop_folder_view() -> Iterator[tuple[int, int]]:
    """Yield ``(IFolderView*, IShellFolder*)`` for the current desktop view."""
    ole32 = ctypes.OleDLL("ole32", use_last_error=True)
    create_instance = ole32.CoCreateInstance
    create_instance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    create_instance.restype = ctypes.c_long
    shell_windows = _guid(_CLSID_SHELL_WINDOWS)
    iid_shell_windows = _guid(_IID_ISHELL_WINDOWS)
    shell_ptr = ctypes.c_void_p()
    hr = create_instance(
        ctypes.byref(shell_windows),
        None,
        0x17,
        ctypes.byref(iid_shell_windows),
        ctypes.byref(shell_ptr),
    )  # CLSCTX_ALL
    _check_hr(hr, "CoCreateInstance(CLSID_ShellWindows)")
    pointers: list[int] = [int(shell_ptr.value)]
    try:
        location = _VARIANT(vt=3, lVal=0)  # VT_I4 / CSIDL_DESKTOP
        root = _VARIANT(vt=0)  # VT_EMPTY
        desktop_dispatch = ctypes.c_void_p()
        shell_hwnd = ctypes.c_int32(0)
        hr = _invoke(
            int(shell_ptr.value),
            15,
            ctypes.c_long,
            [
                ctypes.POINTER(_VARIANT),
                ctypes.POINTER(_VARIANT),
                ctypes.c_int32,
                ctypes.POINTER(ctypes.c_int32),
                ctypes.c_int32,
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.byref(location),
            ctypes.byref(root),
            8,
            ctypes.byref(shell_hwnd),
            1,
            ctypes.byref(desktop_dispatch),
        )  # FindWindowSW, SWC_DESKTOP, SWFO_NEEDDISPATCH
        _check_hr(hr, "IShellWindows.FindWindowSW")
        if not desktop_dispatch.value:
            raise _NativeCallError("IShellWindows.FindWindowSW")
        pointers.append(int(desktop_dispatch.value))
        service = _query_interface(
            int(desktop_dispatch.value), _IID_ISERVICE_PROVIDER, "QueryInterface(IServiceProvider)"
        )
        pointers.append(service)
        browser = _query_service(
            service,
            _SID_TOP_LEVEL_BROWSER,
            _IID_ISHELL_BROWSER,
            "IServiceProvider.QueryService(IShellBrowser)",
        )
        pointers.append(browser)
        shell_view_out = ctypes.c_void_p()
        hr = _invoke(
            browser,
            15,
            ctypes.c_long,
            [ctypes.POINTER(ctypes.c_void_p)],
            ctypes.byref(shell_view_out),
        )
        _check_hr(hr, "IShellBrowser.QueryActiveShellView")
        if not shell_view_out.value:
            raise _NativeCallError("IShellBrowser.QueryActiveShellView")
        shell_view = int(shell_view_out.value)
        pointers.append(shell_view)
        folder_view = _query_interface(shell_view, _IID_IFOLDER_VIEW, "QueryInterface(IFolderView)")
        pointers.append(folder_view)
        folder_out = ctypes.c_void_p()
        iid_shell_folder = _guid(_IID_ISHELL_FOLDER)
        hr = _invoke(
            folder_view,
            5,
            ctypes.c_long,
            [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
            ctypes.byref(iid_shell_folder),
            ctypes.byref(folder_out),
        )
        _check_hr(hr, "IFolderView.GetFolder(IShellFolder)")
        if not folder_out.value:
            raise _NativeCallError("IFolderView.GetFolder(IShellFolder)")
        folder = int(folder_out.value)
        pointers.append(folder)
        yield folder_view, folder
    finally:
        for pointer in reversed(pointers):
            _release(pointer)


def _iter_folder_items(view: int) -> Iterator[int]:
    iid = _guid(_IID_IENUM_IDLIST)
    enum_out = ctypes.c_void_p()
    hr = _invoke(
        view,
        8,
        ctypes.c_long,
        [ctypes.c_int32, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
        2,
        ctypes.byref(iid),
        ctypes.byref(enum_out),
    )  # Items(SVGIO_ALLVIEW, IID_IEnumIDList)
    _check_hr(hr, "IFolderView.Items")
    if not enum_out.value:
        raise _NativeCallError("IFolderView.Items")
    enumerator = int(enum_out.value)
    try:
        count = 0
        while count < 4096:
            item = ctypes.c_void_p()
            fetched = ctypes.c_uint32(0)
            hr = _invoke(
                enumerator,
                3,
                ctypes.c_long,
                [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)],
                1,
                ctypes.byref(item),
                ctypes.byref(fetched),
            )
            _check_hr(hr, "IEnumIDList.Next")
            if fetched.value == 0 or not item.value:
                break
            count += 1
            pointer = int(item.value)
            try:
                yield pointer
            finally:
                _co_task_mem_free(pointer)
    finally:
        _release(enumerator)


def _co_task_mem_free(pointer: int | None) -> None:
    if not pointer:
        return
    ole32 = ctypes.OleDLL("ole32", use_last_error=True)
    free = ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    free(ctypes.c_void_p(pointer))


def _pidl_bytes(pointer: int) -> bytes:
    offset = 0
    while offset < 16384:
        cb = ctypes.c_uint16.from_address(pointer + offset).value
        if cb == 0:
            return ctypes.string_at(pointer, offset + 2)
        if cb < 2 or offset + cb > 16384:
            raise _NativeCallError("Invalid Shell item identifier")
        offset += cb
    raise _NativeCallError("Oversized Shell item identifier")


def _icon_identity(pidl_data: bytes) -> str:
    return "pidl-sha256:" + hashlib.sha256(pidl_data).hexdigest()


def _item_name(folder: int, pidl: int, pidl_data: bytes) -> str:
    value = _STRRET()
    hr = _invoke(
        folder,
        11,
        ctypes.c_long,
        [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_STRRET)],
        ctypes.c_void_p(pidl),
        1,
        ctypes.byref(value),
    )  # GetDisplayNameOf(SHGDN_INFOLDER)
    _check_hr(hr, "IShellFolder.GetDisplayNameOf")
    if value.uType == 0 and value.pOleStr:
        try:
            return ctypes.wstring_at(value.pOleStr)
        finally:
            _co_task_mem_free(int(value.pOleStr))
    if value.uType == 1:
        offset = int(value.uOffset)
        if offset < 0 or offset >= len(pidl_data):
            return ""
        return pidl_data[offset:].split(b"\0", 1)[0].decode("mbcs", errors="replace")
    if value.uType == 2:
        raw = bytes(value.cStr).split(b"\0", 1)[0]
        return raw.decode("mbcs", errors="replace")
    return ""


def _item_position(view: int, pidl: int) -> Point:
    point = _POINT()
    hr = _invoke(
        view,
        11,
        ctypes.c_long,
        [ctypes.c_void_p, ctypes.POINTER(_POINT)],
        ctypes.c_void_p(pidl),
        ctypes.byref(point),
    )
    _check_hr(hr, "IFolderView.GetItemPosition")
    return int(point.x), int(point.y)


def _auto_arrange_enabled(view: int) -> bool:
    hr = _invoke(view, 14, ctypes.c_long, [])  # S_OK means auto-arrange is enabled.
    _check_hr(hr, "IFolderView.GetAutoArrange")
    return int(hr) == 0


def _icon_size() -> Point:
    import win32api
    import win32con

    return (
        max(1, int(win32api.GetSystemMetrics(win32con.SM_CXICON))),
        max(1, int(win32api.GetSystemMetrics(win32con.SM_CYICON))),
    )


def _icon_center(anchor: Point) -> Point:
    width, height = _icon_size()
    return anchor[0] + width // 2, anchor[1] + height // 2


def _is_recycle_bin(pidl_data: bytes, name: str) -> bool:
    recycle_bin_clsid = uuid.UUID("645FF040-5081-101B-9F08-00AA002F954E").bytes_le
    if recycle_bin_clsid in pidl_data:
        return True
    return name.strip().casefold() in {
        "recycle bin",
        "回收站",
        "垃圾箱",
        "papierkorb",
        "corbeille",
        "cestino",
    }


def _capture_icons(view: int, folder: int) -> tuple[tuple[DesktopIcon, ...], dict[str, _IconCache]]:
    icons: list[DesktopIcon] = []
    cache: dict[str, _IconCache] = {}
    for pidl in _iter_folder_items(view):
        pidl_data = _pidl_bytes(pidl)
        identity = _icon_identity(pidl_data)
        name = _item_name(folder, pidl, pidl_data)
        if not name:
            continue
        anchor = _item_position(view, pidl)
        center = _icon_center(anchor)
        can_move = not _is_recycle_bin(pidl_data, name)
        icon = DesktopIcon(identity=identity, name=name, x=center[0], y=center[1])
        icons.append(icon)
        cache[identity] = _IconCache(name=name, position=center, anchor=anchor, can_move=can_move)
    return tuple(icons), cache


def _select_and_position(view: int, pidl: int, position: Point) -> None:
    pidls = (ctypes.c_void_p * 1)(ctypes.c_void_p(pidl))
    points = (_POINT * 1)(_POINT(int(position[0]), int(position[1])))
    hr = _invoke(
        view,
        16,
        ctypes.c_long,
        [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(_POINT), ctypes.c_uint32],
        1,
        pidls,
        points,
        0x80,  # SVSI_POSITIONITEM
    )
    _check_hr(hr, "IFolderView.SelectAndPositionItems")
