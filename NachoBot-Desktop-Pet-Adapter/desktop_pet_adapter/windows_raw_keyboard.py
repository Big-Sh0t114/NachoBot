"""Windows Raw Input keyboard listener used by the Sprite pet counter.

The module performs no Windows calls at import time. Each listener owns a
message-only window created and destroyed on its caller's thread.
"""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

WM_INPUT = 0x00FF
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

RIM_TYPEKEYBOARD = 1
RID_INPUT = 0x10000003
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100

RI_KEY_BREAK = 0x0001
RI_KEY_E0 = 0x0002
RI_KEY_E1 = 0x0004
_RI_KEY_FLAGS = RI_KEY_BREAK | RI_KEY_E0 | RI_KEY_E1
_UINT_ERROR = 0xFFFFFFFF
_VK_PAUSE = 0x13
_VK_INVALID = 0xFF
_MAKE_CODE_OVERRUN = 0xFF
_HWND_MESSAGE = ctypes.c_void_p(-3)


class RawInputPacketError(ValueError):
    """A WM_INPUT packet was truncated or did not describe a keyboard key."""


@dataclass(frozen=True, slots=True)
class RawKeyboardTransition:
    vk_code: int
    message: int
    flags: int
    scan_code: int


class _RawInputHeader(ctypes.Structure):
    _fields_ = [
        ("dwType", ctypes.c_uint32),
        ("dwSize", ctypes.c_uint32),
        ("hDevice", ctypes.c_void_p),
        ("wParam", ctypes.c_size_t),
    ]


class _RawKeyboard(ctypes.Structure):
    _fields_ = [
        ("MakeCode", ctypes.c_uint16),
        ("Flags", ctypes.c_uint16),
        ("Reserved", ctypes.c_uint16),
        ("VKey", ctypes.c_uint16),
        ("Message", ctypes.c_uint32),
        ("ExtraInformation", ctypes.c_uint32),
    ]


class _RawInputDevice(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.c_uint16),
        ("usUsage", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("hwndTarget", ctypes.c_void_p),
    ]


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


@dataclass(frozen=True, slots=True)
class _DeviceRegistration:
    usage_page: int
    usage: int
    flags: int
    hwnd_target: int | None


def decode_raw_keyboard_packet(packet: bytes) -> RawKeyboardTransition | None:
    """Decode a complete RAWINPUT keyboard packet without retaining its data.

    Invalid virtual keys and the documented keyboard-overrun make code are
    ignored. Structural inconsistencies raise ``RawInputPacketError``.
    """

    header_size = ctypes.sizeof(_RawInputHeader)
    keyboard_size = ctypes.sizeof(_RawKeyboard)
    minimum_size = header_size + keyboard_size
    if len(packet) < minimum_size:
        raise RawInputPacketError("raw keyboard packet is truncated")

    header = _RawInputHeader.from_buffer_copy(packet[:header_size])
    if header.dwType != RIM_TYPEKEYBOARD:
        raise RawInputPacketError("raw input packet is not a keyboard packet")
    if header.dwSize < minimum_size or header.dwSize != len(packet):
        raise RawInputPacketError("raw input packet size does not match its header")

    keyboard = _RawKeyboard.from_buffer_copy(packet[header_size:minimum_size])
    flags = int(keyboard.Flags)
    if flags & ~_RI_KEY_FLAGS:
        raise RawInputPacketError("raw keyboard packet contains unknown key flags")
    if (flags & RI_KEY_E0) and (flags & RI_KEY_E1):
        raise RawInputPacketError("raw keyboard packet has conflicting extended flags")
    if int(keyboard.VKey) >= _VK_INVALID or int(keyboard.MakeCode) == _MAKE_CODE_OVERRUN:
        return None

    is_break = bool(flags & RI_KEY_BREAK)
    if is_break:
        message = WM_SYSKEYUP if int(keyboard.Message) == WM_SYSKEYUP else WM_KEYUP
    else:
        message = WM_SYSKEYDOWN if int(keyboard.Message) == WM_SYSKEYDOWN else WM_KEYDOWN

    # RI_KEY_E0/E1 are 0x02/0x04, while the legacy-hook injected bits are
    # 0x02/0x10. Normalize to the legacy extended bit; never forward raw flags.
    normalized_flags = 0x01 if flags & (RI_KEY_E0 | RI_KEY_E1) else 0
    scan_code = int(keyboard.MakeCode)
    if flags & RI_KEY_E1:
        # Preserve E1 separately from E0 in the in-memory edge identity.
        scan_code |= 0x10000
    return RawKeyboardTransition(
        vk_code=int(keyboard.VKey),
        message=message,
        flags=normalized_flags,
        scan_code=scan_code,
    )


