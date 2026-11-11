"""Small Win32 helpers used by the independent Live2D renderer.

This module deliberately has no dependency on the desktop-pet adapter. DPI
awareness is scoped to the render thread and restored after the window closes.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def _thread_dpi_function() -> Any | None:
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        function = user32.SetThreadDpiAwarenessContext
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_void_p
        return function
    except (AttributeError, OSError):
        return None


@contextmanager
def per_monitor_dpi_context() -> Iterator[bool]:
    """Use per-monitor V2/V1 DPI coordinates while creating the SDL window."""

    function = _thread_dpi_function()
    if function is None:
        yield False
        return

    previous = None
    for context in (-4, -3):
        try:
            previous = function(ctypes.c_void_p(context))
        except (OSError, TypeError, ValueError):
            previous = None
        if previous:
            break

    if not previous:
        yield False
        return

    previous_value = getattr(previous, "value", previous)
    try:
        yield True
    finally:
        try:
            function(ctypes.c_void_p(previous_value))
        except (OSError, TypeError, ValueError):
            pass


def configure_renderer_window(hwnd: int, width: int, height: int, *, transparent: bool) -> None:
    """Apply the original standalone renderer's color-key and topmost styles."""

    if os.name != "nt" or not hwnd:
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not transparent:
        return

    hwnd_value = ctypes.c_void_p(int(hwnd))
    get_window_long = user32.GetWindowLongW
    get_window_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
    get_window_long.restype = ctypes.c_long
    set_window_long = user32.SetWindowLongW
    set_window_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    set_window_long.restype = ctypes.c_long
    ex_style = int(get_window_long(hwnd_value, -20))
    set_window_long(hwnd_value, -20, ex_style | 0x00080000)  # WS_EX_LAYERED

    set_layered = user32.SetLayeredWindowAttributes
    set_layered.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint]
    set_layered.restype = ctypes.c_int
    if not set_layered(hwnd_value, 0x00000000, 0, 0x00000001):  # black / LWA_COLORKEY
        raise ctypes.WinError(ctypes.get_last_error())

    set_window_pos = user32.SetWindowPos
    set_window_pos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    set_window_pos.restype = ctypes.c_int
    if not set_window_pos(
        hwnd_value,
        ctypes.c_void_p(-1),  # HWND_TOPMOST
        0,
        0,
        int(width),
        int(height),
        0x0002 | 0x0010,  # SWP_NOMOVE | SWP_NOACTIVATE
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def inspect_renderer_window(hwnd: int) -> dict[str, int]:
    """Return native style and physical-pixel bounds for smoke verification."""

    if os.name != "nt" or not hwnd:
        raise OSError("native renderer window inspection is only available on Windows")

    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_window_long = user32.GetWindowLongW
    get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_window_long.restype = ctypes.c_long
    get_window_rect = user32.GetWindowRect
    get_window_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    get_window_rect.restype = wintypes.BOOL
    rect = wintypes.RECT()
    hwnd_value = wintypes.HWND(int(hwnd))
    is_window_visible = user32.IsWindowVisible
    is_window_visible.argtypes = [wintypes.HWND]
    is_window_visible.restype = wintypes.BOOL
    if not get_window_rect(hwnd_value, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    color_key = wintypes.DWORD()
    alpha = ctypes.c_ubyte()
    layered_flags = wintypes.DWORD()
    get_layered = user32.GetLayeredWindowAttributes
    get_layered.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_layered.restype = wintypes.BOOL
    get_layered(
        hwnd_value,
        ctypes.byref(color_key),
        ctypes.byref(alpha),
        ctypes.byref(layered_flags),
    )

    return {
        "style": int(get_window_long(hwnd_value, -16)) & 0xFFFFFFFF,
        "ex_style": int(get_window_long(hwnd_value, -20)) & 0xFFFFFFFF,
        "visible": int(bool(is_window_visible(hwnd_value))),
        "color_key": int(color_key.value),
        "alpha": int(alpha.value),
        "layered_flags": int(layered_flags.value),
        "left": int(rect.left),
        "top": int(rect.top),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
    }


def get_cursor_position() -> tuple[int, int] | None:
    """Read the physical-pixel cursor position for right-drag window movement."""

    if os.name != "nt":
        return None
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_cursor_pos = user32.GetCursorPos
    get_cursor_pos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    get_cursor_pos.restype = wintypes.BOOL
    point = wintypes.POINT()
    if not get_cursor_pos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(point.x), int(point.y)


def move_renderer_window(hwnd: int, x: int, y: int) -> None:
    """Move the existing native window without changing its size or Z order."""

    if os.name != "nt" or not hwnd:
        return
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_window_pos = user32.SetWindowPos
    set_window_pos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    set_window_pos.restype = wintypes.BOOL
    if not set_window_pos(
        wintypes.HWND(int(hwnd)),
        wintypes.HWND(0),
        int(x),
        int(y),
        0,
        0,
        0x0001 | 0x0004 | 0x0010,  # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
    ):
        raise ctypes.WinError(ctypes.get_last_error())
