"""Process identity helpers used to recover controller crashes safely."""

from __future__ import annotations

from pathlib import Path
import os
import json


def _windows_process_identity(pid: int) -> dict | None:
    """Read the kernel process creation FILETIME without a host dependency."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error in {87, 1168}:  # invalid parameter / not found
            return None
        if error == 5:  # access denied: retain the conservative unknown state
            return {"pid": pid, "created": None, "unknown": True}
        raise OSError(error, "OpenProcess failed")
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(handle, creation, exit_time, kernel_time, user_time):
            error = ctypes.get_last_error()
            if error == 5:
                return {"pid": pid, "created": None, "unknown": True}
            if error in {87, 1168}:
                return None
            raise OSError(error, "GetProcessTimes failed")
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        created = ticks / 10_000_000 - 11_644_473_600
        return {"pid": pid, "created": created}
    finally:
        kernel32.CloseHandle(handle)


def process_identity(pid: int) -> dict | None:
    """Return a PID plus creation-time identity, or None when it is gone."""
    if os.name == "nt":
        return _windows_process_identity(pid)
    try:
        import psutil

        process = psutil.Process(pid)
        return {"pid": process.pid, "created": process.create_time()}
    except ImportError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return {"pid": pid, "created": None, "unknown": True}
        return {"pid": pid, "created": None, "unknown": True}
    except Exception as exc:
        try:
            import psutil

            if isinstance(exc, (psutil.NoSuchProcess, psutil.ZombieProcess)):
                return None
            if isinstance(exc, psutil.AccessDenied):
                # Unknown is handled conservatively by callers as still alive.
                return {"pid": pid, "created": None, "unknown": True}
        except ImportError:
            pass
        raise


def worker_identity(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text("utf-8"))
        pid = int(value["pid"])
        created = float(value["created"])
        return {"pid": pid, "created": created}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def worker_is_same_process(identity: dict | None) -> bool | None:
    """True means the exact worker is alive, False means absent/reused, None unknown."""
    if not identity:
        return False
    current = process_identity(int(identity["pid"]))
    if current is None:
        return False
    if current.get("unknown"):
        return None
    return abs(float(current["created"]) - float(identity["created"])) < 0.02
