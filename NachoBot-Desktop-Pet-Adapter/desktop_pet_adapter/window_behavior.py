"""Win32 pet windows receive pointer interactions without becoming foreground."""

from __future__ import annotations

import ctypes
from pathlib import Path


def _keep_sdl_window_topmost():
    """Update SDL's flag as well as Win32's, so SDL events cannot demote the pet."""
    import pygame
    from pygame._sdl2 import Window

    sdl = ctypes.CDLL(str(Path(pygame.__file__).with_name("SDL2.dll")))
    sdl.SDL_GetWindowFromID.argtypes = [ctypes.c_uint32]
    sdl.SDL_GetWindowFromID.restype = ctypes.c_void_p
    sdl.SDL_SetWindowAlwaysOnTop.argtypes = [ctypes.c_void_p, ctypes.c_int]
    sdl.SDL_SetWindowAlwaysOnTop.restype = None
    window = Window.from_display_module()
    pointer = sdl.SDL_GetWindowFromID(window.id)
    if not pointer:
        raise RuntimeError("SDL display window is unavailable")
    sdl.SDL_SetWindowAlwaysOnTop(pointer, 1)
    # Pygame stores this borrowed wrapper as a raw pg_window pointer in SDL.
    # Retain it until the data is cleared; a temporary here corrupts later events.
    return sdl, window, pointer


class PassiveWindow:
    """Subclass on the window's owning thread; retain the callback until restored."""

    def __init__(self, hwnd: int):
        import win32con
        import win32gui

        self.hwnd = hwnd
        self._owner = 0
        self._original = None
        self._callback = None
        self._sdl_state = None
        self._previous_owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
        try:
            self._install()
        except BaseException:
            self.close()
            raise

    def _install(self) -> None:
        import win32con
        import win32gui

        hwnd = self.hwnd
        # A hidden topmost owner keeps its owned pet in the topmost band even
        # when Windows' foreground lock denies promotion by a background process.
        # It is never shown, activated, or registered in the taskbar.
        self._owner = win32gui.CreateWindowEx(
            0x08000000 | win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
            "STATIC", "", win32con.WS_POPUP, 0, 0, 0, 0, 0, 0, 0, None,
        )
        win32gui.SetWindowLong(hwnd, win32con.GWL_HWNDPARENT, self._owner)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        procedure = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
        self._set_proc = self._user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else self._user32.SetWindowLongW
        self._set_proc.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self._set_proc.restype = ctypes.c_void_p
        self._user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        self._user32.CallWindowProcW.restype = ctypes.c_ssize_t
        self._callback = procedure(self._window_proc)
        self._original = self._set_proc(hwnd, win32con.GWL_WNDPROC, ctypes.cast(self._callback, ctypes.c_void_p))
        if not self._original:
            raise ctypes.WinError(ctypes.get_last_error())
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style | 0x08000000)  # WS_EX_NOACTIVATE
        self._sdl_state = _keep_sdl_window_topmost()

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        import win32con

        if message == win32con.WM_MOUSEACTIVATE:
            return win32con.MA_NOACTIVATE
        return self._user32.CallWindowProcW(self._original, hwnd, message, wparam, lparam)

    def close(self) -> None:
        import win32con
        import win32gui

        if self.hwnd and win32gui.IsWindow(self.hwnd):
            if self._sdl_state is not None:
                sdl, _window, pointer = self._sdl_state
                sdl.SDL_SetWindowData.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
                sdl.SDL_SetWindowData.restype = ctypes.c_void_p
                sdl.SDL_SetWindowData(pointer, b"pg_window", None)
            if self._original and not self._set_proc(self.hwnd, win32con.GWL_WNDPROC, self._original):
                raise ctypes.WinError(ctypes.get_last_error())
            win32gui.SetWindowLong(self.hwnd, win32con.GWL_HWNDPARENT, self._previous_owner)
        self.hwnd = 0
        if self._owner:
            win32gui.DestroyWindow(self._owner)
            self._owner = 0
        self._callback = None
        self._sdl_state = None