class _WindowsRawInputApi:
    """Explicitly typed Win32 calls for one worker-owned message-only window."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Raw Input is available only on Windows")

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        self.kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        self.kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
        self.kernel32.GetCurrentThreadId.argtypes = []
        self._module = self.kernel32.GetModuleHandleW(None)
        if not self._module:
            raise ctypes.WinError(ctypes.get_last_error())
        self.thread_id = int(self.kernel32.GetCurrentThreadId())

        self.user32.GetRawInputData.restype = ctypes.c_uint32
        self.user32.GetRawInputData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        ]
        self.user32.GetRegisteredRawInputDevices.restype = ctypes.c_uint32
        self.user32.GetRegisteredRawInputDevices.argtypes = [
            ctypes.POINTER(_RawInputDevice),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        ]
        self.user32.RegisterRawInputDevices.restype = ctypes.c_int
        self.user32.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(_RawInputDevice),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]

        self._wndproc_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
            ctypes.c_ssize_t,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        self.user32.RegisterClassExW.restype = ctypes.c_uint16
        self.user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
        self.user32.CreateWindowExW.restype = ctypes.c_void_p
        self.user32.CreateWindowExW.argtypes = [
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.user32.DestroyWindow.restype = ctypes.c_int
        self.user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        self.user32.UnregisterClassW.restype = ctypes.c_int
        self.user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        self.user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self.user32.DefWindowProcW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        self.user32.PeekMessageW.restype = ctypes.c_int
        self.user32.PeekMessageW.argtypes = [
            ctypes.POINTER(_Message),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]

    def prepare_message_queue(self) -> None:
        # Ensure the owner thread has a queue before the shared worker posts
        # WM_QUIT to wake its message loop during shutdown.
        message = _Message()
        self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)

    def make_wndproc(self, callback: Callable[..., int]) -> Any:
        return self._wndproc_type(callback)

    def register_window_class(self, name: str, callback: Any) -> None:
        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint32),
                ("style", ctypes.c_uint32),
                ("lpfnWndProc", self._wndproc_type),
                ("cbClsExtra", ctypes.c_int32),
                ("cbWndExtra", ctypes.c_int32),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.c_void_p),
            ]

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = callback
        wc.hInstance = self._module
        wc.lpszClassName = name
        if not self.user32.RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())

    def create_message_window(self, class_name: str) -> int:
        hwnd = self.user32.CreateWindowExW(
            0,
            class_name,
            class_name,
            0,
            0,
            0,
            0,
            0,
            _HWND_MESSAGE,
            None,
            self._module,
            None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(hwnd)

    def get_keyboard_registration(self) -> _DeviceRegistration | None:
        count = ctypes.c_uint32(0)
        header_size = ctypes.sizeof(_RawInputDevice)
        result = int(
            self.user32.GetRegisteredRawInputDevices(None, ctypes.byref(count), header_size)
        )
        if result == _UINT_ERROR:
            raise ctypes.WinError(ctypes.get_last_error())
        if count.value == 0:
            return None

        devices = (_RawInputDevice * count.value)()
        capacity = ctypes.c_uint32(count.value)
        result = int(
            self.user32.GetRegisteredRawInputDevices(
                devices,
                ctypes.byref(capacity),
                header_size,
            )
        )
        if result == _UINT_ERROR or result > len(devices):
            raise ctypes.WinError(ctypes.get_last_error())
        for device in devices[:result]:
            if device.usUsagePage == 0x01 and device.usUsage == 0x06:
                target = int(device.hwndTarget) if device.hwndTarget else None
                return _DeviceRegistration(
                    usage_page=int(device.usUsagePage),
                    usage=int(device.usUsage),
                    flags=int(device.dwFlags),
                    hwnd_target=target,
                )
        return None

    def _register(self, registration: _DeviceRegistration) -> None:
        device = _RawInputDevice(
            registration.usage_page,
            registration.usage,
            registration.flags,
            registration.hwnd_target,
        )
        if not self.user32.RegisterRawInputDevices(
            ctypes.byref(device),
            1,
            ctypes.sizeof(_RawInputDevice),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def register_keyboard_inputsink(self, hwnd: int) -> None:
        self._register(_DeviceRegistration(0x01, 0x06, RIDEV_INPUTSINK, hwnd))

    def restore_keyboard_registration(self, registration: _DeviceRegistration) -> None:
        self._register(registration)

    def remove_keyboard_registration(self) -> None:
        # RIDEV_REMOVE requires a NULL target and is called only after verifying
        # the current registration still points at this listener's HWND.
        self._register(_DeviceRegistration(0x01, 0x06, RIDEV_REMOVE, None))

    def read_keyboard_event(self, raw_handle: int) -> RawKeyboardTransition | None:
        header_size = ctypes.sizeof(_RawInputHeader)
        required = ctypes.c_uint32(0)
        result = int(
            self.user32.GetRawInputData(
                ctypes.c_void_p(raw_handle),
                RID_INPUT,
                None,
                ctypes.byref(required),
                header_size,
            )
        )
        if result == _UINT_ERROR:
            raise ctypes.WinError(ctypes.get_last_error())
        if result != 0 or required.value < header_size + ctypes.sizeof(_RawKeyboard):
            raise RawInputPacketError("GetRawInputData returned an invalid packet size")

        buffer = ctypes.create_string_buffer(required.value)
        capacity = ctypes.c_uint32(required.value)
        copied = int(
            self.user32.GetRawInputData(
                ctypes.c_void_p(raw_handle),
                RID_INPUT,
                buffer,
                ctypes.byref(capacity),
                header_size,
            )
        )
        if copied == _UINT_ERROR:
            raise ctypes.WinError(ctypes.get_last_error())
        if copied != required.value or capacity.value != required.value:
            raise RawInputPacketError("GetRawInputData returned a truncated packet")
        return decode_raw_keyboard_packet(buffer.raw[:copied])

    def def_window_proc(self, hwnd: int, message: int, w_param: int, l_param: int) -> int:
        return int(self.user32.DefWindowProcW(hwnd, message, w_param, l_param))

    def destroy_window(self, hwnd: int) -> None:
        if int(self.kernel32.GetCurrentThreadId()) != self.thread_id:
            raise RuntimeError("message-only window must be destroyed by its owner thread")
        if not self.user32.DestroyWindow(hwnd):
            raise ctypes.WinError(ctypes.get_last_error())

    def unregister_window_class(self, name: str) -> None:
        if not self.user32.UnregisterClassW(name, self._module):
            raise ctypes.WinError(ctypes.get_last_error())


_ORPHANED_RAW_WINDOWS: list[WindowsRawKeyboard] = []
_ORPHANED_RAW_WINDOWS_LOCK = threading.Lock()


class WindowsRawKeyboard:
    """Own a Raw Input registration and message-only window on one thread."""

    def __init__(self, *, api_factory: Callable[[], Any] | None = None) -> None:
        self._api_factory = api_factory or _WindowsRawInputApi
        self._api: Any = None
        self._thread_id: int | None = None
        self._class_name: str | None = None
        self._window: int | None = None
        self._class_registered = False
        self._registered = False
        self._original_registration: _DeviceRegistration | None = None
        self._wndproc: Any = None
        self._event_handler: Callable[[RawKeyboardTransition], None] | None = None
        self._error_handler: Callable[[], None] | None = None
        self._closed = False
        self._destroy_failed = False

    @property
    def window(self) -> int | None:
        return self._window

    def start(
        self,
        event_handler: Callable[[RawKeyboardTransition], None],
        error_handler: Callable[[], None],
    ) -> None:
        if self._api is not None or self._closed:
            raise RuntimeError("Raw Input keyboard listener cannot be started twice")
        api = self._api_factory()
        self._api = api
        self._thread_id = int(getattr(api, "thread_id", threading.get_ident()))
        self._event_handler = event_handler
        self._error_handler = error_handler
        self._class_name = (
            f"NachoBotRawKeyboard_{os.getpid()}_{self._thread_id}_{uuid.uuid4().hex}"
        )
        try:
            api.prepare_message_queue()
            self._wndproc = api.make_wndproc(self._window_proc)
            api.register_window_class(self._class_name, self._wndproc)
            self._class_registered = True
            self._window = int(api.create_message_window(self._class_name))
            self._original_registration = api.get_keyboard_registration()
            api.register_keyboard_inputsink(self._window)
            self._registered = True
        except BaseException as exc:
            try:
                self.close()
            except Exception as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"Raw Input startup cleanup also failed: {cleanup_error}")
            raise

    def close(self) -> None:
        if self._closed:
            return
        errors: list[BaseException] = []
        api = self._api
        window = self._window
        class_name = self._class_name

        if api is not None and self._registered and window is not None:
            try:
                current = api.get_keyboard_registration()
                if current is not None and current.hwnd_target == window:
                    if self._original_registration is None:
                        api.remove_keyboard_registration()
                    else:
                        api.restore_keyboard_registration(self._original_registration)
            except BaseException as exc:
                errors.append(exc)

        if api is not None and window is not None:
            try:
                api.destroy_window(window)
                self._window = None
            except BaseException as exc:
                self._destroy_failed = True
                errors.append(exc)

        # Keep the WndProc thunk alive until after HWND destruction.
        if api is not None and class_name is not None and self._class_registered:
            try:
                api.unregister_window_class(class_name)
                self._class_registered = False
            except BaseException as exc:
                errors.append(exc)

        self._closed = True
        if self._destroy_failed or self._class_registered:
            with _ORPHANED_RAW_WINDOWS_LOCK:
                _ORPHANED_RAW_WINDOWS.append(self)
        else:
            self._wndproc = None
            self._event_handler = None
            self._error_handler = None

        if errors:
            raise errors[0]

    def _window_proc(self, hwnd: int, message: int, w_param: int, l_param: int) -> int:
        api = self._api
        if api is None:
            return 0
        if int(message) == WM_INPUT:
            try:
                event = api.read_keyboard_event(int(l_param))
                if event is not None and self._event_handler is not None:
                    self._event_handler(event)
            except BaseException:
                handler = self._error_handler
                if handler is not None:
                    try:
                        handler()
                    except BaseException:
                        pass
        # DefWindowProc performs required cleanup for foreground RIM_INPUT.
        # It is also the normal path for unknown messages.
        try:
            return int(api.def_window_proc(hwnd, message, w_param, l_param))
        except BaseException:
            handler = self._error_handler
            if handler is not None:
                try:
                    handler()
                except BaseException:
                    pass
            return 0
