"""Narrow Windows process and launch primitives used by manager.py."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import ipaddress
import json
import ntpath
import os
import re
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psutil

from .snowluma import safe_auth_failure_stage


_MAX_PUBLIC_COMMAND_LINE_CHARS = 32_767
_MAX_PUBLIC_COMMAND_LINE_ARGS = 4_096
_SNOWLUMA_PINNED_QQ_BUILD = "9.9.26-44343"
_SNOWLUMA_PINNED_WRAPPER_SHA256 = "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2"
_NAMED_PIPE_ENUMERATION_LIMIT = 4096
_TCP_CONNECTION_SNAPSHOT_LIMIT = 65_536
_TCP_LISTENER_MATCH_LIMIT = 32
_ERROR_FILE_NOT_FOUND = 2
_ERROR_NO_MORE_FILES = 18


class _Win32FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _Win32FindDataW(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", _Win32FileTime),
        ("ftLastAccessTime", _Win32FileTime),
        ("ftLastWriteTime", _Win32FileTime),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("dwReserved0", wintypes.DWORD),
        ("dwReserved1", wintypes.DWORD),
        ("cFileName", wintypes.WCHAR * 260),
        ("cAlternateFileName", wintypes.WCHAR * 14),
        ("dwFileType", wintypes.DWORD),
        ("dwCreatorType", wintypes.DWORD),
        ("wFinderFlags", wintypes.WORD),
    ]


class WindowsServiceError(RuntimeError):
    def __init__(self, code: str, *, diagnostic_stage: str | None = None):
        self.code = code
        self._diagnostic_stage = safe_auth_failure_stage(code, diagnostic_stage)
        super().__init__(code)


_PROCESS_QUERY_ERRORS = (
    WindowsServiceError,
    OSError,
    AttributeError,
    TypeError,
    ValueError,
    ctypes.ArgumentError,
)


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    parent_pid: int
    image_path: str
    command_line: str
    creation_time_100ns: int

    def public_binding(self) -> dict[str, int | str]:
        return {
            "pid": self.pid,
            "parentPid": self.parent_pid,
            "imagePath": self.image_path,
            "creationTime100ns": str(self.creation_time_100ns),
        }


@dataclass(frozen=True, slots=True)
class TargetBinding:
    adapter_pid: int
    adapter_creation_time_100ns: int
    kernel_pid: int
    kernel_creation_time_100ns: int
    app_path: str


@dataclass(frozen=True, slots=True)
class _SnowLumaBindingProof:
    account_id: str
    onebot_url: str
    endpoint: tuple[str, int, str]
    runtime_path: str
    listener_pid: int
    listener_creation_time_100ns: int
    listener_image_path: str
    listener_working_directory: str
    listener_command: tuple[str, ...]
    kernel_pid: int
    kernel_creation_time_100ns: int
    kernel_image_path: str
    qqnt_module_path: str
    wrapper_module_path: str


@dataclass(frozen=True, slots=True)
class HostLaunch:
    launcher_pid: int
    run_directory: str
    profile_path: str
    report_path: str
    host_generation: int = 0
    launcher_creation_time_100ns: int = 0
    launcher_image_path: str = ""


_HOST_LAUNCH_OUTPUT_LIMIT = 64 * 1024
_HOST_LAUNCH_REAP_TIMEOUT_SECONDS = 5.0
_HOST_LAUNCH_POLL_SECONDS = 0.2


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non_finite_json_value")


def _read_host_launch_receipt(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if (path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400 or
                not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or
                info.st_size > _HOST_LAUNCH_OUTPUT_LIMIT):
            raise WindowsServiceError("host_launch_failed")
        with path.open("rb") as stream:
            raw = stream.read(_HOST_LAUNCH_OUTPUT_LIMIT + 1)
        if not raw or len(raw) > _HOST_LAUNCH_OUTPUT_LIMIT:
            raise WindowsServiceError("host_launch_failed")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except WindowsServiceError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise WindowsServiceError("host_launch_failed") from None
    if type(value) is not dict:
        raise WindowsServiceError("host_launch_failed")
    return value


def _run_process_with_file_stdout(
    command: list[str],
    stdout_file: Any,
    *,
    timeout_seconds: float,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[], None] | None = None,
) -> int:
    """Wait only for the immediate child; descendants may keep stdout open."""
    if should_stop is not None and should_stop():
        raise WindowsServiceError("service_stop_requested")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise WindowsServiceError("host_launch_failed") from None

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    stop_code = "host_launch_failed"
    process_reaped = False
    try:
        while True:
            if should_stop is not None and should_stop():
                stop_code = "service_stop_requested"
                break
            if on_progress is not None:
                on_progress()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                returncode = process.wait(timeout=min(_HOST_LAUNCH_POLL_SECONDS, remaining))
                process_reaped = True
                return returncode
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                break

        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_HOST_LAUNCH_REAP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            raise WindowsServiceError("host_launch_failed") from None
        process_reaped = True
        raise WindowsServiceError(stop_code)
    except BaseException:
        # Callback failures must not strand the immediate PowerShell child.
        if not process_reaped and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=_HOST_LAUNCH_REAP_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                raise WindowsServiceError("host_launch_failed") from None
            process_reaped = True
        raise


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.realpath(value)).rstrip("\\/")


def _path_is_under(candidate: str, root: str) -> bool:
    candidate_key = _path_key(candidate)
    root_key = _path_key(root)
    return candidate_key == root_key or candidate_key.startswith(root_key + os.sep)


def _descendants(parent_map: dict[int, int], root_pid: int) -> set[int]:
    found: set[int] = set()
    frontier = {root_pid}
    for _ in range(1024):
        children = {pid for pid, parent in parent_map.items() if parent in frontier and pid != root_pid and pid not in found}
        if not children:
            return found
        found.update(children)
        if len(found) > 1024:
            raise WindowsServiceError("process_tree_too_large")
        frontier = children
    raise WindowsServiceError("process_tree_invalid")


def _option(command_line: str, name: str) -> str | None:
    match = re.search(
        rf"(?:^|\s)--{re.escape(name)}(?:=|\s+)(?:\"([^\"]*)\"|'([^']*)'|([^\s\"]+))",
        command_line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return next((group for group in match.groups() if group is not None), None)


_APP_PATH_FLAG = re.compile(r"(?:^|\s)--app-path(?=$|\s|=)", re.IGNORECASE)
_APP_PATH_VALUE = re.compile(
    r"(?:^|\s)--app-path(?:=|\s+)(?:\"([^\"]*)\"|'([^']*)'|([^\s\"']+))",
    re.IGNORECASE,
)


def _app_path_arguments(command_line: str) -> tuple[bool, tuple[str, ...], bool]:
    flags = list(_APP_PATH_FLAG.finditer(command_line))
    if not flags:
        return False, (), False
    matches = list(_APP_PATH_VALUE.finditer(command_line))
    if len(matches) != len(flags):
        return True, (), True
    values = tuple(next((group for group in match.groups() if group is not None), "") for match in matches)
    return True, values, any(not value.strip() for value in values)


def _is_node_service(process: ProcessRecord) -> bool:
    command = process.command_line.casefold()
    return "--type=utility" in command and "--utility-sub-type=node.mojom.nodeservice" in command


_HOST_PROCESS_TYPES = {"ppapi", "ppapi-broker", "renderer"}


def _host_process_type(command_line: str) -> str:
    value = _option(command_line, "type")
    if value is None:
        return "other"
    normalized = value.casefold()
    return normalized if normalized in _HOST_PROCESS_TYPES else "other"


class WindowsSystem:
    """Read process identities with GetProcessTimes and stop only pinned Hosts."""

    def __init__(self, *, powershell: str = "powershell.exe"):
        self.powershell = powershell
        self._snowluma_root = Path(__file__).resolve().parents[2]
        self._snowluma_pinned_binding: tuple[tuple[Any, ...], _SnowLumaBindingProof] | None = None
        self._snowluma_wrapper_digest_cache: tuple[
            str, tuple[int, int, int, int, int, int], str,
        ] | None = None

    @staticmethod
    def _require_windows() -> None:
        if os.name != "nt":
            raise WindowsServiceError("platform_unsupported")

    def _run_powershell(self, command: str, *, timeout: float = 8.0) -> str:
        self._require_windows()
        try:
            result = subprocess.run(
                [self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
                text=True,
                encoding="utf-8",
                errors="strict",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            raise WindowsServiceError("windows_query_unavailable") from None
        if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 8 * 1024 * 1024:
            raise WindowsServiceError("windows_query_unavailable")
        return result.stdout.strip()

    def _kernel32(self):
        self._require_windows()
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (OSError, AttributeError):
            raise WindowsServiceError("process_query_unavailable") from None
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateProcess.restype = ctypes.c_int
        return kernel32

    @staticmethod
    def _filetime_value(filetime: Any) -> int:
        return (int(filetime.high) << 32) | int(filetime.low)

    def process_identity(self, pid: int) -> tuple[int, str] | None:
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        kernel32 = self._kernel32()
        handle = kernel32.OpenProcess(0x00100000 | 0x00001000, 0, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            if kernel32.WaitForSingleObject(handle, 0) != 0x00000102:
                return None
            image = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(len(image))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)) or size.value == 0:
                return None
            return self._filetime_value(created), image.value
        finally:
            kernel32.CloseHandle(handle)

    def process_is_current(self, pid: int, creation_time_100ns: int) -> bool | None:
        identity = self.process_identity(pid)
        if identity is not None:
            return identity[0] == creation_time_100ns

        # A fresh OpenProcess error distinguishes a vanished PID from a live
        # process that this account cannot query. In particular, never treat
        # ERROR_ACCESS_DENIED as evidence that the recorded process is gone.
        try:
            kernel32 = self._kernel32()
            handle = kernel32.OpenProcess(0x00100000 | 0x00001000, 0, pid)
        except _PROCESS_QUERY_ERRORS:
            return None
        if not handle:
            try:
                last_error = ctypes.get_last_error()
            except (AttributeError, OSError, TypeError, ValueError):
                return None
            return False if last_error == 87 else None  # ERROR_INVALID_PARAMETER

        result: bool | None = None
        try:
            class FILETIME(ctypes.Structure):
                _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

            created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel_time), ctypes.byref(user_time),
            ):
                return None
            actual = (int(created.high) << 32) | int(created.low)
            wait_result = kernel32.WaitForSingleObject(handle, 0)
            if wait_result == 0:
                # The opened process has exited; the recorded incarnation can
                # no longer trigger a Host-owned descendant.
                result = False
            elif wait_result == 0x00000102:
                result = actual == creation_time_100ns
        except _PROCESS_QUERY_ERRORS:
            result = None
        finally:
            try:
                close_ok = bool(kernel32.CloseHandle(handle))
            except _PROCESS_QUERY_ERRORS:
                close_ok = False
        return result if close_ok else None

    def discover_qq_executable(self) -> str:
        """Discover an installation path, never an injection target or account."""
        images: dict[str, str] = {}
        for record in self.process_table().values():
            image = record.image_path
            if record.creation_time_100ns <= 0 or ntpath.basename(image).casefold() != "qq.exe":
                continue
            if not ntpath.isabs(image):
                continue
            images.setdefault(_path_key(image), image)
        if not images:
            raise WindowsServiceError("qq_executable_not_found")
        if len(images) != 1:
            raise WindowsServiceError("qq_executable_ambiguous")
        return next(iter(images.values()))

    def process_table(self) -> dict[int, ProcessRecord]:
        raw = self._run_powershell(
            "$ErrorActionPreference='Stop'; Get-CimInstance -ClassName Win32_Process "
            "-Property ProcessId,ParentProcessId,ExecutablePath,CommandLine | "
            "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress -Depth 3",
            timeout=10.0,
        )
        if not raw:
            raise WindowsServiceError("process_snapshot_unavailable")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise WindowsServiceError("process_snapshot_invalid") from None
        rows = value if isinstance(value, list) else [value]
        if len(rows) > 16384:
            raise WindowsServiceError("process_snapshot_too_large")
        result: dict[int, ProcessRecord] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise WindowsServiceError("process_snapshot_invalid")
            pid, parent = row.get("ProcessId"), row.get("ParentProcessId")
            image, command = row.get("ExecutablePath"), row.get("CommandLine")
            if type(pid) is not int or pid <= 0 or type(parent) is not int or parent < 0:
                continue
            identity = self.process_identity(pid)
            if identity is None:
                continue
            has_wmi_image = isinstance(image, str) and bool(image)
            if has_wmi_image and _path_key(identity[1]) != _path_key(image):
                continue
            # QueryFullProcessImageNameW fills a missing WMI image using the
            # same limited-information handle used for FILETIME. If WMI omitted
            # either image or command line, discard its command text: the QQ
            # binder will request a fresh public psutil cmdline only for this
            # verified QQ image and its validated process family.
            has_wmi_command = (
                isinstance(command, str)
                and bool(command.strip())
                and "\x00" not in command
                and len(command) <= _MAX_PUBLIC_COMMAND_LINE_CHARS
            )
            command_line = command if has_wmi_image and has_wmi_command else ""
            result[pid] = ProcessRecord(pid, parent, identity[1], command_line, identity[0])
        return result

    @staticmethod
    def _serialize_public_command_line(argv: Any) -> str:
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > _MAX_PUBLIC_COMMAND_LINE_ARGS
            or any(type(argument) is not str for argument in argv)
            or any(not argument or "\x00" in argument for argument in argv)
        ):
            raise WindowsServiceError("target_process_unavailable")
        try:
            command_line = subprocess.list2cmdline(argv)
        except (TypeError, ValueError):
            raise WindowsServiceError("target_process_unavailable") from None
        if (
            not isinstance(command_line, str)
            or not command_line
            or "\x00" in command_line
            or len(command_line) > _MAX_PUBLIC_COMMAND_LINE_CHARS
        ):
            raise WindowsServiceError("target_process_unavailable")
        return command_line

    def _public_command_line_for_record(
        self,
        record: ProcessRecord,
        processes: dict[int, ProcessRecord],
    ) -> str:
        """Read a missing QQ command line and pin both the process and its parent."""
        self._require_windows()
        try:
            process = psutil.Process(record.pid)
        except (psutil.Error, OSError):
            raise WindowsServiceError("target_process_unavailable") from None

        before = self.process_identity(record.pid)
        if (
            before is None
            or before[0] != record.creation_time_100ns
            or _path_key(before[1]) != _path_key(record.image_path)
        ):
            raise WindowsServiceError("target_process_changed")
        try:
            parent_pid_before = process.ppid()
        except (psutil.Error, OSError):
            raise WindowsServiceError("target_process_unavailable") from None
        if parent_pid_before != record.parent_pid:
            raise WindowsServiceError("target_process_changed")

        parent_record = processes.get(record.parent_pid) if record.parent_pid else None
        parent_before: tuple[int, str] | None = None
        if record.parent_pid:
            parent_before = self.process_identity(record.parent_pid)
            if parent_before is None:
                raise WindowsServiceError("target_process_tree_invalid")
            if parent_before[0] > record.creation_time_100ns:
                raise WindowsServiceError("target_process_tree_invalid")
            if parent_record is not None and (
                parent_before[0] != parent_record.creation_time_100ns
                or _path_key(parent_before[1]) != _path_key(parent_record.image_path)
            ):
                raise WindowsServiceError("target_process_changed")

        try:
            argv = process.cmdline()
        except (psutil.Error, OSError):
            raise WindowsServiceError("target_process_unavailable") from None
        command_line = self._serialize_public_command_line(argv)

        try:
            parent_pid_after = process.ppid()
        except (psutil.Error, OSError):
            raise WindowsServiceError("target_process_unavailable") from None
        after = self.process_identity(record.pid)
        if (
            parent_pid_after != record.parent_pid
            or after is None
            or after[0] != before[0]
            or _path_key(after[1]) != _path_key(before[1])
        ):
            raise WindowsServiceError("target_process_changed")
        if record.parent_pid:
            parent_after = self.process_identity(record.parent_pid)
            if (
                parent_before is None
                or parent_after is None
                or parent_after[0] != parent_before[0]
                or _path_key(parent_after[1]) != _path_key(parent_before[1])
            ):
                raise WindowsServiceError("target_process_changed")
        return command_line

    def listener_owners(self, port: int) -> list[int]:
        if type(port) is not int or not 1 <= port <= 65535:
            raise WindowsServiceError("listener_port_invalid")
        try:
            self._require_windows()
        except WindowsServiceError:
            raise
        try:
            snapshot = psutil.net_connections(kind="tcp")
        except Exception:
            raise WindowsServiceError("windows_query_unavailable") from None

        if not isinstance(snapshot, (list, tuple)) or len(snapshot) > _TCP_CONNECTION_SNAPSHOT_LIMIT:
            raise WindowsServiceError("listener_snapshot_invalid") from None

        owners: set[int] = set()
        matching_rows = 0
        for connection in snapshot:
            try:
                family = connection.family
                socktype = connection.type
                status = connection.status
            except Exception:
                raise WindowsServiceError("listener_snapshot_invalid") from None

            if (
                isinstance(family, bool)
                or not isinstance(family, int)
                or family not in (socket.AF_INET, socket.AF_INET6)
                or isinstance(socktype, bool)
                or not isinstance(socktype, int)
                or socktype != socket.SOCK_STREAM
                or not isinstance(status, str)
            ):
                raise WindowsServiceError("listener_snapshot_invalid")
            if status != psutil.CONN_LISTEN:
                continue

            try:
                local_address = connection.laddr
                valid_address_shape = (
                    isinstance(local_address, tuple)
                    and (
                        len(local_address) == 2
                        or (family == socket.AF_INET6 and len(local_address) == 4)
                    )
                )
                host = local_address[0] if valid_address_shape else None
                local_port = local_address[1] if valid_address_shape else None
                if not isinstance(host, str) or not host:
                    raise ValueError
                parsed_host = ipaddress.ip_address(host)
                if (family == socket.AF_INET and parsed_host.version != 4) or (
                    family == socket.AF_INET6 and parsed_host.version != 6
                ):
                    raise ValueError
                if type(local_port) is not int or not 1 <= local_port <= 65535:
                    raise ValueError
            except Exception:
                raise WindowsServiceError("listener_snapshot_invalid") from None

            if local_port != port:
                continue
            matching_rows += 1
            if matching_rows > _TCP_LISTENER_MATCH_LIMIT:
                raise WindowsServiceError("listener_snapshot_invalid")
            try:
                pid = connection.pid
            except Exception:
                raise WindowsServiceError("listener_snapshot_invalid") from None
            if type(pid) is not int or pid <= 0:
                raise WindowsServiceError("listener_snapshot_invalid")
            owners.add(pid)

        return sorted(owners)

    @staticmethod
    def _validated_ancestors(
        owner: ProcessRecord,
        processes: dict[int, ProcessRecord],
        expected_exe: str,
    ) -> list[ProcessRecord]:
        ancestors: list[ProcessRecord] = []
        visited = {owner.pid}
        child = owner
        external_boundary_seen = False
        for _ in range(64):
            if child.creation_time_100ns <= 0 or not child.image_path:
                raise WindowsServiceError("target_process_tree_invalid")
            parent_pid = child.parent_pid
            if not parent_pid:
                return ancestors
            if parent_pid in visited:
                raise WindowsServiceError("target_process_tree_invalid")
            parent = processes.get(parent_pid)
            if parent is None:
                if external_boundary_seen:
                    return ancestors
                raise WindowsServiceError("target_process_tree_invalid")
            if parent.creation_time_100ns <= 0 or not parent.image_path:
                raise WindowsServiceError("target_process_tree_invalid")
            if parent.creation_time_100ns > child.creation_time_100ns:
                raise WindowsServiceError("target_process_tree_invalid")
            visited.add(parent_pid)
            ancestors.append(parent)
            if _path_key(parent.image_path) != expected_exe:
                external_boundary_seen = True
            child = parent
        raise WindowsServiceError("target_process_tree_invalid")

    @staticmethod
    def _validate_descendant_lineage(
        candidate: ProcessRecord,
        root: ProcessRecord,
        processes: dict[int, ProcessRecord],
    ) -> None:
        child = candidate
        visited: set[int] = set()
        for _ in range(64):
            if child.pid == root.pid:
                return
            if child.pid in visited:
                raise WindowsServiceError("target_process_tree_invalid")
            visited.add(child.pid)
            parent = processes.get(child.parent_pid)
            if parent is None or parent.pid in visited:
                raise WindowsServiceError("target_process_tree_invalid")
            if parent.creation_time_100ns > child.creation_time_100ns:
                raise WindowsServiceError("target_process_tree_invalid")
            child = parent
        raise WindowsServiceError("target_process_tree_invalid")

    def _node_service_descendants(
        self,
        root: ProcessRecord,
        processes: dict[int, ProcessRecord],
        expected_exe: str,
    ) -> list[ProcessRecord]:
        parent_map = {pid: process.parent_pid for pid, process in processes.items()}
        child_ids = _descendants(parent_map, root.pid)
        result: list[ProcessRecord] = []
        for pid in child_ids:
            candidate = processes.get(pid)
            if not candidate or _path_key(candidate.image_path) != expected_exe or not _is_node_service(candidate):
                continue
            self._validate_descendant_lineage(candidate, root, processes)
            result.append(candidate)
        return result

    @staticmethod
    def _checked_app_path(value: str) -> str:
        if not value or not os.path.isabs(value):
            raise WindowsServiceError("target_app_path_unverified")
        app_path = os.path.realpath(value)
        if not Path(app_path).is_dir() or not (Path(app_path) / "avsdk" / "AVSDKPlugin.dll").is_file():
            raise WindowsServiceError("target_app_path_unverified")
        return app_path

    def _query_qqnt_modules(self, pid: int) -> list[str]:
        command = (
            "$ErrorActionPreference='Stop'; $id=" + str(pid) + "; "
            "try { $p=Get-Process -Id $id -ErrorAction Stop; "
            "$moduleCollection=$p.Modules; "
            "if ($null -eq $moduleCollection) "
            "{ [ordered]@{status='unavailable'} | ConvertTo-Json -Compress } "
            "else { $hits=@($moduleCollection | Where-Object { [IO.Path]::GetFileName($_.FileName) -ieq 'QQNT.dll' } | "
            "ForEach-Object { $_.FileName }); "
            "if ($hits.Count -eq 0) { [ordered]@{status='missing';paths=@()} | ConvertTo-Json -Compress } "
            "else { [ordered]@{status='ok';paths=@($hits)} | ConvertTo-Json -Compress } } "
            "} catch [System.UnauthorizedAccessException] { '{\"status\":\"denied\"}' } "
            "catch { $err=$_.Exception; "
            "if ($err -is [System.ComponentModel.Win32Exception] -and "
            "($err.NativeErrorCode -eq 5 -or $err.NativeErrorCode -eq 299)) "
            "{ '{\"status\":\"denied\"}' } else { '{\"status\":\"unavailable\"}' } }"
        )
        try:
            raw = self._run_powershell(command)
        except WindowsServiceError:
            raise WindowsServiceError("target_module_metadata_unavailable") from None
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise WindowsServiceError("target_module_metadata_invalid") from None
        if not isinstance(value, dict):
            raise WindowsServiceError("target_module_metadata_invalid")
        status = value.get("status")
        if status == "denied":
            raise WindowsServiceError("target_module_access_denied")
        if status == "unavailable":
            raise WindowsServiceError("target_module_metadata_unavailable")
        if status == "missing":
            return []
        paths = value.get("paths")
        if status != "ok" or not isinstance(paths, list) or len(paths) > 32 or any(not isinstance(path, str) or not path for path in paths):
            raise WindowsServiceError("target_module_metadata_invalid")
        return paths

    def _query_snowluma_qq_modules(self, pid: int, *, timeout: float = 8.0) -> tuple[list[str], list[str]]:
        """Read QQNT and wrapper module paths from one exact QQ process."""
        command = (
            "$ErrorActionPreference='Stop'; $id=" + str(pid) + "; "
            "try { $p=Get-Process -Id $id -ErrorAction Stop; $moduleCollection=$p.Modules; "
            "if ($null -eq $moduleCollection) { [ordered]@{status='unavailable'} | ConvertTo-Json -Compress } "
            "else { $qqnt=@($moduleCollection | Where-Object { [IO.Path]::GetFileName($_.FileName) -ieq 'QQNT.dll' } | ForEach-Object { $_.FileName }); "
            "$wrapper=@($moduleCollection | Where-Object { [IO.Path]::GetFileName($_.FileName) -ieq 'wrapper.node' } | ForEach-Object { $_.FileName }); "
            "[ordered]@{status='ok';qqnt=@($qqnt);wrapper=@($wrapper)} | ConvertTo-Json -Compress -Depth 3 } "
            "} catch [System.UnauthorizedAccessException] { '{\"status\":\"denied\"}' } "
            "catch { $err=$_.Exception; if ($err -is [System.ComponentModel.Win32Exception] -and "
            "($err.NativeErrorCode -eq 5 -or $err.NativeErrorCode -eq 299)) "
            "{ '{\"status\":\"denied\"}' } else { '{\"status\":\"unavailable\"}' } }"
        )
        try:
            raw = self._run_powershell(command, timeout=timeout)
            value = json.loads(raw)
        except WindowsServiceError:
            raise WindowsServiceError("target_module_metadata_unavailable") from None
        except (json.JSONDecodeError, ValueError):
            raise WindowsServiceError("target_module_metadata_invalid") from None
        if type(value) is not dict:
            raise WindowsServiceError("target_module_metadata_invalid")
        status = value.get("status")
        if status == "denied":
            raise WindowsServiceError("target_module_access_denied")
        if status != "ok":
            raise WindowsServiceError("target_module_metadata_unavailable")

        def paths(field: str) -> list[str]:
            raw_paths = value.get(field)
            if not isinstance(raw_paths, list) or len(raw_paths) > 32:
                raise WindowsServiceError("target_module_metadata_invalid")
            if any(type(path) is not str or not path for path in raw_paths):
                raise WindowsServiceError("target_module_metadata_invalid")
            return raw_paths

        return paths("qqnt"), paths("wrapper")

    def _query_current_snowluma_qq_modules(
        self, pid: int, expected_identity: tuple[int, str],
    ) -> tuple[list[str], list[str]]:
        try:
            return self._query_snowluma_qq_modules(pid)
        except WindowsServiceError as error:
            if error.code != "target_module_metadata_unavailable":
                raise
        # Retry only an unavailable read, once, with a shorter fixed budget.
        # Fresh native identity checks surround the retry; the caller still
        # validates all returned module paths, hashes, pipes and process lineage.
        def identity_current() -> bool:
            current = self.process_identity(pid)
            return bool(current and current[0] == expected_identity[0]
                        and _path_key(current[1]) == _path_key(expected_identity[1]))

        if not identity_current():
            raise WindowsServiceError("target_process_changed")
        modules = self._query_snowluma_qq_modules(pid, timeout=1.0)
        if not identity_current():
            raise WindowsServiceError("target_process_changed")
        return modules

    def _enumerate_named_pipe_names(self) -> frozenset[str]:
        """Enumerate pipe names without opening any pipe or consuming its data."""
        if os.name != "nt":
            raise WindowsServiceError("target_process_unavailable")
        library_loader = getattr(ctypes, "WinDLL", None)
        if not callable(library_loader):
            raise WindowsServiceError("target_process_unavailable")
        try:
            kernel32 = library_loader("kernel32", use_last_error=True)
            find_first = kernel32.FindFirstFileW
            find_next = kernel32.FindNextFileW
            find_close = kernel32.FindClose
            find_first.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_Win32FindDataW)]
            find_first.restype = wintypes.HANDLE
            find_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Win32FindDataW)]
            find_next.restype = wintypes.BOOL
            find_close.argtypes = [wintypes.HANDLE]
            find_close.restype = wintypes.BOOL
        except (AttributeError, OSError, TypeError, ValueError):
            raise WindowsServiceError("target_process_unavailable") from None

        data = _Win32FindDataW()
        try:
            ctypes.set_last_error(0)
            handle = find_first(r"\\.\pipe\*", ctypes.byref(data))
        except (OSError, TypeError, ValueError, ctypes.ArgumentError):
            raise WindowsServiceError("target_process_unavailable") from None
        handle_value = handle if isinstance(handle, int) else getattr(handle, "value", None)
        invalid_handle = ctypes.c_void_p(-1).value
        if handle_value in (None, 0, invalid_handle):
            error = ctypes.get_last_error()
            if error == _ERROR_FILE_NOT_FOUND:
                return frozenset()
            raise WindowsServiceError("target_process_unavailable")

        names: set[str] = set()
        count = 0
        try:
            while True:
                name = data.cFileName
                if type(name) is not str or not name or "\x00" in name:
                    raise WindowsServiceError("target_process_unavailable")
                count += 1
                if count > _NAMED_PIPE_ENUMERATION_LIMIT or name in names:
                    raise WindowsServiceError("target_process_unavailable")
                names.add(name)

                try:
                    ctypes.set_last_error(0)
                    more = find_next(handle, ctypes.byref(data))
                except (OSError, TypeError, ValueError, ctypes.ArgumentError):
                    raise WindowsServiceError("target_process_unavailable") from None
                if more:
                    continue
                if ctypes.get_last_error() != _ERROR_NO_MORE_FILES:
                    raise WindowsServiceError("target_process_unavailable")
                break
        finally:
            try:
                ctypes.set_last_error(0)
                closed = find_close(handle)
            except (OSError, TypeError, ValueError, ctypes.ArgumentError):
                raise WindowsServiceError("target_process_unavailable") from None
            if not closed:
                raise WindowsServiceError("target_process_unavailable")
        return frozenset(names)

    def _named_pipe_snapshot(self, pid: int) -> frozenset[str]:
        if type(pid) is not int or pid <= 0:
            raise WindowsServiceError("target_process_unavailable")
        names = self._enumerate_named_pipe_names()
        return frozenset(
            endpoint
            for endpoint in ("control", "recv")
            if f"mojo.{pid}.{endpoint}" in names
        )

    def _named_pipe_exists(self, pid: int, endpoint: str) -> bool:
        if type(pid) is not int or pid <= 0 or endpoint not in ("control", "recv"):
            return False
        return endpoint in self._named_pipe_snapshot(pid)

    def _snowluma_listener_state(self, record: ProcessRecord) -> tuple[str, tuple[str, ...]]:
        """Read listener cwd/argv between two exact PID+image+FILETIME fences."""
        before = self.process_identity(record.pid)
        if (
            before is None
            or before[0] != record.creation_time_100ns
            or _path_key(before[1]) != _path_key(record.image_path)
        ):
            raise WindowsServiceError("target_process_changed")
        try:
            process = psutil.Process(record.pid)
            cwd = process.cwd()
            argv = process.cmdline()
        except (psutil.Error, OSError, AttributeError, TypeError, ValueError):
            raise WindowsServiceError("target_process_unavailable") from None
        after = self.process_identity(record.pid)
        if (
            after is None
            or after[0] != before[0]
            or _path_key(after[1]) != _path_key(before[1])
        ):
            raise WindowsServiceError("target_process_changed")
        if (
            type(cwd) is not str
            or not os.path.isabs(cwd)
            or not isinstance(argv, list)
            or len(argv) != 2
            or any(type(argument) is not str or not argument or "\x00" in argument for argument in argv)
        ):
            raise WindowsServiceError("target_process_mismatch")
        return cwd, tuple(argv)

    def _verify_snowluma_listener(
        self,
        record: ProcessRecord,
        runtime_path: str,
    ) -> tuple[str, tuple[str, ...]]:
        if record.creation_time_100ns <= 0 or not os.path.isabs(record.image_path):
            raise WindowsServiceError("target_process_mismatch")
        if ntpath.basename(record.image_path).casefold() != "node.exe":
            raise WindowsServiceError("target_process_mismatch")
        runtime = Path(runtime_path)
        entrypoint = runtime / "index.mjs"
        bundled_node = runtime / "node.exe"
        if not entrypoint.is_file():
            raise WindowsServiceError("target_process_mismatch")
        if bundled_node.is_file() and _path_key(record.image_path) != _path_key(str(bundled_node)):
            raise WindowsServiceError("target_process_mismatch")

        cwd, argv = self._snowluma_listener_state(record)
        if _path_key(cwd) != _path_key(runtime_path):
            raise WindowsServiceError("target_process_mismatch")
        if (
            ntpath.basename(argv[0]).casefold() not in ("node", "node.exe")
            or argv[1].replace("\\", "/") != "./index.mjs"
        ):
            raise WindowsServiceError("target_process_mismatch")
        return cwd, argv

    def _snowluma_wrapper_digest(self, path: Path) -> str:
        def identity_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
            try:
                birth_ns = getattr(metadata, "st_birthtime_ns", None)
                if type(birth_ns) is not int:
                    birth = getattr(metadata, "st_birthtime", None)
                    if type(birth) not in (int, float):
                        raise ValueError
                    birth_ns = int(birth * 1_000_000_000)
                identity = (
                    int(metadata.st_dev),
                    int(metadata.st_ino),
                    int(metadata.st_size),
                    int(metadata.st_mtime_ns),
                    birth_ns,
                )
            except (AttributeError, OverflowError, TypeError, ValueError):
                raise OSError from None
            if identity[0] < 0 or identity[1] <= 0 or identity[2] <= 0 or identity[3] < 0 or identity[4] < 0:
                raise OSError
            return identity

        def path_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
            return identity_fingerprint(metadata) + (int(getattr(metadata, "st_ctime_ns", 0)),)

        try:
            metadata = path.lstat()
            if (
                path.is_symlink()
                or getattr(metadata, "st_file_attributes", 0) & 0x400
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > 256 * 1024 * 1024
            ):
                raise OSError
            path_key = _path_key(str(path))
            current_identity = identity_fingerprint(metadata)
            current_fingerprint = path_fingerprint(metadata)
            cached = self._snowluma_wrapper_digest_cache
            if cached is not None and cached[:2] == (path_key, current_fingerprint):
                return cached[2]
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if identity_fingerprint(opened) != current_identity:
                    raise OSError
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                finished = os.fstat(stream.fileno())
            final_metadata = path.lstat()
            if (
                identity_fingerprint(finished) != current_identity
                or path_fingerprint(final_metadata) != current_fingerprint
            ):
                raise OSError
            value = digest.hexdigest().upper()
            self._snowluma_wrapper_digest_cache = (path_key, current_fingerprint, value)
            return value
        except OSError:
            raise WindowsServiceError("target_module_missing") from None

    def _bind_snowluma_adapter(
        self,
        config: dict[str, Any],
        manifest: dict[str, Any],
    ) -> TargetBinding:
        try:
            from service.snowluma import SnowLumaBindingError, load_snowluma_authority
        except ImportError:  # pragma: no cover - direct-module compatibility
            from snowluma import SnowLumaBindingError, load_snowluma_authority  # type: ignore

        account_id = config["account_id"]
        onebot_url = config["onebot"]["url"]
        expected_qq_image = config["paths"]["qq_exe"]
        try:
            port = _onebot_port(onebot_url)
        except (TypeError, ValueError):
            raise WindowsServiceError("adapter_endpoint_mismatch") from None

        # Capture the listener and process incarnations before the authenticated
        # account lookup. The account API returns a PID, so its result must be
        # matched against this earlier snapshot instead of choosing an OS
        # process snapshot that could already contain a reused PID.
        initial_owners = self.listener_owners(port)
        if len(initial_owners) != 1:
            raise WindowsServiceError("target_ambiguous" if len(initial_owners) > 1 else "adapter_listener_unavailable")
        initial_processes = self.process_table()
        initial_adapter = initial_processes.get(manifest["adapterPid"])
        initial_listener = initial_processes.get(initial_owners[0])
        if initial_adapter is None:
            raise WindowsServiceError("adapter_process_unavailable")
        if (
            initial_adapter.creation_time_100ns != int(manifest["adapterCreationTime100ns"])
            or ntpath.basename(initial_adapter.image_path).casefold() not in ("python.exe", "pythonw.exe")
        ):
            raise WindowsServiceError("adapter_process_mismatch")
        if initial_listener is None:
            raise WindowsServiceError("target_process_unavailable")

        try:
            authority = load_snowluma_authority(
                self._snowluma_root,
                account_id,
                onebot_url,
            )
        except SnowLumaBindingError as exc:
            raise WindowsServiceError(exc.code, diagnostic_stage=exc._diagnostic_stage) from None
        except Exception:
            raise WindowsServiceError("adapter_auth_unavailable") from None
        if authority.account_id != account_id or authority.process.account_id != account_id:
            raise WindowsServiceError("adapter_account_mismatch")

        owners = self.listener_owners(port)
        if len(owners) != 1:
            raise WindowsServiceError("target_ambiguous" if len(owners) > 1 else "adapter_listener_unavailable")
        if owners != initial_owners:
            raise WindowsServiceError("target_process_changed")
        processes = self.process_table()
        adapter = processes.get(manifest["adapterPid"])
        if adapter is None:
            raise WindowsServiceError("adapter_process_unavailable")
        if (
            adapter.creation_time_100ns != int(manifest["adapterCreationTime100ns"])
            or ntpath.basename(adapter.image_path).casefold() not in ("python.exe", "pythonw.exe")
        ):
            raise WindowsServiceError("adapter_process_mismatch")
        if (
            adapter.creation_time_100ns != initial_adapter.creation_time_100ns
            or _path_key(adapter.image_path) != _path_key(initial_adapter.image_path)
        ):
            raise WindowsServiceError("target_process_changed")

        listener = processes.get(owners[0])
        if listener is None:
            raise WindowsServiceError("target_process_unavailable")
        if (
            listener.creation_time_100ns != initial_listener.creation_time_100ns
            or _path_key(listener.image_path) != _path_key(initial_listener.image_path)
        ):
            raise WindowsServiceError("target_process_changed")
        listener_cwd, listener_argv = self._verify_snowluma_listener(listener, authority.runtime_path)

        # The API PID is accepted only if that exact PID+FILETIME+image was
        # already present before authentication and remains unchanged after it.
        initial_kernel = initial_processes.get(authority.process.pid)
        if initial_kernel is None:
            raise WindowsServiceError("target_process_changed")
        kernel = processes.get(authority.process.pid)
        if kernel is None:
            raise WindowsServiceError("target_process_changed")
        if (
            kernel.creation_time_100ns != initial_kernel.creation_time_100ns
            or _path_key(kernel.image_path) != _path_key(initial_kernel.image_path)
        ):
            raise WindowsServiceError("target_process_changed")
        if (
            kernel.creation_time_100ns <= 0
            or _path_key(kernel.image_path) != _path_key(expected_qq_image)
            or (
                authority.process.image_path
                and _path_key(authority.process.image_path) != _path_key(expected_qq_image)
            )
        ):
            raise WindowsServiceError("target_process_mismatch")
        if self._named_pipe_snapshot(kernel.pid) != frozenset(("control", "recv")):
            raise WindowsServiceError("target_process_unavailable")

        identity_before = self.process_identity(kernel.pid)
        if (
            identity_before is None
            or identity_before[0] != kernel.creation_time_100ns
            or _path_key(identity_before[1]) != _path_key(kernel.image_path)
            or identity_before[0] != initial_kernel.creation_time_100ns
            or _path_key(identity_before[1]) != _path_key(initial_kernel.image_path)
        ):
            raise WindowsServiceError("target_process_changed")
        qqnt_paths, wrapper_paths = self._query_current_snowluma_qq_modules(kernel.pid, identity_before)
        if len(qqnt_paths) != 1 or len(wrapper_paths) != 1:
            raise WindowsServiceError("target_module_ambiguous" if len(qqnt_paths) > 1 or len(wrapper_paths) > 1 else "target_module_missing")

        expected_install = os.path.dirname(os.path.realpath(expected_qq_image))
        qqnt_module, app_path = self._qqnt_app_path(qqnt_paths[0], expected_install)
        if Path(qqnt_module).parent.name != _SNOWLUMA_PINNED_QQ_BUILD:
            raise WindowsServiceError("target_module_mismatch")
        expected_wrapper = Path(app_path) / "wrapper.node"
        wrapper_module = str(Path(os.path.realpath(wrapper_paths[0])))
        if (
            _path_key(wrapper_module) != _path_key(str(expected_wrapper))
            or not expected_wrapper.is_file()
            or self._snowluma_wrapper_digest(expected_wrapper) != _SNOWLUMA_PINNED_WRAPPER_SHA256
        ):
            raise WindowsServiceError("target_module_mismatch")

        identity_after = self.process_identity(kernel.pid)
        if (
            identity_after is None
            or identity_after[0] != identity_before[0]
            or _path_key(identity_after[1]) != _path_key(identity_before[1])
            or self._named_pipe_snapshot(kernel.pid) != frozenset(("control", "recv"))
        ):
            raise WindowsServiceError("target_process_changed")
        final_listener_state = self._verify_snowluma_listener(listener, authority.runtime_path)
        if final_listener_state != (listener_cwd, listener_argv):
            raise WindowsServiceError("target_process_changed")

        self._verify_binding_snapshot(port, listener.pid, [adapter, listener, kernel])
        target = TargetBinding(
            adapter.pid,
            adapter.creation_time_100ns,
            kernel.pid,
            kernel.creation_time_100ns,
            app_path,
        )
        proof = _SnowLumaBindingProof(
            account_id,
            onebot_url,
            (authority.endpoint_host, authority.endpoint_port, authority.endpoint_path),
            _path_key(authority.runtime_path),
            listener.pid,
            listener.creation_time_100ns,
            _path_key(listener.image_path),
            _path_key(listener_cwd),
            listener_argv,
            kernel.pid,
            kernel.creation_time_100ns,
            _path_key(kernel.image_path),
            _path_key(qqnt_module),
            _path_key(wrapper_module),
        )
        proof_key = (
            manifest["adapterPid"],
            int(manifest["adapterCreationTime100ns"]),
            str(manifest["connectionGeneration"]),
        )
        pinned = self._snowluma_pinned_binding
        if pinned is not None and pinned[0] == proof_key and pinned[1] != proof:
            raise WindowsServiceError("target_binding_changed")
        if pinned is None or pinned[0] != proof_key:
            self._snowluma_pinned_binding = (proof_key, proof)
        return target

    @staticmethod
    def _qqnt_app_path(module_path: str, expected_install: str) -> tuple[str, str]:
        if not module_path or not os.path.isabs(module_path):
            raise WindowsServiceError("target_module_outside_install")
        canonical = Path(os.path.realpath(module_path))
        install_root = canonical.parent.parent.parent
        if (
            canonical.name.casefold() != "qqnt.dll"
            or canonical.parent.parent.name.casefold() != "versions"
            or not canonical.parent.name
            or _path_key(str(install_root)) != _path_key(expected_install)
        ):
            raise WindowsServiceError("target_module_outside_install")
        if not canonical.is_file():
            raise WindowsServiceError("target_module_missing")
        app_path = canonical.parent / "resources" / "app"
        if not app_path.is_dir() or not (app_path / "avsdk" / "AVSDKPlugin.dll").is_file():
            raise WindowsServiceError("target_app_path_unverified")
        return str(canonical), str(Path(os.path.realpath(app_path)))

    def _verify_binding_snapshot(
        self,
        port: int,
        owner_pid: int,
        records: list[ProcessRecord],
    ) -> None:
        unique: dict[int, ProcessRecord] = {record.pid: record for record in records}
        for record in unique.values():
            identity = self.process_identity(record.pid)
            if (
                identity is None
                or identity[0] != record.creation_time_100ns
                or _path_key(identity[1]) != _path_key(record.image_path)
            ):
                raise WindowsServiceError("target_process_changed")
        current_owners = self.listener_owners(port)
        if current_owners != [owner_pid]:
            if len(current_owners) > 1:
                raise WindowsServiceError("target_ambiguous")
            raise WindowsServiceError("adapter_listener_mismatch")

    def discover_onebot_connection(self, config: dict[str, Any]) -> tuple[str, str | None]:
        if config["adapter"] == "snowluma":
            from .snowluma import configured_onebot_connection, SnowLumaBindingError

            try:
                return configured_onebot_connection(self._snowluma_root)
            except SnowLumaBindingError as exc:
                raise WindowsServiceError(exc.code) from None
        from .napcat import configured_onebot_connections

        try:
            choices = configured_onebot_connections(config)
        except ValueError:
            raise WindowsServiceError("adapter_endpoint_mismatch") from None
        for url, token in choices:
            if len(self.listener_owners(_onebot_port(url))) == 1:
                return url, token
        raise WindowsServiceError("adapter_listener_unavailable")

    def bind_adapter(self, config: dict[str, Any], manifest: dict[str, Any]) -> TargetBinding:
        if config.get("adapter") == "snowluma":
            return self._bind_snowluma_adapter(config, manifest)
        owners = self.listener_owners(_onebot_port(config["onebot"]["url"]))
        if len(owners) != 1:
            raise WindowsServiceError("target_ambiguous" if len(owners) > 1 else "adapter_listener_unavailable")
        port = _onebot_port(config["onebot"]["url"])
        processes = self.process_table()
        adapter = processes.get(manifest["adapterPid"])
        if adapter is None:
            raise WindowsServiceError("adapter_process_unavailable")
        if (adapter.creation_time_100ns != int(manifest["adapterCreationTime100ns"]) or
                ntpath.basename(adapter.image_path).casefold() not in ("python.exe", "pythonw.exe")):
            raise WindowsServiceError("adapter_process_mismatch")

        owner = processes.get(owners[0])
        if owner is None:
            raise WindowsServiceError("target_process_unavailable")
        expected_exe = _path_key(config["paths"]["qq_exe"])
        if _path_key(owner.image_path) != expected_exe:
            raise WindowsServiceError("target_process_mismatch")

        ancestors = self._validated_ancestors(owner, processes, expected_exe)
        same_image_ancestors = [record for record in ancestors if _path_key(record.image_path) == expected_exe]
        roots = [owner, *same_image_ancestors]

        # Only hydrate missing command lines inside the validated QQ image
        # family rooted at the highest same-image ancestor. This avoids
        # querying unrelated QQ instances such as a user's personal client.
        family_root = roots[-1]
        parent_map = {pid: record.parent_pid for pid, record in processes.items()}
        family_pids = {family_root.pid, *_descendants(parent_map, family_root.pid)}
        for pid in sorted(family_pids):
            candidate = processes.get(pid)
            if candidate is None:
                continue
            if candidate.creation_time_100ns <= 0 or not candidate.image_path:
                raise WindowsServiceError("target_ambiguous")
            if _path_key(candidate.image_path) != expected_exe or candidate.command_line:
                continue
            self._validate_descendant_lineage(candidate, family_root, processes)
            command_line = self._public_command_line_for_record(candidate, processes)
            processes[pid] = ProcessRecord(
                candidate.pid,
                candidate.parent_pid,
                candidate.image_path,
                command_line,
                candidate.creation_time_100ns,
            )
        roots = [processes[record.pid] for record in roots]

        app_roots: list[tuple[int, ProcessRecord, str]] = []
        for distance, candidate in enumerate(roots):
            present, values, invalid = _app_path_arguments(candidate.command_line)
            if invalid:
                raise WindowsServiceError("target_app_path_unverified")
            if present:
                for value in values:
                    app_roots.append((distance, candidate, self._checked_app_path(value)))
        distinct_app_paths = {_path_key(app_path) for _, _, app_path in app_roots}
        if app_roots and len(distinct_app_paths) != 1:
            raise WindowsServiceError("target_ambiguous")

        if app_roots:
            # Preserve the explicit --app-path selection and validation path.
            _distance, root, app_path = max(app_roots, key=lambda item: item[0])
            utility = self._node_service_descendants(root, processes, expected_exe)
            matching_utility: list[ProcessRecord] = []
            for candidate in utility:
                present, values, invalid = _app_path_arguments(candidate.command_line)
                if invalid:
                    raise WindowsServiceError("target_app_path_unverified")
                if not present:
                    continue
                candidate_paths = [self._checked_app_path(value) for value in values]
                if any(_path_key(path) != _path_key(app_path) for path in candidate_paths):
                    raise WindowsServiceError("target_ambiguous")
                matching_utility.append(candidate)
            if len(matching_utility) != 1:
                raise WindowsServiceError("target_ambiguous" if len(matching_utility) > 1 else "node_service_unavailable")
            kernel = matching_utility[0]
        else:
            # With no explicit path in the listener's QQ lineage, bind to the
            # unique NodeService first; module metadata must not disambiguate
            # multiple candidates from the same QQ tree.
            root = roots[-1]
            utility = self._node_service_descendants(root, processes, expected_exe)
            if len(utility) != 1:
                raise WindowsServiceError("target_ambiguous" if len(utility) > 1 else "node_service_unavailable")
            kernel = utility[0]

            pinned_qq = {record.pid: record for record in (root, owner, kernel)}
            module_paths: list[str] = []
            app_paths: list[str] = []
            expected_install = os.path.dirname(os.path.realpath(config["paths"]["qq_exe"]))
            for pid in pinned_qq:
                paths = self._query_qqnt_modules(pid)
                if not paths:
                    raise WindowsServiceError("target_module_missing")
                if len(paths) != 1:
                    raise WindowsServiceError("target_module_ambiguous")
                module_path, derived_app_path = self._qqnt_app_path(paths[0], expected_install)
                module_paths.append(module_path)
                app_paths.append(derived_app_path)
            if len({_path_key(path) for path in module_paths}) != 1 or len({_path_key(path) for path in app_paths}) != 1:
                raise WindowsServiceError("target_module_mismatch")
            app_path = app_paths[0]

            for candidate in (root, owner, kernel):
                present, values, invalid = _app_path_arguments(candidate.command_line)
                if invalid:
                    raise WindowsServiceError("target_app_path_unverified")
                if present:
                    candidate_paths = [self._checked_app_path(value) for value in values]
                    if any(_path_key(path) != _path_key(app_path) for path in candidate_paths):
                        raise WindowsServiceError("target_ambiguous")

        self._verify_binding_snapshot(port, owner.pid, [adapter, root, owner, kernel])
        return TargetBinding(adapter.pid, adapter.creation_time_100ns, kernel.pid, kernel.creation_time_100ns, app_path)

    def revalidate_binding(self, config: dict[str, Any], manifest: dict[str, Any], target: TargetBinding) -> bool:
        try:
            return self.revalidate_binding_with_error(config, manifest, target)
        except WindowsServiceError:
            return False

    def revalidate_binding_with_error(self, config: dict[str, Any], manifest: dict[str, Any], target: TargetBinding) -> bool:
        """Keep typed provenance failures available to service supervision."""
        current = self.bind_adapter(config, manifest)
        return current == target

    def _host_launch_from_receipt(
        self,
        value: dict[str, Any],
        config: dict[str, Any],
        generation: int,
        runtime: Path,
    ) -> HostLaunch:
        if (value.get("ok") is not True or value.get("dryRun") is not False or
                value.get("processLaunched") is not True or
                type(value.get("hostGeneration")) is not int or value["hostGeneration"] != generation or
                type(value.get("listenPort")) is not int or value["listenPort"] != config["ports"]["host"] or
                type(value.get("outputPort")) is not int or value["outputPort"] != config["ports"]["callback"]):
            raise WindowsServiceError("host_launch_failed")

        path_values = (value.get("runDirectory"), value.get("profilePath"), value.get("launchPlanPath"))
        if any(type(item) is not str or not item or len(item) > 32768 or "\x00" in item for item in path_values):
            raise WindowsServiceError("host_launch_failed")
        try:
            run_directory, profile, plan = (Path(item).resolve(strict=False) for item in path_values)
        except (OSError, RuntimeError, ValueError):
            raise WindowsServiceError("host_launch_failed") from None
        expected_run = (runtime / f"host-{generation}").resolve(strict=False)
        if (_path_key(str(run_directory)) != _path_key(str(expected_run)) or
                _path_key(str(profile)) != _path_key(str(run_directory / "profile")) or
                _path_key(str(plan)) != _path_key(str(run_directory / "launch-plan.json"))):
            raise WindowsServiceError("host_launch_failed")

        launcher_pid = value.get("launcherPid")
        if type(launcher_pid) is not int or not 1 <= launcher_pid <= 0x7FFFFFFF:
            raise WindowsServiceError("host_launch_failed")
        expected_launcher = str(Path(config["paths"]["loader_root"]) / "NapCatWinBootMain.exe")
        identity = self.process_identity(launcher_pid)
        if identity is None:
            raise WindowsServiceError("host_launcher_identity_unavailable")
        if _path_key(identity[1]) != _path_key(expected_launcher):
            raise WindowsServiceError("host_launcher_identity_mismatch")
        report = plan.parent / "host"
        return HostLaunch(
            launcher_pid, str(run_directory), str(profile), str(report),
            host_generation=generation,
            launcher_creation_time_100ns=identity[0],
            launcher_image_path=identity[1],
        )

    def _cleanup_rejected_host_launch(
        self,
        receipt_path: Path,
        config: dict[str, Any],
        generation: int,
        runtime: Path,
    ) -> None:
        """Best-effort cleanup when a failed launch left a complete pinned receipt."""
        try:
            launch = self._host_launch_from_receipt(
                _read_host_launch_receipt(receipt_path), config, generation, runtime,
            )
            records = self.owned_host_processes(launch, config)
            self.terminate_owned(records)
        except Exception:
            # Incomplete or mismatched receipts do not authorize process selection.
            return

    def launch_host(
        self,
        config: dict[str, Any],
        generation: int,
        target: TargetBinding,
        guard_config: str | None,
        *,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> HostLaunch:
        project = Path(__file__).resolve().parents[1]
        launcher = project / "scripts" / "launch-host.ps1"
        runtime = Path(config["runtime_dir"]) / "hosts"
        runtime.mkdir(parents=True, exist_ok=True)
        command = [
            self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(launcher),
            "-QQExePath", config["paths"]["qq_exe"],
            "-AppPath", target.app_path,
            "-NapCatShellRoot", config["paths"]["loader_root"],
            "-RuntimeDirectory", str(runtime),
            "-ListenPort", str(config["ports"]["host"]),
            "-OutputPort", str(config["ports"]["callback"]),
            "-HostGeneration", str(generation),
        ]
        if guard_config:
            command.extend(("-AudioRouteGuardConfig", guard_config))
        try:
            receipt_fd, receipt_name = tempfile.mkstemp(
                prefix=f"host-launch-{generation}-", suffix=".receipt", dir=runtime,
            )
        except OSError:
            raise WindowsServiceError("host_launch_failed") from None
        receipt_path = Path(receipt_name)
        try:
            try:
                with os.fdopen(receipt_fd, "wb") as receipt_stream:
                    returncode = _run_process_with_file_stdout(
                        command,
                        receipt_stream,
                        timeout_seconds=15.0,
                        should_stop=should_stop,
                        on_progress=on_progress,
                    )
            except Exception:
                self._cleanup_rejected_host_launch(receipt_path, config, generation, runtime)
                raise
            if returncode != 0:
                self._cleanup_rejected_host_launch(receipt_path, config, generation, runtime)
                raise WindowsServiceError("host_launch_failed")
            try:
                value = _read_host_launch_receipt(receipt_path)
                return self._host_launch_from_receipt(value, config, generation, runtime)
            except WindowsServiceError:
                self._cleanup_rejected_host_launch(receipt_path, config, generation, runtime)
                raise
        except WindowsServiceError:
            raise
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise WindowsServiceError("host_launch_failed") from None
        finally:
            try:
                receipt_path.unlink(missing_ok=True)
            except OSError:
                pass

    def wait_host_ready(
        self,
        launch: HostLaunch,
        config: dict[str, Any],
        *,
        timeout_seconds: float = 20.0,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> tuple[list[dict[str, Any]], int, str, str]:
        """Wait for the launch-bound Host PID and private bootstrap sidecar.

        Runtime readiness is established separately by the authenticated
        HostClient health response after its callback listener is active.
        """
        sidecar = launch.report_path + ".host.config.json"
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                raise WindowsServiceError("service_stop_requested")
            if on_progress is not None:
                on_progress()
            records = self.owned_host_processes(launch, config)
            if records and Path(sidecar).is_file():
                roots = [record for record in records if record["role"] == "host"]
                if len(roots) == 1:
                    return records, roots[0]["pid"], sidecar, roots[0]["creationTime100ns"]
                if len(roots) > 1:
                    raise WindowsServiceError("host_sidecar_target_ambiguous")
            time.sleep(0.1)
        raise WindowsServiceError("host_bootstrap_timeout")

    def owned_host_processes(self, launch: HostLaunch, config: dict[str, Any]) -> list[dict[str, Any]]:
        processes = self.process_table()
        launcher = processes.get(launch.launcher_pid)
        expected_launcher = _path_key(str(Path(config["paths"]["loader_root"]) / "NapCatWinBootMain.exe"))
        pinned_launcher = _path_key(launch.launcher_image_path) if launch.launcher_image_path else ""
        if (launch.launcher_creation_time_100ns <= 0 or not pinned_launcher or
                pinned_launcher != expected_launcher):
            raise WindowsServiceError("host_launcher_unpinned")
        if launcher is None:
            # Without the pinned launcher in the current process snapshot, a
            # numeric parent PID cannot prove this invocation's lineage.
            raise WindowsServiceError("host_launcher_lineage_unavailable")
        if (launcher.creation_time_100ns != launch.launcher_creation_time_100ns or
                _path_key(launcher.image_path) != pinned_launcher):
            raise WindowsServiceError("host_launcher_identity_mismatch")

        sidecar_path = Path(launch.report_path + ".host.config.json")
        metadata = self._read_host_bootstrap_metadata(sidecar_path, launch, config)
        result: list[dict[str, Any]] = [{
            "role": "launcher",
            "pid": launch.launcher_pid,
            "creationTime100ns": str(launch.launcher_creation_time_100ns),
            "imagePath": launch.launcher_image_path,
            "profilePath": launch.profile_path,
        }]
        # Before early-main publishes metadata, retain only the launcher's
        # exact pinned identity. Never infer a root from profile flags alone.
        if metadata is None:
            return result

        profile = _path_key(launch.profile_path)
        expected_exe = _path_key(config["paths"]["qq_exe"])
        target_pid = metadata["targetPid"]
        root = processes.get(target_pid)
        if root is None:
            return result
        if _path_key(root.image_path) != expected_exe:
            raise WindowsServiceError("host_root_image_mismatch")
        process_type = _option(root.command_line, "type")
        if process_type is not None and process_type.casefold() != "browser":
            # The sidecar target is the Host's main Electron process. A
            # renderer/utility PID may carry the same profile flag and remain
            # a descendant, but it cannot be promoted to the ownership root.
            raise WindowsServiceError("host_sidecar_binding_mismatch")
        user_data = _option(root.command_line, "user-data-dir")
        if user_data and _path_key(user_data) != profile:
            raise WindowsServiceError("host_profile_conflict")
        if root.creation_time_100ns <= launch.launcher_creation_time_100ns:
            raise WindowsServiceError("host_root_creation_order_invalid")

        parent_map = {pid: process.parent_pid for pid, process in processes.items()}
        if not self._is_pinned_descendant(root, launch, processes):
            raise WindowsServiceError("host_root_not_launcher_descendant")
        descendants = _descendants(parent_map, root.pid)
        host_members = [root]
        for pid in descendants:
            child = processes.get(pid)
            if child and _path_key(child.image_path) == expected_exe:
                user_data = _option(child.command_line, "user-data-dir")
                if user_data and _path_key(user_data) != profile:
                    raise WindowsServiceError("host_profile_conflict")
                host_members.append(child)
        for process in host_members:
            depth = self._verified_host_depth(process, root.pid, launch, processes)
            parent = processes.get(process.parent_pid)
            if parent is None:
                raise WindowsServiceError("host_process_tree_invalid")
            result.append({
                "role": "host" if process.pid == root.pid else "host_child",
                "pid": process.pid,
                "creationTime100ns": str(process.creation_time_100ns),
                "imagePath": process.image_path,
                "profilePath": launch.profile_path,
                "depth": depth,
                "parentPid": process.parent_pid,
                "parentCreationTime100ns": str(parent.creation_time_100ns),
                "processType": _host_process_type(process.command_line),
            })
        return result

    def _read_host_bootstrap_metadata(
        self,
        sidecar_path: Path,
        launch: HostLaunch,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            resolved_sidecar = sidecar_path.resolve(strict=False)
            run_directory = Path(launch.run_directory).resolve(strict=False)
            if not _path_is_under(str(resolved_sidecar), str(run_directory)):
                raise WindowsServiceError("host_sidecar_path_invalid")
            try:
                info = sidecar_path.lstat()
            except FileNotFoundError:
                return None
            if (sidecar_path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400 or
                    not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024):
                raise WindowsServiceError("host_sidecar_invalid")
            raw = sidecar_path.read_text(encoding="utf-8")
            value = json.loads(raw)
        except WindowsServiceError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise WindowsServiceError("host_sidecar_invalid") from None
        if not isinstance(value, dict):
            raise WindowsServiceError("host_sidecar_invalid")
        target_pid = value.get("targetPid")
        generation = value.get("hostGeneration")
        listen_port = value.get("listenPort")
        output_port = value.get("outputPort")
        profile_path = value.get("profilePath")
        if (type(target_pid) is not int or target_pid <= 0 or target_pid > 0x7FFFFFFF or
                type(generation) is not int or generation != launch.host_generation or
                type(listen_port) is not int or listen_port != config["ports"]["host"] or
                type(output_port) is not int or output_port != config["ports"]["callback"] or
                not isinstance(profile_path, str) or _path_key(profile_path) != _path_key(launch.profile_path)):
            raise WindowsServiceError("host_sidecar_binding_mismatch")
        return {"targetPid": target_pid}

    @staticmethod
    def _is_pinned_descendant(
        process: ProcessRecord,
        launch: HostLaunch,
        processes: dict[int, ProcessRecord],
    ) -> bool:
        child = process
        visited = {process.pid}
        for _ in range(64):
            if child.creation_time_100ns <= launch.launcher_creation_time_100ns:
                return False
            if child.parent_pid == launch.launcher_pid:
                return True
            if child.parent_pid in visited:
                raise WindowsServiceError("host_process_tree_invalid")
            visited.add(child.parent_pid)
            parent = processes.get(child.parent_pid)
            if parent is None:
                return False
            if child.creation_time_100ns <= parent.creation_time_100ns:
                raise WindowsServiceError("host_process_tree_invalid")
            child = parent
        raise WindowsServiceError("host_process_tree_invalid")

    @staticmethod
    def _verified_host_depth(
        process: ProcessRecord,
        root_pid: int,
        launch: HostLaunch,
        processes: dict[int, ProcessRecord],
    ) -> int:
        """Return distance from the pinned launcher after validating every ancestry edge."""
        child = process
        visited: set[int] = set()
        saw_root = False
        for depth in range(64):
            if child.pid in visited:
                raise WindowsServiceError("host_process_tree_invalid")
            visited.add(child.pid)
            if child.pid == root_pid:
                saw_root = True
            if child.pid == launch.launcher_pid:
                if (not saw_root or child.creation_time_100ns != launch.launcher_creation_time_100ns):
                    raise WindowsServiceError("host_process_tree_invalid")
                return depth
            parent = processes.get(child.parent_pid)
            if parent is None or child.creation_time_100ns <= parent.creation_time_100ns:
                raise WindowsServiceError("host_process_tree_invalid")
            child = parent
        raise WindowsServiceError("host_process_tree_invalid")

    def current_process_identity(self) -> tuple[int, int]:
        identity = self.process_identity(os.getpid())
        if identity is None:
            raise WindowsServiceError("supervisor_identity_unavailable")
        return os.getpid(), identity[0]

    def terminate_owned(self, records: list[dict[str, Any]]) -> bool:
        """Stop exact recorded Host instances, descendants first, then the pinned launcher."""
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        process_records = [record for record in records if record.get("role") in ("host", "host_child")]
        if any(record.get("role") not in ("launcher", "host", "host_child") for record in records):
            return False
        if any(
            type(record.get("pid")) is not int or record["pid"] <= 0 or
            type(record.get("creationTime100ns")) is not str or not record["creationTime100ns"].isdecimal() or
            not isinstance(record.get("imagePath"), str) or not record["imagePath"] or
            not isinstance(record.get("profilePath"), str) or not record["profilePath"]
            for record in records
        ):
            return False

        # All current records carry validated depth. Old in-memory records do
        # not, so keep their safe role order without inventing ancestry data.
        has_depth = bool(process_records) and all(
            type(record.get("depth")) is int and record["depth"] >= 1
            for record in process_records
        )
        if has_depth:
            process_type_order = {"ppapi": 0, "ppapi-broker": 1, "renderer": 2, "other": 3}
            ordered = sorted(
                records,
                key=lambda record: (
                    0 if record["role"] == "host_child" else 1 if record["role"] == "host" else 2,
                    -record["depth"] if record["role"] in ("host", "host_child") else 0,
                    process_type_order.get(record.get("processType"), 3),
                ),
            )
        else:
            role_order = {"host_child": 0, "host": 1, "launcher": 2}
            process_type_order = {"ppapi": 0, "ppapi-broker": 1, "renderer": 2, "other": 3}
            ordered = sorted(
                records,
                key=lambda record: (
                    role_order[record["role"]],
                    -(record["depth"] if type(record.get("depth")) is int and record["depth"] >= 1 else 0),
                    process_type_order.get(record.get("processType"), 3),
                ),
            )

        try:
            kernel32 = self._kernel32()
        except _PROCESS_QUERY_ERRORS:
            return False
        deadline = time.monotonic() + 5.0
        for record in ordered:
            if time.monotonic() >= deadline:
                return False
            pid = record.get("pid")
            expected_time = record.get("creationTime100ns")
            image = record.get("imagePath")
            try:
                current = self.process_identity(pid)
            except _PROCESS_QUERY_ERRORS:
                return False
            if current is None:
                try:
                    still_current = self.process_is_current(pid, int(expected_time))
                except _PROCESS_QUERY_ERRORS:
                    return False
                if still_current is False:
                    continue
                # A live but unqueryable process is not equivalent to an
                # absent one. Preserve all ancestors that could trigger it.
                return False
            if current[0] != int(expected_time):
                # The recorded incarnation is gone; never touch a PID reuse.
                continue
            if _path_key(current[1]) != _path_key(image):
                return False
            try:
                handle = kernel32.OpenProcess(
                    0x0001 | 0x00100000 | 0x00001000, 0, pid,
                )  # TERMINATE | SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            except _PROCESS_QUERY_ERRORS:
                return False
            if not handle:
                try:
                    still_current = self.process_is_current(pid, int(expected_time))
                except _PROCESS_QUERY_ERRORS:
                    return False
                if still_current is False:
                    continue
                return False
            close_ok = True
            try:
                created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if not kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel_time), ctypes.byref(user_time),
                ):
                    return False
                actual = (int(created.high) << 32) | int(created.low)
                if actual != int(expected_time):
                    # OpenProcess may have raced with PID reuse. The original
                    # exact FILETIME cannot still own this numeric PID.
                    continue
                actual_image = ctypes.create_unicode_buffer(32768)
                image_size = ctypes.c_uint32(len(actual_image))
                if (not kernel32.QueryFullProcessImageNameW(
                        handle, 0, actual_image, ctypes.byref(image_size)) or
                        image_size.value == 0 or _path_key(actual_image.value) != _path_key(image)):
                    return False
                if not kernel32.TerminateProcess(handle, 0):
                    if kernel32.WaitForSingleObject(handle, 0) != 0:
                        return False
                    continue
                remaining_ms = max(0, min(5000, int((deadline - time.monotonic()) * 1000)))
                if kernel32.WaitForSingleObject(handle, remaining_ms) != 0:
                    return False
            except _PROCESS_QUERY_ERRORS:
                return False
            finally:
                try:
                    close_ok = bool(kernel32.CloseHandle(handle))
                except _PROCESS_QUERY_ERRORS:
                    close_ok = False
            if not close_ok:
                return False
        return True


def _onebot_port(url: str) -> int:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme in ("https", "wss") else 80


__all__ = ["HostLaunch", "ProcessRecord", "TargetBinding", "WindowsServiceError", "WindowsSystem"]
