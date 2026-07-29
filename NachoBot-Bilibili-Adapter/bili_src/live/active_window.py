"""Windows foreground-window discovery shared by screen capture."""

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable, Optional, Set, Tuple


DEFAULT_EXCLUDED_EXES = {"obs64.exe", "obs32.exe"}


@dataclass(frozen=True)
class ActiveWindowInfo:
    """Metadata and bounds for a visible foreground window."""

    hwnd: int
    title: str
    window_class: str
    executable: str
    rect: Tuple[int, int, int, int]

    @property
    def obs_window_value(self) -> str:
        """Return the title:class:executable value used by OBS Window Capture."""
        return build_obs_window_value(
            self.title,
            self.window_class,
            self.executable,
        )


WINDOWS_ACTIVE_WINDOW_SUPPORTED = os.name == "nt"
user32 = None
kernel32 = None

if WINDOWS_ACTIVE_WINDOW_SUPPORTED:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL

    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL

    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int

    user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int

    user32.GetClassNameW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetClassNameW.restype = ctypes.c_int

    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    user32.GetWindowRect.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.RECT),
    ]
    user32.GetWindowRect.restype = wintypes.BOOL

    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def normalise_executables(executables: Optional[Iterable[str]]) -> Set[str]:
    """Convert configured executable names to the case-insensitive lookup form."""
    if isinstance(executables, str):
        executables = executables.split(",")

    values = {
        str(executable).strip().lower()
        for executable in (executables or DEFAULT_EXCLUDED_EXES)
        if str(executable).strip()
    }
    return values or set(DEFAULT_EXCLUDED_EXES)


def _get_window_title(hwnd: wintypes.HWND) -> str:
    if not user32:
        return ""

    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""

    buffer = ctypes.create_unicode_buffer(length + 1)
    copied = user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value if copied > 0 else ""


def _get_window_class(hwnd: wintypes.HWND) -> str:
    if not user32:
        return ""

    buffer = ctypes.create_unicode_buffer(256)
    copied = user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value if copied > 0 else ""


def _get_window_executable(hwnd: wintypes.HWND) -> str:
    """Resolve a foreground window's executable using the OBS tracker approach."""
    if not user32 or not kernel32:
        return "unknown"

    process_id = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return "unknown"

    process = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        process_id.value,
    )
    if not process:
        return "unknown"

    try:
        capacity = 32768
        size = wintypes.DWORD(capacity)
        buffer = ctypes.create_unicode_buffer(capacity)
        success = kernel32.QueryFullProcessImageNameW(
            process,
            0,
            buffer,
            ctypes.byref(size),
        )
        if not success:
            return "unknown"
        return os.path.basename(buffer.value)
    finally:
        kernel32.CloseHandle(process)


def _get_window_rect(hwnd: wintypes.HWND) -> Optional[Tuple[int, int, int, int]]:
    if not user32:
        return None

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None

    bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        return None
    return bounds


def encode_obs_component(value: str) -> str:
    """Escape a Window Capture component in OBS's window-helpers.c order."""
    return value.replace("#", "#22").replace(":", "#3A")


def build_obs_window_value(title: str, window_class: str, executable: str) -> str:
    """Build the exact Window Capture identifier emitted by the OBS script."""
    return ":".join(
        (
            encode_obs_component(title),
            encode_obs_component(window_class),
            encode_obs_component(executable),
        )
    )


def get_active_window_info(
    excluded_exes: Optional[Iterable[str]] = None,
) -> Optional[ActiveWindowInfo]:
    """Return a capturable foreground window, or None when one is unavailable."""
    if not user32 or not kernel32:
        return None

    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None

    title = _get_window_title(hwnd)
    window_class = _get_window_class(hwnd)
    executable = _get_window_executable(hwnd)
    if not title or not window_class:
        return None

    if executable.lower() in normalise_executables(excluded_exes):
        return None

    rect = _get_window_rect(hwnd)
    if rect is None:
        return None

    hwnd_value = int(getattr(hwnd, "value", hwnd) or 0)
    return ActiveWindowInfo(
        hwnd=hwnd_value,
        title=title,
        window_class=window_class,
        executable=executable,
        rect=rect,
    )

