"""Thread-scoped Windows DPI awareness helpers.

Importing this module has no process-wide or native side effects. Renderers use
the context manager around SDL creation and their native window lifetime so
cursor, monitor, and HWND coordinates share physical pixels.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def _set_thread_context_function() -> Any | None:
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
    """Temporarily set this thread to per-monitor DPI awareness.

    Per-monitor V2 is preferred. Per-monitor V1 is used on older Windows
    versions. Unsupported systems and non-Windows hosts continue unchanged.
    The previous thread context is restored even when the caller raises.
    """

    function = _set_thread_context_function()
    if function is None:
        yield False
        return

    previous = None
    for awareness_context in (-4, -3):
        try:
            previous = function(ctypes.c_void_p(awareness_context))
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
            # Context setup is best-effort on Windows versions that expose the
            # API incompletely; do not mask the renderer's original outcome.
            pass
