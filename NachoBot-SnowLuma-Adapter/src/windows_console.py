"""Small Windows console lifecycle hook for the SnowLuma adapter.

The Windows console sends ``CTRL_CLOSE_EVENT`` when a visible console window is
closed.  That event can arrive after the launcher has gone away, so waiting for
the async bridge to finish leaves the adapter process (and its ``uv`` children)
alive.  The close event therefore terminates this process immediately.  Other
control events are deliberately passed through so Python keeps its normal
``KeyboardInterrupt`` behaviour for Ctrl+C and Ctrl+Break.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from ctypes import wintypes

CTRL_CLOSE_EVENT = 2

# ``WINFUNCTYPE`` is only exposed by ctypes on Windows.  Keeping a portable
# fallback lets the module be imported and unit-tested on other platforms.
_HANDLER_FACTORY = (
    ctypes.WINFUNCTYPE if sys.platform == "win32" else ctypes.CFUNCTYPE
)
_HANDLER_TYPE = _HANDLER_FACTORY(wintypes.BOOL, wintypes.DWORD)

# The native API stores a function pointer after registration; retain the
# ctypes callback on the Python side for the entire process lifetime as well.
_registered_handler: object | None = None


def _terminate_process(exit_code: int) -> None:
    """Terminate immediately from the native console-control callback."""

    os._exit(exit_code)


def _make_handler(terminator: Callable[[int], None]) -> Callable[[int], bool]:
    def handle(control_type: int) -> bool:
        if control_type != CTRL_CLOSE_EVENT:
            return False
        terminator(0)
        return True

    return handle


def register_console_close_handler(
    *,
    terminator: Callable[[int], None] = _terminate_process,
    platform: str | None = None,
    kernel32: object | None = None,
) -> bool:
    """Register the immediate close-window handler on Windows.

    ``platform`` and ``kernel32`` are intentionally injectable so tests can
    exercise native dispatch without terminating the test process or requiring
    a real console.  Registration failures are treated as a safe no-op because
    a service/no-console launch is still a valid adapter environment.
    """

    global _registered_handler

    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return False

    # Avoid registering a second native callback if startup code is invoked
    # more than once in the same process.
    if _registered_handler is not None:
        return True

    try:
        if kernel32 is not None:
            native_api = kernel32
        else:
            windll = getattr(ctypes, "windll", None)
            native_api = (
                windll.kernel32
                if windll is not None
                else ctypes.WinDLL("kernel32", use_last_error=True)
            )
        set_handler = native_api.SetConsoleCtrlHandler

        # Real ctypes function pointers support these attributes.  Mock callables
        # used by tests do not need native signature configuration.
        if hasattr(set_handler, "argtypes"):
            set_handler.argtypes = [_HANDLER_TYPE, wintypes.BOOL]
        if hasattr(set_handler, "restype"):
            set_handler.restype = wintypes.BOOL

        callback = _HANDLER_TYPE(_make_handler(terminator))
        if not set_handler(callback, True):
            return False
    except Exception:
        # A missing console or an unavailable kernel32 entry point must not stop
        # the adapter from starting.  The caller may emit a bounded debug note.
        return False

    _registered_handler = callback
    return True
