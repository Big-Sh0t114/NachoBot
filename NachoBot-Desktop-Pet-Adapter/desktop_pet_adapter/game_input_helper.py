"""One-way elevated input-count transport for game compatibility mode.

Importing this module is inert. The host opens a read-only named-pipe server;
the fixed elevated collector connects as a write-only client after its START
event is signaled. Counts are cumulative and contain no input identities.
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


_VERSION = 1
_STATUSES = frozenset({"prepared", "ready", "snapshot", "final", "failed"})
_MAX_FRAME_BYTES = 4096
_MAX_COUNT = (1 << 63) - 1
_MAX_FRAMES_PER_POLL = 32
_POLL_INTERVAL = 1.0 / 60.0
_STARTUP_TIMEOUT = 45.0
_STOP_TIMEOUT = 2.5
_IO_TIMEOUT = 0.5
_PROTOCOL_ERROR = "invalid-message"


@dataclass(frozen=True)
class GameInputMessage:
    version: int
    session: str
    sequence: int
    status: str
    keyboard: int
    mouse: int


class _TransportError(RuntimeError):
    """An internal, sanitized transport failure."""

    def __init__(self, reason: str):
        self.reason = reason


class _LaunchError(RuntimeError):
    """An internal, sanitized elevation-launch failure."""

    def __init__(self, reason: str):
        self.reason = reason


@dataclass(frozen=True)
class _LaunchInfo:
    pid: int
    process_handle: Any


def _valid_count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= _MAX_COUNT


def _encode_message(message: GameInputMessage) -> bytes:
    """Return one bounded length-prefixed UTF-8 frame."""
    if (
        not isinstance(message, GameInputMessage)
        or type(message.version) is not int
        or message.version != _VERSION
        or not isinstance(message.session, str)
        or not message.session
        or len(message.session) > 64
        or type(message.sequence) is not int
        or message.sequence <= 0
        or not isinstance(message.status, str)
        or message.status not in _STATUSES
        or not _valid_count(message.keyboard)
        or not _valid_count(message.mouse)
    ):
        raise ValueError(_PROTOCOL_ERROR)
    body = json.dumps(
        {
            "version": message.version,
            "session": message.session,
            "sequence": message.sequence,
            "status": message.status,
            "keyboard": message.keyboard,
            "mouse": message.mouse,
        },
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    if not body or len(body) > _MAX_FRAME_BYTES:
        raise ValueError(_PROTOCOL_ERROR)
    return len(body).to_bytes(4, "big") + body


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_PROTOCOL_ERROR)
        result[key] = value
    return result


def _decode_message(body: bytes, expected_session: str) -> GameInputMessage:
    if not isinstance(body, bytes) or not body or len(body) > _MAX_FRAME_BYTES:
        raise ValueError(_PROTOCOL_ERROR)
    try:
        payload = json.loads(
            body.decode("utf-8", errors="strict"), object_pairs_hook=_no_duplicate_pairs
        )
    except (UnicodeError, ValueError, TypeError):
        raise ValueError(_PROTOCOL_ERROR) from None
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "session",
        "sequence",
        "status",
        "keyboard",
        "mouse",
    }:
        raise ValueError(_PROTOCOL_ERROR)
    if (
        type(payload["version"]) is not int
        or payload["version"] != _VERSION
        or not isinstance(payload["session"], str)
        or not secrets.compare_digest(payload["session"], expected_session)
        or type(payload["sequence"]) is not int
        or payload["sequence"] <= 0
        or not isinstance(payload["status"], str)
        or payload["status"] not in _STATUSES
        or not _valid_count(payload["keyboard"])
        or not _valid_count(payload["mouse"])
    ):
        raise ValueError(_PROTOCOL_ERROR)
    return GameInputMessage(
        payload["version"],
        payload["session"],
        payload["sequence"],
        payload["status"],
        payload["keyboard"],
        payload["mouse"],
    )


class _MessageValidator:
    """Validate an authenticated, ordered cumulative message stream."""

    def __init__(self, session: str):
        self.session = session
        self.sequence = 0
        self.keyboard = 0
        self.mouse = 0
        self.status = "new"

    def accept(self, message: GameInputMessage) -> None:
        if message.session != self.session or message.sequence <= self.sequence:
            raise ValueError(_PROTOCOL_ERROR)
        allowed = {
            "new": {"prepared"},
            "prepared": {"ready", "final", "failed"},
            "ready": {"snapshot", "final", "failed"},
            "snapshot": {"snapshot", "final", "failed"},
        }
        if message.status not in allowed.get(self.status, set()):
            raise ValueError(_PROTOCOL_ERROR)
        if message.keyboard < self.keyboard or message.mouse < self.mouse:
            raise ValueError(_PROTOCOL_ERROR)
        self.sequence = message.sequence
        self.keyboard = message.keyboard
        self.mouse = message.mouse
        self.status = message.status


def _validate_parent_identity(
    *,
    current_sid: str,
    parent_sid: str,
    expected_sid: str,
    elevated: bool,
    current_creation: str,
    expected_creation: str,
    current_session: int,
    expected_session: int,
    parent_alive: bool,
) -> None:
    if (
        not elevated
        or current_sid != expected_sid
        or parent_sid != current_sid
        or current_creation != expected_creation
        or current_session != expected_session
    ):
        raise _TransportError("identity-validation")
    if not parent_alive:
        raise _TransportError("parent-exited")


def _validate_peer_identity(
    *,
    peer_pid: int,
    launch_pid: int,
    peer_session: int,
    expected_session: int,
    peer_sid: str,
    expected_sid: str,
    elevated: bool,
    peer_image: str,
    expected_image: str | tuple[str, ...],
    launched_descendant: bool,
    peer_alive: bool,
) -> None:
    expected_images = (
        (expected_image,)
        if isinstance(expected_image, str)
        else tuple(expected_image)
    )
    normalized_expected = {
        os.path.normcase(os.path.abspath(path)) for path in expected_images
    }
    if (
        peer_pid <= 0
        or peer_session != expected_session
        or peer_sid != expected_sid
        or not elevated
        or os.path.normcase(os.path.abspath(peer_image)) not in normalized_expected
        or not launched_descendant
        or not peer_alive
    ):
        raise _TransportError("peer-validation")


def _win32() -> tuple[Any, ...]:
    """Load Win32 modules only when a session or helper actually starts."""
    if os.name != "nt":
        raise _TransportError("windows-only")
    try:
        import pywintypes
        import win32api
        import win32con
        import win32event
        import win32file
        import win32pipe
        import win32process
        import win32security
        import win32ts
    except Exception:
        raise _TransportError("windows-api-unavailable") from None
    return (
        pywintypes,
        win32api,
        win32con,
        win32event,
        win32file,
        win32pipe,
        win32process,
        win32security,
        win32ts,
    )


def _token_identity(process_handle: Any) -> tuple[str, bool]:
    _, _, win32con, _, _, _, _, win32security, _ = _win32()
    try:
        token = win32security.OpenProcessToken(process_handle, win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            sid_text = win32security.ConvertSidToStringSid(sid)
            elevated = bool(
                win32security.GetTokenInformation(token, win32security.TokenElevation)
            )
            return sid_text, elevated
        finally:
            token.Close()
    except Exception:
        raise _TransportError("token-validation") from None


def _process_creation_identity(process_handle: Any) -> str:
    _, _, _, _, _, _, win32process, _, _ = _win32()
    try:
        created = win32process.GetProcessTimes(process_handle)["CreationTime"]
        return created.isoformat(timespec="microseconds")
    except Exception:
        raise _TransportError("process-validation") from None


def _session_id(pid: int) -> int:
    *_, win32ts = _win32()
    try:
        return int(win32ts.ProcessIdToSessionId(int(pid)))
    except Exception:
        raise _TransportError("session-validation") from None


class _WindowsSessionTransport:
    """Read-only parent named-pipe server and its session-bound events."""

    def __init__(self, session: str):
        self.session = session
        self.pipe_name = ""
        self.start_name = ""
        self.stop_name = ""
        self.parent_sid = ""
        self.parent_creation = ""
        self.parent_pid = 0
        self.parent_session = 0
        self.pipe = None
        self.start_event = None
        self.stop_event = None
        self.parent_handle = None
        self._connect_event = None
        self._connect_overlapped = None
        self._connected = False
        self._closed = False
        self._lock = threading.RLock()
        self._read_buffer = bytearray()
        self._security_descriptor = None
        self._security_attributes = None
        self._pending_refs: list[tuple[Any, ...]] = []
        self._peer_handle = None
        self._expected_interpreter_images: tuple[str, ...] = ()

    def open(self) -> None:
        (
            _,
            win32api,
            win32con,
            win32event,
            _,
            win32pipe,
            win32process,
            win32security,
            _,
        ) = _win32()
        try:
            self.parent_pid = int(win32process.GetCurrentProcessId())
            self.parent_handle = win32api.OpenProcess(
                win32con.SYNCHRONIZE
                | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                self.parent_pid,
            )
            self.parent_sid, _ = _token_identity(self.parent_handle)
            self.parent_creation = _process_creation_identity(self.parent_handle)
            self.parent_session = _session_id(self.parent_pid)
            suffix = f"{self.parent_pid}-{self.session}"
            self.pipe_name = rf"\\.\pipe\NachoBotGameInput-{suffix}"
            self.start_name = rf"Local\NachoBotGameInputStart-{suffix}"
            self.stop_name = rf"Local\NachoBotGameInputStop-{suffix}"
            sddl = f"D:P(A;;GA;;;{self.parent_sid})(A;;GA;;;SY)"
            self._security_descriptor = (
                win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                    sddl, win32security.SDDL_REVISION_1
                )
            )
            self._security_attributes = win32security.SECURITY_ATTRIBUTES()
            self._security_attributes.bInheritHandle = False
            self._security_attributes.SECURITY_DESCRIPTOR = self._security_descriptor
            self.start_event = win32event.CreateEvent(
                self._security_attributes, True, False, self.start_name
            )
            self.stop_event = win32event.CreateEvent(
                self._security_attributes, True, False, self.stop_name
            )
            self.pipe = win32pipe.CreateNamedPipe(
                self.pipe_name,
                win32pipe.PIPE_ACCESS_INBOUND | win32con.FILE_FLAG_OVERLAPPED,
                win32pipe.PIPE_TYPE_BYTE
                | win32pipe.PIPE_READMODE_BYTE
                | win32pipe.PIPE_WAIT
                | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
                1,
                0,
                _MAX_FRAME_BYTES + 4,
                0,
                self._security_attributes,
            )
            self._connect_event = win32event.CreateEvent(None, True, False, None)
            self._connect_overlapped = __import__("win32file").OVERLAPPED()
            self._connect_overlapped.hEvent = self._connect_event
            try:
                connect_result = win32pipe.ConnectNamedPipe(self.pipe, self._connect_overlapped)
                self._connected = connect_result != 997  # ERROR_IO_PENDING
            except Exception as exc:
                code = getattr(exc, "winerror", None)
                if code == 535:  # ERROR_PIPE_CONNECTED
                    self._connected = True
                elif code == 997:  # ERROR_IO_PENDING
                    self._connected = False
                else:
                    raise
        except _TransportError:
            self.close()
            raise
        except Exception:
            self.close()
            raise _TransportError("pipe-setup") from None

    def launch_spec(self) -> tuple[str, str]:
        package_root = Path(__file__).resolve().parents[1]
        interpreter = package_root / ".venv" / "Scripts" / "pythonw.exe"
        if not interpreter.is_file() or not interpreter.is_absolute():
            raise _LaunchError("interpreter-unavailable")
        self._expected_interpreter_images = _interpreter_image_candidates(
            package_root, interpreter
        )
        bootstrap = (
            "import sys;"
            f"sys.path.insert(0,{str(package_root)!r});"
            "from desktop_pet_adapter.game_input_helper import _collector_cli;"
            "raise SystemExit(_collector_cli())"
        )
        args = [
            "-I",
            "-B",
            "-c",
            bootstrap,
            "--gih-helper",
            "--pipe",
            self.pipe_name,
            "--start",
            self.start_name,
            "--stop",
            self.stop_name,
            "--session",
            self.session,
            "--parent-pid",
            str(self.parent_pid),
            "--parent-created",
            self.parent_creation,
            "--parent-sid",
            self.parent_sid,
            "--parent-session",
            str(self.parent_session),
        ]
        return str(interpreter), subprocess.list2cmdline(args)

    def signal_start(self) -> None:
        if self._closed or self.start_event is None:
            raise _TransportError("transport-closed")
        try:
            _, _, _, win32event, *_ = _win32()
            win32event.SetEvent(self.start_event)
        except Exception:
            raise _TransportError("start-signal") from None

    def signal_stop(self) -> None:
        if self._closed or self.stop_event is None:
            return
        try:
            _, _, _, win32event, *_ = _win32()
            win32event.SetEvent(self.stop_event)
        except Exception:
            raise _TransportError("stop-signal") from None

    def wait_for_client(self, timeout: float) -> bool:
        if self._connected:
            return True
        if self._closed or self.pipe is None:
            return False
        _, _, _, win32event, win32file, _, _, _, _ = _win32()
        wait_ms = max(0, min(100, int(max(0.0, timeout) * 1000)))
        try:
            result = win32event.WaitForSingleObject(self._connect_event, wait_ms)
            if result == win32event.WAIT_TIMEOUT:
                return False
            win32file.GetOverlappedResult(self.pipe, self._connect_overlapped, False)
            self._connected = True
            return True
        except Exception as exc:
            code = getattr(exc, "winerror", None)
            if code == 996:  # ERROR_IO_INCOMPLETE
                return False
            raise _TransportError("pipe-connect") from None

    def verify_peer(self, launch_info: _LaunchInfo) -> None:
        if not self._connected or self.pipe is None:
            raise _TransportError("peer-not-connected")
        (
            _,
            win32api,
            win32con,
            _,
            _,
            win32pipe,
            win32process,
            win32security,
            win32ts,
        ) = _win32()
        peer_handle = None
        try:
            peer_pid = int(win32pipe.GetNamedPipeClientProcessId(self.pipe))
            peer_session = int(win32pipe.GetNamedPipeClientSessionId(self.pipe))
            peer_handle = win32api.OpenProcess(
                win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                peer_pid,
            )
            sid, elevated = _token_identity(peer_handle)
            peer_executable = _process_image_path(peer_handle)
            _validate_peer_identity(
                peer_pid=peer_pid,
                launch_pid=int(launch_info.pid),
                peer_session=peer_session,
                expected_session=self.parent_session,
                peer_sid=sid,
                expected_sid=self.parent_sid,
                elevated=elevated,
                peer_image=peer_executable,
                expected_image=self._expected_interpreter_images,
                launched_descendant=_is_descendant_of(peer_pid, int(launch_info.pid)),
                peer_alive=not win32event_wait_dead(peer_handle)
                and int(win32ts.ProcessIdToSessionId(peer_pid)) == self.parent_session,
            )
            self._peer_handle = peer_handle
        except _TransportError:
            if peer_handle is not None and peer_handle is not self._peer_handle:
                try:
                    win32api.CloseHandle(peer_handle)
                except Exception:
                    pass
            raise
        except Exception:
            if peer_handle is not None and peer_handle is not self._peer_handle:
                try:
                    win32api.CloseHandle(peer_handle)
                except Exception:
                    pass
            raise _TransportError("peer-validation") from None

    def helper_alive(self, launch_info: _LaunchInfo) -> bool:
        if self._peer_handle is None:
            return launch_info.process_handle is not None and not win32event_wait_dead(
                launch_info.process_handle
            )
        return not win32event_wait_dead(self._peer_handle)

    def read_frame(self, timeout: float) -> bytes | None:
        if not self._connected or self.pipe is None or self._closed:
            return None
        deadline = time.monotonic() + max(0.0, min(timeout, 0.1))
        while len(self._read_buffer) < 4:
            chunk = self._read_available(deadline, 4 - len(self._read_buffer))
            if chunk is None:
                return None
            if not chunk:
                raise _TransportError("pipe-closed")
            self._read_buffer.extend(chunk)
        body_length = int.from_bytes(self._read_buffer[:4], "big")
        if body_length < 1 or body_length > _MAX_FRAME_BYTES:
            raise _TransportError("invalid-message")
        total_length = 4 + body_length
        while len(self._read_buffer) < total_length:
            chunk = self._read_available(deadline, total_length - len(self._read_buffer))
            if chunk is None:
                return None
            if not chunk:
                raise _TransportError("pipe-closed")
            self._read_buffer.extend(chunk)
        frame = bytes(self._read_buffer[4:total_length])
        del self._read_buffer[:total_length]
        return frame

    def _read_available(self, deadline: float, max_bytes: int) -> bytes | None:
        _, _, _, _, _, win32pipe, _, _, _ = _win32()
        try:
            _, available, _ = win32pipe.PeekNamedPipe(self.pipe, 0)
        except Exception:
            raise _TransportError("pipe-closed") from None
        if not available:
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
            return None
        return _overlapped_read(self.pipe, min(available, max_bytes), deadline)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = (
                self._connect_event,
                self.pipe,
                self.start_event,
                self.stop_event,
                self.parent_handle,
                self._peer_handle,
            )
            self._connect_event = None
            self.pipe = None
            self.start_event = None
            self.stop_event = None
            self.parent_handle = None
            self._peer_handle = None
        keep_connect_event = False
        try:
            _, win32api, _, win32event, win32file, _, _, _, _ = _win32()
            if handles[1] is not None and self._connect_overlapped is not None:
                try:
                    win32file.CancelIoEx(handles[1], self._connect_overlapped)
                    completed = handles[0] is None or win32event.WaitForSingleObject(
                        handles[0], 1000
                    ) == win32event.WAIT_OBJECT_0
                    if not completed:
                        keep_connect_event = True
                        with _ORPHANED_IO_LOCK:
                            _ORPHANED_IO.append(
                                (handles[1], handles[0], self._connect_overlapped)
                            )
                except Exception:
                    pass
            for handle in handles:
                if keep_connect_event and handle is handles[0]:
                    continue
                if handle is not None:
                    try:
                        win32api.CloseHandle(handle)
                    except Exception:
                        pass
        except Exception:
            pass
        if not keep_connect_event:
            self._connect_overlapped = None
        self._read_buffer.clear()


def win32event_wait_dead(process_handle: Any) -> bool:
    _, _, _, win32event, _, _, _, _, _ = _win32()
    return win32event.WaitForSingleObject(process_handle, 0) == win32event.WAIT_OBJECT_0


def _process_image_path(process_handle: Any) -> str:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel32.QueryFullProcessImageNameW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    capacity = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(capacity.value)
    try:
        raw_handle = int(process_handle)
    except (TypeError, ValueError, OverflowError):
        raise _TransportError("peer-validation") from None
    if raw_handle <= 0 or not query(wintypes.HANDLE(raw_handle), 0, buffer, ctypes.byref(capacity)):
        raise _TransportError("peer-validation")
    return os.path.normcase(os.path.abspath(buffer.value))


def _interpreter_image_candidates(package_root: Path, interpreter: Path) -> tuple[str, ...]:
    """Return only this venv executable and its pyvenv.cfg-declared base twin."""
    interpreter = Path(interpreter)
    venv_root = Path(package_root) / ".venv"
    fixed_interpreter = venv_root / "Scripts" / interpreter.name
    if interpreter.resolve(strict=True) != fixed_interpreter.resolve(strict=True):
        raise _LaunchError("interpreter-unavailable")
    candidates = {os.path.normcase(str(interpreter.resolve(strict=True)))}
    config_path = venv_root / "pyvenv.cfg"
    try:
        entries = {}
        for line in config_path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                entries[key.strip().casefold()] = value.strip().strip('"')
        home_text = entries.get("home", "")
        home = Path(home_text)
        if not home_text or not home.is_absolute():
            raise ValueError("missing venv home")
        base_interpreter = home / interpreter.name
        candidates.add(os.path.normcase(str(base_interpreter.resolve(strict=True))))
    except (OSError, RuntimeError, ValueError):
        raise _LaunchError("interpreter-unavailable") from None
    return tuple(sorted(candidates))


def _is_descendant_of(process_id: int, ancestor_id: int) -> bool:
    """Confirm the pipe writer belongs to the ShellExecuteEx process tree."""
    if process_id == ancestor_id:
        return True
    from ctypes import wintypes

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot_fn = kernel32.CreateToolhelp32Snapshot
    snapshot_fn.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snapshot_fn.restype = wintypes.HANDLE
    first_fn = kernel32.Process32FirstW
    first_fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    first_fn.restype = wintypes.BOOL
    next_fn = kernel32.Process32NextW
    next_fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    next_fn.restype = wintypes.BOOL
    close_fn = kernel32.CloseHandle
    close_fn.argtypes = [wintypes.HANDLE]
    close_fn.restype = wintypes.BOOL
    snapshot = snapshot_fn(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return False
    parents: dict[int, int] = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if first_fn(snapshot, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                entry.dwSize = ctypes.sizeof(entry)
                if not next_fn(snapshot, ctypes.byref(entry)):
                    break
    finally:
        close_fn(snapshot)
    seen: set[int] = set()
    current = int(process_id)
    for _ in range(16):
        if current in seen:
            return False
        seen.add(current)
        parent = parents.get(current)
        if parent is None or parent <= 0:
            return False
        if parent == ancestor_id:
            return True
        current = parent
    return False


def _overlapped_read(handle: Any, size: int, deadline: float) -> bytes | None:
    _, _, _, win32event, win32file, _, _, _, _ = _win32()
    event = win32event.CreateEvent(None, True, False, None)
    buffer = win32file.AllocateReadBuffer(int(size))
    overlapped = win32file.OVERLAPPED()
    overlapped.hEvent = event
    overlapped.object = buffer
    refs: tuple[Any, ...] = (handle, event, overlapped, buffer)
    keep_event = False
    pending = False
    try:
        result, data = win32file.ReadFile(handle, buffer, overlapped)
        if result == 997:  # ERROR_IO_PENDING
            pending = True
            remaining = max(0.0, deadline - time.monotonic())
            wait_ms = max(0, min(500, int(remaining * 1000)))
            if win32event.WaitForSingleObject(event, wait_ms) == win32event.WAIT_TIMEOUT:
                keep_event = not _cancel_overlapped(handle, overlapped, event, refs)
                pending = False
                return None
            pending = False
            result = 0
        transferred = win32file.GetOverlappedResult(handle, overlapped, False)
        if isinstance(transferred, tuple):
            transferred = transferred[-1]
        if result not in (0, None):
            raise _TransportError("pipe-read")
        if type(transferred) is not int or transferred < 0 or transferred > int(size):
            raise _TransportError("pipe-read")
        return bytes(buffer[:transferred])
    except _TransportError:
        raise
    except Exception:
        if pending:
            keep_event = not _cancel_overlapped(handle, overlapped, event, refs)
        raise _TransportError("pipe-read") from None
    finally:
        if not keep_event:
            try:
                _, win32api, *_ = _win32()
                win32api.CloseHandle(event)
            except Exception:
                pass


_ORPHANED_IO_LOCK = threading.Lock()
_ORPHANED_IO: list[tuple[Any, ...]] = []


def _cancel_overlapped(handle: Any, overlapped: Any, event: Any, refs: tuple[Any, ...]) -> bool:
    _, _, _, win32event, win32file, *_ = _win32()
    try:
        win32file.CancelIoEx(handle, overlapped)
    except Exception:
        pass
    try:
        complete = win32event.WaitForSingleObject(event, 1000) == win32event.WAIT_OBJECT_0
    except Exception:
        complete = False
    if not complete:
        # Keep the native buffer and OVERLAPPED alive if cancellation completion
        # is delayed; dropping them while the kernel owns them is unsafe.
        with _ORPHANED_IO_LOCK:
            _ORPHANED_IO.append(refs)
    return complete


def _overlapped_write(handle: Any, payload: bytes, timeout: float = _IO_TIMEOUT) -> None:
    _, _, _, win32event, win32file, *_ = _win32()
    event = win32event.CreateEvent(None, True, False, None)
    overlapped = win32file.OVERLAPPED()
    overlapped.hEvent = event
    data = bytes(payload)
    overlapped.object = data
    refs: tuple[Any, ...] = (handle, event, overlapped, data)
    keep_event = False
    pending = False
    try:
        result, written = win32file.WriteFile(handle, data, overlapped)
        if result == 997:  # ERROR_IO_PENDING
            pending = True
            if win32event.WaitForSingleObject(event, max(0, int(timeout * 1000))) == win32event.WAIT_TIMEOUT:
                keep_event = not _cancel_overlapped(handle, overlapped, event, refs)
                pending = False
                raise _TransportError("pipe-write-timeout")
            pending = False
            written = win32file.GetOverlappedResult(handle, overlapped, False)
            if isinstance(written, tuple):
                result, written = written
            else:
                result = 0
        if result not in (0, None):
            raise _TransportError("pipe-write")
        if type(written) is int and written != len(data):
            raise _TransportError("pipe-write")
    except _TransportError:
        raise
    except Exception:
        if pending:
            keep_event = not _cancel_overlapped(handle, overlapped, event, refs)
        raise _TransportError("pipe-write") from None
    finally:
        if not keep_event:
            try:
                _, win32api, *_ = _win32()
                win32api.CloseHandle(event)
            except Exception:
                pass


class GameInputSession:
    """One isolated elevated counting session with bounded lifecycle methods."""

    def __init__(
        self,
        logger: Any,
        *,
        _transport_factory: Callable[[str], Any] | None = None,
        _launcher: Callable[[str, str], _LaunchInfo] | None = None,
        _clock: Callable[[], float] = time.monotonic,
        _startup_timeout: float = _STARTUP_TIMEOUT,
    ):
        self.logger = logger
        self._transport_factory = _transport_factory or _WindowsSessionTransport
        self._launcher = _launcher or _shell_execute_runas
        self._clock = _clock
        self._startup_timeout = max(0.05, min(float(_startup_timeout), 120.0))
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._session = uuid.uuid4().hex
        self._generation = 0
        self._started = False
        self._closed = False
        self._closing = False
        self._stop_requested = False
        self._cancel_pending_launch = False
        self._state = "off"
        self._error: str | None = None
        self._transport: Any = None
        self._launch_info: _LaunchInfo | None = None
        self._launch_done = threading.Event()
        self._worker_done = threading.Event()
        self._worker: threading.Thread | None = None
        self._launch_thread: threading.Thread | None = None
        self._messages: deque[GameInputMessage] = deque()
        self._validator = _MessageValidator(self._session)
        self._terminal_seen = False
        self._start_signaled = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def start(self) -> bool:
        with self._lock:
            if self._started or self._closed or self._stop_requested or self._cancel_pending_launch:
                return self._state not in {"off", "failed"}
            self._started = True
            self._generation += 1
            generation = self._generation
            self._state = "pending"
            self._error = None
            self._worker = threading.Thread(
                target=self._run,
                args=(generation,),
                name="NachoBotGameInputSession",
                daemon=True,
            )
            try:
                self._worker.start()
            except Exception:
                self._state = "failed"
                self._error = "worker-start"
                self._worker_done.set()
                self._log_sanitized(self._error)
                return False
            return True

    def activate(self) -> None:
        with self._lock:
            if self._closed or self._closing or self._state != "prepared":
                return
            if self._start_signaled:
                return
            transport = self._transport
            generation = self._generation
            self._start_signaled = True
        if transport is None:
            return
        try:
            transport.signal_start()
        except _TransportError as exc:
            self._fail_if_current(generation, exc.reason)
        except Exception:
            self._fail_if_current(generation, "start-signal")

    def poll(self) -> tuple[GameInputMessage, ...]:
        with self._lock:
            result: list[GameInputMessage] = []
            while self._messages and len(result) < _MAX_FRAMES_PER_POLL:
                result.append(self._messages.popleft())
            return tuple(result)

    def request_stop(self) -> None:
        with self._lock:
            if self._closed or self._terminal_seen:
                return
            self._stop_requested = True
            if self._state not in {"off", "failed"}:
                self._state = "stopping"
            transport = self._transport
            pending = not self._launch_done.is_set()
            if pending:
                # A late UAC completion must not make this generation active.
                self._cancel_pending_launch = True
                self._generation += 1
            self._condition.notify_all()
        if transport is not None:
            try:
                transport.signal_stop()
            except Exception:
                self._fail_if_current(self._generation, "stop-signal")

    def close(self, timeout: float = 3.0) -> tuple[GameInputMessage, ...]:
        bounded_timeout = max(0.0, min(float(timeout), 3.0))
        with self._lock:
            if self._closed:
                return self.poll()
            if not self._started:
                self._closed = True
                self._generation += 1
                self._state = "off"
                self._condition.notify_all()
                return self.poll()
            pending_uac = self._launch_thread is not None and not self._launch_done.is_set()
            self._closing = True
        self.request_stop()
        if pending_uac:
            # Do not block application shutdown on a consent dialog. Closing the
            # pipe/events makes any late child fail before it can start capture.
            self._finish_close("off", None)
            return self.poll()
        deadline = self._clock() + bounded_timeout
        with self._condition:
            while not self._worker_done.is_set() and self._clock() < deadline:
                self._condition.wait(min(0.05, max(0.0, deadline - self._clock())))
            timed_out = not self._worker_done.is_set()
        if timed_out:
            self._finish_close("failed", "stop-timeout")
        else:
            with self._lock:
                prior_state = self._state
                prior_error = self._error
            self._finish_close(
                "failed" if prior_state == "failed" else "off",
                prior_error if prior_state == "failed" else None,
            )
        return self.poll()

    def _run(self, generation: int) -> None:
        transport = None
        try:
            transport = self._transport_factory(self._session)
            transport.open()
            with self._lock:
                if not self._is_current_locked(generation):
                    transport.close()
                    return
                self._transport = transport
            executable, parameters = transport.launch_spec()
            self._launch_done.clear()
            self._launch_thread = threading.Thread(
                target=self._launch_worker,
                args=(generation, executable, parameters),
                name="NachoBotGameInputUAC",
                daemon=True,
            )
            self._launch_thread.start()
            startup_deadline = self._clock() + self._startup_timeout
            stop_deadline: float | None = None
            peer_validated = False
            while True:
                with self._lock:
                    if not self._is_current_locked(generation):
                        break
                    stop_requested = self._stop_requested
                    launch_info = self._launch_info
                    launch_done = self._launch_done.is_set()
                    state = self._state
                if stop_requested:
                    if stop_deadline is None:
                        stop_deadline = self._clock() + _STOP_TIMEOUT
                    if self._clock() >= stop_deadline:
                        self._fail_if_current(generation, "stop-timeout")
                        break
                elif self._clock() >= startup_deadline and state in {"pending", "prepared"}:
                    self._fail_if_current(generation, "startup-timeout")
                    break
                if launch_done and launch_info is None:
                    with self._lock:
                        has_failure = self._state == "failed"
                    if has_failure:
                        break
                if not transport.wait_for_client(0.05):
                    if launch_done and launch_info is None:
                        self._fail_if_current(generation, "elevation-unavailable")
                        break
                    continue
                if launch_info is None:
                    # ShellExecuteEx may return just after the client connects.
                    self._launch_done.wait(0.05)
                    continue
                if not peer_validated:
                    transport.verify_peer(launch_info)
                    peer_validated = True
                frame = transport.read_frame(0.05)
                if frame is None:
                    helper_alive = getattr(transport, "helper_alive", None)
                    alive = (
                        bool(helper_alive(launch_info))
                        if callable(helper_alive)
                        else (
                            launch_info.process_handle is None
                            or not win32event_wait_dead(launch_info.process_handle)
                        )
                    )
                    if not alive:
                        self._fail_if_current(generation, "helper-exited")
                        break
                    continue
                self._accept_frame(generation, frame)
                with self._lock:
                    if self._terminal_seen or self._state == "failed":
                        break
            if transport is not None:
                transport.close()
        except _TransportError as exc:
            self._fail_if_current(generation, exc.reason)
            if transport is not None:
                transport.close()
        except _LaunchError as exc:
            self._fail_if_current(generation, exc.reason)
            if transport is not None:
                transport.close()
        except Exception:
            self._fail_if_current(generation, "transport-error")
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
        finally:
            with self._lock:
                if self._transport is transport:
                    self._transport = None
                launch_info = self._launch_info
                self._launch_info = None
                if self._state == "stopping" and not self._terminal_seen and not self._closed:
                    self._state = "off"
                self._worker_done.set()
                self._condition.notify_all()
            if launch_info is not None:
                _close_process(launch_info.process_handle)

    def _launch_worker(self, generation: int, executable: str, parameters: str) -> None:
        try:
            with self._lock:
                if self._closed or self._cancel_pending_launch or generation != self._generation:
                    return
            info = self._launcher(executable, parameters)
            if not isinstance(info, _LaunchInfo) or int(info.pid) <= 0:
                raise _LaunchError("launch-failed")
            stale = False
            with self._lock:
                if self._closed or self._cancel_pending_launch or generation != self._generation:
                    stale = True
                else:
                    self._launch_info = info
                    self._launch_done.set()
                    self._condition.notify_all()
            if stale:
                _close_process(info.process_handle)
        except _LaunchError as exc:
            with self._lock:
                if not self._closed and generation == self._generation:
                    self._error = exc.reason
                    self._state = "failed"
                    self._log_sanitized(exc.reason)
                    self._condition.notify_all()
        except Exception:
            with self._lock:
                if not self._closed and generation == self._generation:
                    self._error = "launch-failed"
                    self._state = "failed"
                    self._log_sanitized(self._error)
                    self._condition.notify_all()
        finally:
            self._launch_done.set()

    def _accept_frame(self, generation: int, frame: bytes) -> None:
        try:
            message = _decode_message(frame, self._session)
            with self._lock:
                if not self._is_current_locked(generation) or self._terminal_seen:
                    return
                self._validator.accept(message)
                if message.status == "prepared":
                    if not self._stop_requested:
                        self._state = "prepared"
                elif message.status == "ready":
                    if not self._stop_requested:
                        self._state = "active"
                elif message.status == "failed":
                    self._terminal_seen = True
                    self._state = "failed"
                    self._error = "helper-failed"
                elif message.status == "final":
                    self._terminal_seen = True
                    self._state = "off"
                self._queue_message_locked(message)
                self._condition.notify_all()
                if self._error:
                    self._log_sanitized(self._error)
        except ValueError:
            self._fail_if_current(generation, _PROTOCOL_ERROR)

    def _queue_message_locked(self, message: GameInputMessage) -> None:
        # Snapshots are cumulative, so replacing a queued snapshot bounds memory
        # without losing aggregate counts or terminal messages.
        if message.status == "snapshot" and self._messages and self._messages[-1].status == "snapshot":
            self._messages[-1] = message
            return
        self._messages.append(message)

    def _fail_if_current(self, generation: int, reason: str) -> None:
        safe_reason = reason if reason in {
            "windows-only",
            "windows-api-unavailable",
            "token-validation",
            "process-validation",
            "session-validation",
            "pipe-setup",
            "interpreter-unavailable",
            "pipe-connect",
            "peer-validation",
            "helper-exited",
            "invalid-message",
            "pipe-closed",
            "pipe-read",
            "pipe-read-timeout",
            "pipe-write",
            "pipe-write-timeout",
            "transport-closed",
            "start-signal",
            "stop-signal",
            "elevation-cancelled",
            "elevation-unavailable",
            "launch-failed",
            "worker-start",
            "startup-timeout",
            "stop-timeout",
            "transport-error",
        } else "transport-error"
        with self._lock:
            if generation != self._generation or self._closed:
                return
            if self._state == "failed" and self._error is not None:
                return
            self._state = "failed"
            self._error = safe_reason
            self._terminal_seen = True
            transport = self._transport
            self._condition.notify_all()
        self._log_sanitized(safe_reason)
        if transport is not None:
            try:
                transport.signal_stop()
            except Exception:
                pass

    def _finish_close(self, state: str, error: str | None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._state = state
            self._error = error
            transport = self._transport
            self._transport = None
            self._condition.notify_all()
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _is_current_locked(self, generation: int) -> bool:
        return (
            not self._closed
            and not self._cancel_pending_launch
            and generation == self._generation
        )

    def _log_sanitized(self, reason: str) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            logger.warning("[GameInputSession] {}", reason)
        except Exception:
            pass


def _close_process(handle: Any) -> None:
    if handle is None:
        return
    try:
        _, win32api, *_ = _win32()
        win32api.CloseHandle(handle)
    except Exception:
        pass


def _shell_execute_runas(executable: str, parameters: str) -> _LaunchInfo:
    """Launch only the fixed helper command on the UAC worker thread."""
    if os.name != "nt":
        raise _LaunchError("windows-only")
    from ctypes import wintypes

    class _ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HANDLE),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # NOCLOSEPROCESS | NOASYNC
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = parameters
    info.lpDirectory = str(Path(executable).parent.parent.parent)
    info.nShow = 0  # SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:  # ERROR_CANCELLED
            raise _LaunchError("elevation-cancelled")
        raise _LaunchError("launch-failed")
    handle = info.hProcess
    try:
        _, _, _, _, _, _, win32process, _, _ = _win32()
        pid = int(win32process.GetProcessId(handle))
    except Exception:
        _close_process(handle)
        raise _LaunchError("launch-failed") from None
    return _LaunchInfo(pid, handle)


class _HelperArguments:
    def __init__(self, values: dict[str, str]):
        self.values = values


def _parse_helper_arguments(argv: list[str]) -> _HelperArguments:
    expected = {
        "--gih-helper",
        "--pipe",
        "--start",
        "--stop",
        "--session",
        "--parent-pid",
        "--parent-created",
        "--parent-sid",
        "--parent-session",
    }
    if not argv or argv[0] != "--gih-helper":
        raise _TransportError("helper-arguments")
    values: dict[str, str] = {}
    index = 1
    while index < len(argv):
        key = argv[index]
        if key not in expected - {"--gih-helper"} or key in values or index + 1 >= len(argv):
            raise _TransportError("helper-arguments")
        value = argv[index + 1]
        if not value or len(value) > 512:
            raise _TransportError("helper-arguments")
        values[key] = value
        index += 2
    if set(values) != expected - {"--gih-helper"}:
        raise _TransportError("helper-arguments")
    try:
        if int(values["--parent-pid"]) <= 0 or int(values["--parent-session"]) < 0:
            raise ValueError
    except ValueError:
        raise _TransportError("helper-arguments") from None
    if not values["--session"].isalnum() or len(values["--session"]) != 32:
        raise _TransportError("helper-arguments")
    return _HelperArguments(values)


class _NullLogger:
    """Prevent the existing counter from logging exception/input details."""

    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None

    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None


def _helper_check_identity(options: _HelperArguments) -> tuple[Any, Any, Any, Any, Any, Any]:
    (
        _,
        win32api,
        win32con,
        win32event,
        win32file,
        win32pipe,
        win32process,
        _,
        _,
    ) = _win32()
    values = options.values
    parent_pid = int(values["--parent-pid"])
    parent_session = int(values["--parent-session"])
    parent_handle = win32api.OpenProcess(
        win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        parent_pid,
    )
    current_pid = int(win32process.GetCurrentProcessId())
    start_event = stop_event = None
    try:
        current_sid, elevated = _token_identity(win32api.GetCurrentProcess())
        parent_sid, _ = _token_identity(parent_handle)
        _validate_parent_identity(
            current_sid=current_sid,
            parent_sid=parent_sid,
            expected_sid=values["--parent-sid"],
            elevated=elevated,
            current_creation=_process_creation_identity(parent_handle),
            expected_creation=values["--parent-created"],
            current_session=_session_id(current_pid),
            expected_session=parent_session,
            parent_alive=win32event.WaitForSingleObject(parent_handle, 0)
            == win32event.WAIT_TIMEOUT
            and _session_id(parent_pid) == parent_session,
        )
        start_event = win32event.OpenEvent(win32con.SYNCHRONIZE, False, values["--start"])
        stop_event = win32event.OpenEvent(win32con.SYNCHRONIZE, False, values["--stop"])
        return parent_handle, start_event, stop_event, win32file, win32pipe, win32event
    except _TransportError:
        for handle in (start_event, stop_event, parent_handle):
            if handle is not None:
                try:
                    win32api.CloseHandle(handle)
                except Exception:
                    pass
        raise
    except Exception:
        for handle in (start_event, stop_event, parent_handle):
            if handle is not None:
                try:
                    win32api.CloseHandle(handle)
                except Exception:
                    pass
        raise _TransportError("identity-validation") from None


def _helper_wait_start(stop_event: Any, start_event: Any, parent_handle: Any, win32event: Any) -> str:
    handles = (stop_event, parent_handle, start_event)
    result = win32event.WaitForMultipleObjects(handles, False, 30000)
    if result == win32event.WAIT_TIMEOUT:
        return "timeout"
    if result == win32event.WAIT_OBJECT_0:
        return "stop"
    if result == win32event.WAIT_OBJECT_0 + 1:
        return "parent"
    if result == win32event.WAIT_OBJECT_0 + 2:
        # STOP wins if it races with START.
        if win32event.WaitForSingleObject(stop_event, 0) == win32event.WAIT_OBJECT_0:
            return "stop"
        if win32event.WaitForSingleObject(parent_handle, 0) != win32event.WAIT_TIMEOUT:
            return "parent"
        return "start"
    return "error"


def _helper_connect_and_validate(
    options: _HelperArguments,
    parent_handle: Any,
    stop_event: Any,
    win32file: Any,
    win32pipe: Any,
    win32event: Any,
) -> Any:
    values = options.values
    deadline = time.monotonic() + 10.0
    pipe = None
    while time.monotonic() < deadline:
        if win32event.WaitForSingleObject(stop_event, 0) == win32event.WAIT_OBJECT_0:
            raise _TransportError("stop-before-connect")
        if win32event.WaitForSingleObject(parent_handle, 0) != win32event.WAIT_TIMEOUT:
            raise _TransportError("parent-exited")
        try:
            pipe = win32file.CreateFile(
                values["--pipe"],
                0x40000000,  # GENERIC_WRITE; the helper cannot read the pipe.
                0,
                None,
                3,  # OPEN_EXISTING
                0x40000000,  # FILE_FLAG_OVERLAPPED
                None,
            )
            break
        except Exception as exc:
            if getattr(exc, "winerror", None) not in {2, 231, 121}:
                raise _TransportError("pipe-connect") from None
            try:
                win32pipe.WaitNamedPipe(values["--pipe"], 100)
            except Exception:
                pass
    if pipe is None:
        raise _TransportError("pipe-connect-timeout")
    try:
        server_pid = int(win32pipe.GetNamedPipeServerProcessId(pipe))
        server_session = int(win32pipe.GetNamedPipeServerSessionId(pipe))
        if server_pid != int(values["--parent-pid"]) or server_session != int(
            values["--parent-session"]
        ):
            raise _TransportError("server-validation")
        if win32event.WaitForSingleObject(parent_handle, 0) != win32event.WAIT_TIMEOUT:
            raise _TransportError("parent-exited")
        if win32event.WaitForSingleObject(stop_event, 0) == win32event.WAIT_OBJECT_0:
            raise _TransportError("stop-before-connect")
        return pipe
    except Exception:
        try:
            _, win32api, *_ = _win32()
            win32api.CloseHandle(pipe)
        except Exception:
            pass
        raise


def _helper_send(pipe: Any, message: GameInputMessage) -> None:
    frame = _encode_message(message)
    deadline = time.monotonic() + _IO_TIMEOUT
    offset = 0
    while offset < len(frame):
        chunk = frame[offset:]
        _overlapped_write(pipe, chunk, max(0.0, deadline - time.monotonic()))
        offset = len(frame)


def _helper_snapshot(counter: Any) -> tuple[int, int]:
    counts = counter.snapshot()
    return int(counts.keyboard_presses), int(counts.mouse_clicks)


def _collect_helper(options: _HelperArguments) -> int:
    parent_handle = start_event = stop_event = pipe = None
    counter = None
    sequence = 0
    keyboard = mouse = 0

    def send(status: str) -> None:
        nonlocal sequence, keyboard, mouse
        sequence += 1
        _helper_send(
            pipe,
            GameInputMessage(_VERSION, options.values["--session"], sequence, status, keyboard, mouse),
        )

    try:
        parent_handle, start_event, stop_event, win32file, win32pipe, win32event = (
            _helper_check_identity(options)
        )
        pipe = _helper_connect_and_validate(
            options, parent_handle, stop_event, win32file, win32pipe, win32event
        )
        send("prepared")
        start_result = _helper_wait_start(stop_event, start_event, parent_handle, win32event)
        if start_result == "stop":
            send("final")
            return 0
        if start_result != "start":
            send("failed")
            return 1
        if win32event.WaitForSingleObject(stop_event, 0) == win32event.WAIT_OBJECT_0:
            send("final")
            return 0
        if win32event.WaitForSingleObject(parent_handle, 0) != win32event.WAIT_TIMEOUT:
            return 0

        # Keep the host counter and every non-input dependency out of the helper
        # process until START has been authenticated and observed.
        from desktop_pet_adapter.input_counter import InputCounter

        counter = InputCounter(None, _NullLogger())
        if not counter.start():
            keyboard, mouse = _helper_snapshot(counter)
            send("failed")
            return 1
        keyboard, mouse = _helper_snapshot(counter)
        if win32event.WaitForSingleObject(stop_event, 0) == win32event.WAIT_OBJECT_0:
            counter.stop()
            keyboard, mouse = _helper_snapshot(counter)
            send("final")
            return 0
        if win32event.WaitForSingleObject(parent_handle, 0) != win32event.WAIT_TIMEOUT:
            counter.stop()
            return 0
        send("ready")
        next_tick = time.monotonic() + _POLL_INTERVAL
        while True:
            wait_ms = max(0, min(20, int((next_tick - time.monotonic()) * 1000)))
            result = win32event.WaitForMultipleObjects(
                (stop_event, parent_handle), False, wait_ms
            )
            if result in (win32event.WAIT_OBJECT_0, win32event.WAIT_OBJECT_0 + 1):
                break
            if result not in (win32event.WAIT_TIMEOUT,):
                raise _TransportError("stop-wait")
            if not counter.active:
                keyboard, mouse = _helper_snapshot(counter)
                send("failed")
                return 1
            now = time.monotonic()
            if now >= next_tick:
                keyboard, mouse = _helper_snapshot(counter)
                send("snapshot")
                next_tick = now + _POLL_INTERVAL
        counter.stop()
        keyboard, mouse = _helper_snapshot(counter)
        send("final")
        return 0
    except Exception:
        if counter is not None:
            try:
                counter.stop()
                keyboard, mouse = _helper_snapshot(counter)
            except Exception:
                pass
        if pipe is not None:
            try:
                send("failed")
            except Exception:
                pass
        return 1
    finally:
        if counter is not None:
            try:
                if counter.active:
                    counter.stop()
            except Exception:
                pass
        try:
            _, win32api, *_ = _win32()
            for handle in (pipe, start_event, stop_event, parent_handle):
                if handle is not None:
                    try:
                        win32api.CloseHandle(handle)
                    except Exception:
                        pass
        except Exception:
            pass


def _collector_cli(argv: list[str] | None = None) -> int:
    try:
        options = _parse_helper_arguments(list(sys.argv[1:] if argv is None else argv))
        return _collect_helper(options)
    except _TransportError:
        return 1
    except Exception:
        return 1


if __name__ == "__main__":  # pragma: no cover - fixed -c bootstrap is the supported entry.
    raise SystemExit(_collector_cli())

