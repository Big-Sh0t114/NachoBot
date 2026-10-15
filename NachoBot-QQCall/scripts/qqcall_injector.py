#!/usr/bin/env python3
"""Metadata-only by default x64 loader for the QQ Kernel Shim Probe."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import ntpath
import os
import re
import struct
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_CREATE_THREAD = 0x0002
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
STILL_ACTIVE = 259
ERROR_NO_MORE_FILES = 18
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESSOR_ARCHITECTURE_AMD64 = 9
IMAGE_FILE_MACHINE_UNKNOWN = 0
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_I386 = 0x014C
MAX_SAFE_INTEGER = 9_007_199_254_740_991
FILETIME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)

FLAG_CAPABILITY_SCAN = 0x00000001
FLAG_IAT_ARMED = 0x00000002
FLAG_BOOTSTRAP_SCHEDULED = 0x00000004
FLAG_IAT_RESTORED = 0x00000008
FLAG_CANCELLED = 0x00000010
FLAG_LOADER_AVAILABLE = 0x00000020
FLAG_AGENT_OUTCOME_PENDING = 0x00000040
FLAG_JS_EXCEPTION_CLEARED = 0x00000080
FLAG_CALLBACK_CAPTURED = 0x00000100
FLAG_SCRIPT_FAILED = 0x00000200

ERROR_CODES = {
    0: "none",
    10: "config_path",
    11: "config_missing",
    12: "config_invalid",
    13: "config_binding_mismatch",
    14: "controller_busy",
    20: "wrapper_ambiguous",
    21: "wrapper_not_found",
    22: "qqnt_not_found",
    23: "import_layout",
    24: "import_not_found",
    25: "import_already_hooked",
    26: "napi_export_missing",
    27: "iat_protect",
    28: "iat_compare_exchange",
    29: "iat_ownership_lost",
    30: "cancel_too_late",
    31: "cancel_not_armed",
    40: "exception_api",
    41: "preexisting_exception_preserved",
    42: "create_string_failed",
    43: "run_script_failed",
    44: "script_status_unavailable",
    45: "shim_exception_clear_failed",
    46: "loader_unavailable",
    47: "set_immediate_unavailable",
    48: "cancelled",
    50: "status_pointer_invalid",
}


class ProbeError(RuntimeError):
    pass


class RemoteTimeout(ProbeError):
    def __init__(self, operation: str, retained_path: str | None = None):
        super().__init__(operation)
        self.retained_path = retained_path


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    path: str
    base: int
    size: int


@dataclass(frozen=True)
class TargetIdentity:
    pid: int
    image_path: str
    creation_filetime: int

    @property
    def created_utc(self) -> str:
        return filetime_to_datetime(self.creation_filetime).isoformat(timespec="microseconds").replace("+00:00", "Z")


def filetime_to_datetime(value: int) -> dt.datetime:
    return FILETIME_EPOCH + dt.timedelta(microseconds=value // 10)


def datetime_to_filetime(value: dt.datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("expected start time must include a timezone")
    delta = value.astimezone(dt.timezone.utc) - FILETIME_EPOCH
    return ((delta.days * 86400 + delta.seconds) * 10_000_000) + delta.microseconds * 10


def validate_expected_start(expected: str, actual_filetime: int) -> None:
    try:
        parsed = dt.datetime.fromisoformat(expected.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbeError("--expected-start must be an ISO-8601 timestamp with timezone") from exc
    expected_filetime = datetime_to_filetime(parsed)
    if abs(expected_filetime - actual_filetime) > 9:
        raise ProbeError("QQ.exe process start time no longer matches --discover output")


def _is_windows_absolute(value: object) -> bool:
    return isinstance(value, str) and bool(value) and ntpath.isabs(value) and not any(c in value for c in "\r\n\0")


def _validate_path(value: object, label: str, *, must_exist: bool, directory: bool = False) -> str:
    if not _is_windows_absolute(value):
        raise ProbeError(f"config {label} must be an absolute Windows path")
    assert isinstance(value, str)
    if value != value.strip():
        raise ProbeError(f"config {label} cannot have leading or trailing whitespace")
    if must_exist:
        path = Path(value)
        if not path.exists() or (directory and not path.is_dir()) or (not directory and not path.is_file()):
            kind = "directory" if directory else "file"
            raise ProbeError(f"config {label} does not point to an existing {kind}")
    return value


def validate_config(config_path: str | os.PathLike[str], pid: int) -> dict[str, object]:
    path = Path(config_path)
    if not path.is_absolute() or not path.is_file():
        raise ProbeError("--config must be an existing absolute JSON file")
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbeError("config could not be read as UTF-8 JSON") from exc
    if not isinstance(config, dict):
        raise ProbeError("config root must be a JSON object")

    result: dict[str, object] = {}
    result["modulePath"] = _validate_path(config.get("modulePath"), "modulePath", must_exist=True)
    result["appPath"] = _validate_path(config.get("appPath"), "appPath", must_exist=True, directory=True)
    result["reportPath"] = _validate_path(config.get("reportPath"), "reportPath", must_exist=False)
    pipe_name = config.get("pipeName")
    if not isinstance(pipe_name, str) or any(c in pipe_name for c in "\r\n\0"):
        raise ProbeError("config pipeName is invalid")
    match = re.fullmatch(rf"\\\\\.\\pipe\\nachobot-qqcall-{pid}-([A-Fa-f0-9]{{16,64}})", pipe_name)
    if not match:
        raise ProbeError("config pipeName is not bound to the requested QQ PID and nonce format")
    result["pipeName"] = pipe_name

    session_name = config.get("sessionName")
    if not isinstance(session_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", session_name):
        raise ProbeError("config sessionName must match the agent's safe identifier format")
    result["sessionName"] = session_name

    generation = config.get("connectionGeneration")
    if isinstance(generation, bool) or not isinstance(generation, int) or not 1 <= generation <= MAX_SAFE_INTEGER:
        raise ProbeError("config connectionGeneration must be a positive JavaScript-safe integer")
    result["connectionGeneration"] = generation

    token = config.get("ipcToken")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise ProbeError("config ipcToken must be a private base64url or hexadecimal token")
    result["ipcToken"] = token
    return result


def sanitized_config_summary(config: dict[str, object]) -> dict[str, object]:
    return {
        "modulePath": config.get("modulePath"),
        "appPath": config.get("appPath"),
        "sessionName": config.get("sessionName"),
        "connectionGeneration": config.get("connectionGeneration"),
        "reportPath": config.get("reportPath"),
        "pipeConfigured": bool(config.get("pipeName")),
        "ipcTokenConfigured": bool(config.get("ipcToken")),
    }


def build_ini(config: dict[str, object]) -> str:
    keys = (
        "modulePath",
        "pipeName",
        "appPath",
        "sessionName",
        "reportPath",
        "ipcToken",
        "connectionGeneration",
    )
    lines = ["[QQCallShim]"]
    for key in keys:
        value = str(config[key])
        if any(c in value for c in "\r\n\0"):
            raise ProbeError(f"config {key} contains an unsupported line break")
        lines.append(f"{key}={value}")
    return "\r\n".join(lines) + "\r\n"


def _normalize_module_name(name: str) -> str:
    base = ntpath.basename(name).casefold()
    return base if base.endswith(".dll") else base + ".dll"


def _read_c_string(read: Callable[[int, int], bytes], address: int, maximum: int) -> bytes:
    if maximum <= 0 or maximum > 4096:
        raise ProbeError("invalid PE string bound")
    data = read(address, maximum)
    end = data.find(b"\0")
    if end < 0:
        raise ProbeError("unterminated PE string")
    return data[:end]


def _parse_remote_headers(read: Callable[[int, int], bytes], module: ModuleInfo) -> tuple[int, int, int]:
    if module.size < 0x1000 or module.base <= 0:
        raise ProbeError("remote module has invalid size or base")
    dos = read(module.base, 64)
    if dos[:2] != b"MZ":
        raise ProbeError("remote module has no DOS header")
    e_lfanew = struct.unpack_from("<I", dos, 0x3C)[0]
    if e_lfanew > min(module.size - 24, 1 << 20):
        raise ProbeError("remote PE header offset is out of range")
    nt = read(module.base + e_lfanew, 24)
    if nt[:4] != b"PE\0\0" or struct.unpack_from("<H", nt, 4)[0] != IMAGE_FILE_MACHINE_AMD64:
        raise ProbeError("remote module is not an x64 PE image")
    optional_size = struct.unpack_from("<H", nt, 20)[0]
    if optional_size < 120 or optional_size > 4096:
        raise ProbeError("remote optional header size is invalid")
    optional = read(module.base + e_lfanew + 24, optional_size)
    if struct.unpack_from("<H", optional, 0)[0] != 0x20B:
        raise ProbeError("remote module does not use the PE32+ header")
    image_size = struct.unpack_from("<I", optional, 56)[0]
    directory_count = struct.unpack_from("<I", optional, 108)[0]
    if image_size == 0 or image_size > module.size or directory_count < 1 or len(optional) < 120:
        raise ProbeError("remote PE image bounds are invalid")
    export_rva, export_size = struct.unpack_from("<II", optional, 112)
    if not export_rva or export_size < 40 or export_rva > image_size or export_size > image_size - export_rva:
        raise ProbeError("remote export directory is invalid")
    return image_size, export_rva, export_size


def _find_export_rva(
    read: Callable[[int, int], bytes], module: ModuleInfo, symbol: str | int
) -> tuple[int, tuple[int, int]]:
    image_size, export_rva, export_size = _parse_remote_headers(read, module)
    export = read(module.base + export_rva, 40)
    ordinal_base, function_count, name_count = struct.unpack_from("<III", export, 16)
    functions_rva, names_rva, ordinals_rva = struct.unpack_from("<III", export, 28)
    if function_count > 1_000_000 or name_count > 1_000_000:
        raise ProbeError("remote PE export counts exceed safety bounds")
    if functions_rva > image_size or function_count * 4 > image_size - functions_rva:
        raise ProbeError("remote PE function table is out of range")
    if names_rva > image_size or name_count * 4 > image_size - names_rva:
        raise ProbeError("remote PE name table is out of range")
    if ordinals_rva > image_size or name_count * 2 > image_size - ordinals_rva:
        raise ProbeError("remote PE ordinal table is out of range")

    if isinstance(symbol, int):
        index = symbol - ordinal_base
        if not 0 <= index < function_count:
            raise ProbeError(f"remote export ordinal {symbol} is unavailable")
    else:
        name_ptrs = read(module.base + names_rva, name_count * 4)
        ordinal_entries = read(module.base + ordinals_rva, name_count * 2)
        encoded = symbol.encode("ascii")
        index = -1
        for item in range(name_count):
            name_rva = struct.unpack_from("<I", name_ptrs, item * 4)[0]
            if name_rva >= image_size:
                raise ProbeError("remote PE export name is out of range")
            name = _read_c_string(read, module.base + name_rva, min(image_size - name_rva, 512))
            if name == encoded:
                index = struct.unpack_from("<H", ordinal_entries, item * 2)[0]
                break
        if index < 0:
            raise ProbeError(f"remote export {symbol} is unavailable")
    if index >= function_count:
        raise ProbeError("remote export ordinal indexes beyond the function table")
    function_rva = struct.unpack("<I", read(module.base + functions_rva + index * 4, 4))[0]
    if function_rva == 0 or function_rva >= image_size:
        raise ProbeError("remote export function RVA is invalid")
    return function_rva, (export_rva, export_size)


def resolve_remote_export(
    read: Callable[[int, int], bytes],
    modules: Iterable[ModuleInfo],
    module_name: str,
    symbol: str | int,
    *,
    forwarded_owner: Callable[[str, str | int], str | None] | None = None,
    depth: int = 0,
) -> int:
    if depth > 8:
        raise ProbeError("remote export forwarder recursion exceeded safety bound")
    by_name = {_normalize_module_name(module.name): module for module in modules}
    module = by_name.get(_normalize_module_name(module_name))
    if module is None:
        if forwarded_owner is None or isinstance(symbol, int):
            raise ProbeError(f"forwarded module {module_name} is not loaded in target")
        owner = forwarded_owner(module_name, symbol)
        if not owner:
            raise ProbeError(f"cannot resolve forwarded module {module_name} in target")
        return resolve_remote_export(read, modules, owner, symbol, forwarded_owner=forwarded_owner, depth=depth + 1)

    function_rva, (export_rva, export_size) = _find_export_rva(read, module, symbol)
    if export_rva <= function_rva < export_rva + export_size:
        forwarder = _read_c_string(read, module.base + function_rva, min(export_rva + export_size - function_rva, 512)).decode("ascii")
        if "." not in forwarder:
            raise ProbeError("remote export forwarder is malformed")
        target_module, target_symbol = forwarder.rsplit(".", 1)
        if target_symbol.startswith("#") and target_symbol[1:].isdigit():
            next_symbol: str | int = int(target_symbol[1:])
        else:
            next_symbol = target_symbol
        return resolve_remote_export(read, modules, target_module, next_symbol, forwarded_owner=forwarded_owner, depth=depth + 1)
    return module.base + function_rva


def _local_forwarded_owner(module_name: str, symbol: str | int) -> str | None:
    if os.name != "nt" or isinstance(symbol, int):
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        requested = module_name if module_name.casefold().endswith(".dll") else module_name + ".dll"
        local_module = ctypes.WinDLL(requested, use_last_error=True)
        function = getattr(local_module, symbol)
        address = ctypes.cast(function, ctypes.c_void_p).value
        if not address:
            return None
        get_module_handle_ex = kernel32.GetModuleHandleExW
        get_module_handle_ex.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        get_module_handle_ex.restype = ctypes.c_int
        owner = ctypes.c_void_p()
        flags = 0x00000004 | 0x00000002  # from-address, unchanged reference count
        if not get_module_handle_ex(flags, ctypes.c_void_p(address), ctypes.byref(owner)):
            return None
        get_module_filename = kernel32.GetModuleFileNameW
        get_module_filename.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_module_filename.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_module_filename(owner, buffer, len(buffer))
        if not length or length >= len(buffer):
            return None
        return ntpath.basename(buffer.value)
    except (AttributeError, OSError, TypeError):
        return None


def _load_winapi() -> "WinApi":
    if os.name != "nt":
        raise ProbeError("the injector supports Windows only")
    return WinApi()


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    @property
    def value(self) -> int:
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("th32ModuleID", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("GlblcntUsage", ctypes.c_uint32),
        ("ProccntUsage", ctypes.c_uint32),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", ctypes.c_uint32),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


class WinApi:
    def __init__(self) -> None:
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise ProbeError("run the x64 Python interpreter for an x64 QQ process")
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        self.k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        self.k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        self.k32.Process32FirstW.restype = ctypes.c_int
        self.k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        self.k32.Process32NextW.restype = ctypes.c_int
        self.k32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
        self.k32.Module32FirstW.restype = ctypes.c_int
        self.k32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
        self.k32.Module32NextW.restype = ctypes.c_int
        self.k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.k32.CloseHandle.restype = ctypes.c_int
        self.k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self.k32.OpenProcess.restype = ctypes.c_void_p
        self.k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        self.k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        self.k32.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
        self.k32.GetProcessTimes.restype = ctypes.c_int
        self.k32.IsWow64Process.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.k32.IsWow64Process.restype = ctypes.c_int
        self.k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.k32.ReadProcessMemory.restype = ctypes.c_int
        self.k32.WriteProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.k32.WriteProcessMemory.restype = ctypes.c_int
        self.k32.VirtualAllocEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
        self.k32.VirtualAllocEx.restype = ctypes.c_void_p
        self.k32.VirtualFreeEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
        self.k32.VirtualFreeEx.restype = ctypes.c_int
        self.k32.CreateRemoteThread.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        self.k32.CreateRemoteThread.restype = ctypes.c_void_p
        self.k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.k32.WaitForSingleObject.restype = ctypes.c_uint32
        self.k32.GetExitCodeThread.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.k32.GetExitCodeThread.restype = ctypes.c_int
        self.k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.k32.GetExitCodeProcess.restype = ctypes.c_int
        self.k32.IsWow64Process2 = getattr(self.k32, "IsWow64Process2", None)
        if self.k32.IsWow64Process2:
            self.k32.IsWow64Process2.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(ctypes.c_uint16)]
            self.k32.IsWow64Process2.restype = ctypes.c_int

    def check(self, ok: int, operation: str) -> None:
        if not ok:
            raise ProbeError(f"{operation} failed (WinError {ctypes.get_last_error()})")

    def snapshot_processes(self) -> list[tuple[int, str]]:
        snapshot = self.k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            raise ProbeError(f"process snapshot failed (WinError {ctypes.get_last_error()})")
        result: list[tuple[int, str]] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if self.k32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    result.append((int(entry.th32ProcessID), entry.szExeFile))
                    entry.dwSize = ctypes.sizeof(entry)
                    if not self.k32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            self.k32.CloseHandle(snapshot)
        return result

    def open_process(self, pid: int, *, mutation: bool = False) -> ctypes.c_void_p:
        rights = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | SYNCHRONIZE
        if mutation:
            rights |= PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION | PROCESS_VM_WRITE
        handle = self.k32.OpenProcess(rights, False, pid)
        if not handle:
            raise ProbeError(f"OpenProcess({pid}) failed (WinError {ctypes.get_last_error()})")
        return handle

    def identity(self, handle: ctypes.c_void_p, pid: int) -> TargetIdentity:
        path = ctypes.create_unicode_buffer(32768)
        length = ctypes.c_uint32(len(path))
        self.check(self.k32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)), "QueryFullProcessImageNameW")
        if ntpath.basename(path.value).casefold() != "qq.exe":
            raise ProbeError(f"PID {pid} is not QQ.exe")
        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        self.check(self.k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)), "GetProcessTimes")
        return TargetIdentity(pid=pid, image_path=path.value, creation_filetime=created.value)

    def ensure_x64(self, handle: ctypes.c_void_p) -> None:
        if self.k32.IsWow64Process2:
            process_machine = ctypes.c_uint16()
            native_machine = ctypes.c_uint16()
            self.check(self.k32.IsWow64Process2(handle, ctypes.byref(process_machine), ctypes.byref(native_machine)), "IsWow64Process2")
            if process_machine.value != IMAGE_FILE_MACHINE_UNKNOWN or native_machine.value != IMAGE_FILE_MACHINE_AMD64:
                raise ProbeError("target is not a native x64 process")
            return
        wow64 = ctypes.c_int()
        self.check(self.k32.IsWow64Process(handle, ctypes.byref(wow64)), "IsWow64Process")
        if wow64.value:
            raise ProbeError("target is a 32-bit process under WOW64")

    def modules(self, pid: int) -> list[ModuleInfo]:
        snapshot = self.k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if snapshot == INVALID_HANDLE_VALUE:
            raise ProbeError(f"module snapshot failed for PID {pid} (WinError {ctypes.get_last_error()})")
        result: list[ModuleInfo] = []
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if self.k32.Module32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    result.append(ModuleInfo(entry.szModule, entry.szExePath, int(entry.modBaseAddr or 0), int(entry.modBaseSize)))
                    entry.dwSize = ctypes.sizeof(entry)
                    if not self.k32.Module32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            self.k32.CloseHandle(snapshot)
        return result

    def read_memory(self, handle: ctypes.c_void_p, address: int, size: int) -> bytes:
        if size < 0 or size > 1 << 20:
            raise ProbeError("remote memory read exceeded 1 MiB bound")
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        self.check(self.k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)), "ReadProcessMemory")
        if read.value != size:
            raise ProbeError("ReadProcessMemory returned a short read")
        return buffer.raw[:size]

    def write_memory(self, handle: ctypes.c_void_p, address: int, data: bytes) -> None:
        buffer = ctypes.create_string_buffer(data, len(data))
        written = ctypes.c_size_t()
        self.check(self.k32.WriteProcessMemory(handle, ctypes.c_void_p(address), buffer, len(data), ctypes.byref(written)), "WriteProcessMemory")
        if written.value != len(data):
            raise ProbeError("WriteProcessMemory returned a short write")

    def alloc(self, handle: ctypes.c_void_p, size: int) -> int:
        address = self.k32.VirtualAllocEx(handle, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not address:
            raise ProbeError(f"VirtualAllocEx({size}) failed (WinError {ctypes.get_last_error()})")
        return int(address)

    def free(self, handle: ctypes.c_void_p, address: int) -> None:
        if address:
            self.check(self.k32.VirtualFreeEx(handle, ctypes.c_void_p(address), 0, MEM_RELEASE), "VirtualFreeEx")

    def remote_thread(self, handle: ctypes.c_void_p, start: int, parameter: int, timeout_ms: int, operation: str) -> int:
        thread = self.k32.CreateRemoteThread(handle, None, 0, ctypes.c_void_p(start), ctypes.c_void_p(parameter or 0), 0, None)
        if not thread:
            raise ProbeError(f"CreateRemoteThread({operation}) failed (WinError {ctypes.get_last_error()})")
        try:
            wait = self.k32.WaitForSingleObject(thread, timeout_ms)
            if wait == WAIT_TIMEOUT:
                raise RemoteTimeout(f"remote {operation} timed out; remote buffer retained")
            if wait == WAIT_FAILED:
                raise RemoteTimeout(f"WaitForSingleObject({operation}) failed (WinError {ctypes.get_last_error()}); remote buffer retained")
            if wait != WAIT_OBJECT_0:
                raise RemoteTimeout(f"remote {operation} returned unconfirmed wait state {wait}; remote buffer retained")
            exit_code = ctypes.c_uint32()
            self.check(self.k32.GetExitCodeThread(thread, ctypes.byref(exit_code)), "GetExitCodeThread")
            return int(exit_code.value)
        finally:
            self.k32.CloseHandle(thread)


def _require_qq_identity(api: WinApi, pid: int, expected_start: str | None, mutation: bool) -> tuple[ctypes.c_void_p, TargetIdentity]:
    handle = api.open_process(pid, mutation=mutation)
    try:
        identity = api.identity(handle, pid)
        if expected_start:
            validate_expected_start(expected_start, identity.creation_filetime)
        api.ensure_x64(handle)
        return handle, identity
    except Exception:
        api.k32.CloseHandle(handle)
        raise


def discover() -> list[TargetIdentity]:
    api = _load_winapi()
    found: list[TargetIdentity] = []
    for pid, exe_name in api.snapshot_processes():
        if exe_name.casefold() != "qq.exe":
            continue
        try:
            handle = api.open_process(pid)
            try:
                identity = api.identity(handle, pid)
                api.ensure_x64(handle)
                found.append(identity)
            finally:
                api.k32.CloseHandle(handle)
        except ProbeError:
            continue
    return found


def _read_local_image(path: Path) -> tuple[bytes, int, int, int]:
    data = path.read_bytes()
    if len(data) < 0x1000 or data[:2] != b"MZ":
        raise ProbeError("shim DLL is not a valid PE image")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 > len(data) or data[pe : pe + 4] != b"PE\0\0":
        raise ProbeError("shim DLL PE header is truncated")
    if struct.unpack_from("<H", data, pe + 4)[0] != IMAGE_FILE_MACHINE_AMD64:
        raise ProbeError("shim DLL is not x64")
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    if optional_size < 120 or optional + optional_size > len(data) or struct.unpack_from("<H", data, optional)[0] != 0x20B:
        raise ProbeError("shim DLL optional header is invalid")
    image_size = struct.unpack_from("<I", data, optional + 56)[0]
    export_rva, export_size = struct.unpack_from("<II", data, optional + 112)
    return data, image_size, export_rva, export_size


def _local_export_rva(data: bytes, export_rva: int, export_size: int, symbol: str) -> int:
    if not export_rva or export_rva + 40 > len(data):
        raise ProbeError("shim DLL export directory is unavailable")
    export = data[export_rva : export_rva + 40]
    ordinal_base, function_count, name_count = struct.unpack_from("<III", export, 16)
    functions_rva, names_rva, ordinals_rva = struct.unpack_from("<III", export, 28)
    if function_count > 10000 or name_count > 10000:
        raise ProbeError("shim DLL export count is invalid")
    for i in range(name_count):
        name_rva = struct.unpack_from("<I", data, names_rva + i * 4)[0]
        end = data.find(b"\0", name_rva, min(len(data), name_rva + 512))
        if end < 0:
            raise ProbeError("shim DLL export name is unterminated")
        if data[name_rva:end] == symbol.encode("ascii"):
            ordinal = struct.unpack_from("<H", data, ordinals_rva + i * 2)[0]
            if ordinal >= function_count:
                raise ProbeError("shim DLL export ordinal is invalid")
            function_rva = struct.unpack_from("<I", data, functions_rva + ordinal * 4)[0]
            if export_rva <= function_rva < export_rva + export_size:
                raise ProbeError("shim DLL export unexpectedly uses a forwarder")
            return function_rva
    raise ProbeError(f"shim DLL does not export {symbol}")


def _find_exact_module(modules: Iterable[ModuleInfo], path: str) -> ModuleInfo | None:
    wanted = ntpath.normcase(ntpath.normpath(path)).removeprefix("\\\\?\\")
    matches = [m for m in modules if ntpath.normcase(ntpath.normpath(m.path)).removeprefix("\\\\?\\") == wanted]
    if len(matches) > 1:
        raise ProbeError("target has ambiguous modules with the requested path")
    return matches[0] if matches else None


def _find_module_name(modules: Iterable[ModuleInfo], name: str) -> ModuleInfo | None:
    matches = [m for m in modules if _normalize_module_name(m.name) == _normalize_module_name(name)]
    if len(matches) > 1:
        raise ProbeError(f"target has multiple loaded modules named {name}")
    return matches[0] if matches else None


def _resolve_kernel32_loadlibrary(api: WinApi, handle: ctypes.c_void_p, modules: list[ModuleInfo]) -> int:
    reader = lambda address, size: api.read_memory(handle, address, size)
    return resolve_remote_export(reader, modules, "kernel32.dll", "LoadLibraryW", forwarded_owner=_local_forwarded_owner)


def _make_remote_config_file(config: dict[str, object]) -> str:
    with tempfile.NamedTemporaryFile(prefix="qqcall-shim-", suffix=".ini", delete=False) as file:
        path = file.name
        file.write(("\ufeff" + build_ini(config)).encode("utf-16le"))
    return os.path.abspath(path)


def _remote_call_with_buffer(
    api: WinApi,
    handle: ctypes.c_void_p,
    address: int,
    data: bytes,
    timeout_ms: int,
    operation: str,
    retained_path: str | None = None,
) -> int:
    remote_buffer = api.alloc(handle, len(data))
    created_thread = False
    try:
        api.write_memory(handle, remote_buffer, data)
        try:
            return api.remote_thread(handle, address, remote_buffer, timeout_ms, operation)
        except RemoteTimeout as exc:
            created_thread = True
            exc.retained_path = retained_path
            raise
    finally:
        # remote_thread returns/raises only after a thread exists. On timeout its
        # parameter is still in use, so intentionally leave that allocation live.
        if not created_thread:
            api.free(handle, remote_buffer)


def _verify_handle_identity(api: WinApi, handle: ctypes.c_void_p, expected: TargetIdentity) -> None:
    if api.k32.WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
        raise ProbeError("QQ process exited before the requested remote operation")
    current = api.identity(handle, expected.pid)
    if current.creation_filetime != expected.creation_filetime or ntpath.normcase(current.image_path) != ntpath.normcase(expected.image_path):
        raise ProbeError("QQ process identity changed before the requested remote operation")


def _selected_shim_path(recovery_shim: bool = False, recovery_attempt: int = 1) -> Path:
    if recovery_attempt not in (1, 2):
        raise ProbeError("recovery attempt must be 1 or 2")
    if not recovery_shim and recovery_attempt != 1:
        raise ProbeError("--recovery-attempt requires --recovery-shim")
    root = Path(__file__).resolve().parents[1]
    if recovery_shim:
        filename = "QQCallRecoveryShim.dll" if recovery_attempt == 1 else "QQCallRecoveryShim2.dll"
    else:
        filename = "QQCallShim.dll"
    return root / "native" / "build" / "x64" / filename


def _find_selected_shim(
    modules: Iterable[ModuleInfo],
    shim_path: str,
    recovery_shim: bool = False,
) -> ModuleInfo | None:
    # Preserve the existing default lookup behavior. Recovery mode is explicit
    # and binds only to the fixed recovery copy's exact path.
    if recovery_shim:
        return _find_exact_module(modules, shim_path)
    return _find_module_name(modules, "QQCallShim.dll")


def inject(
    pid: int,
    config_path: str,
    expected_start: str,
    timeout_ms: int,
    dry_run: bool = False,
    recovery_shim: bool = False,
    recovery_attempt: int = 1,
) -> dict[str, object]:
    config = validate_config(config_path, pid)
    dll_path = _selected_shim_path(recovery_shim, recovery_attempt)
    module_name = dll_path.name
    if not dll_path.is_file():
        if recovery_shim:
            raise ProbeError(f"selected recovery shim is missing: {module_name}")
        raise ProbeError("build QQCallShim.dll with native/build.ps1 before injection")
    dll_bytes, _image_size, _export_rva, _export_size = _read_local_image(dll_path)
    if dry_run:
        api = _load_winapi()
        handle, identity = _require_qq_identity(api, pid, expected_start, mutation=False)
        try:
            modules = api.modules(pid)
            wrapper = _find_module_name(modules, "wrapper.node")
            qqnt = _find_module_name(modules, "QQNT.dll")
            return {
                "ok": True,
                "dryRun": True,
                "target": {"pid": identity.pid, "imagePath": identity.image_path, "createdUtc": identity.created_utc},
                "capabilitiesObserved": {"wrapperLoaded": wrapper is not None, "qqntLoaded": qqnt is not None},
                "shimPath": str(dll_path),
                "config": sanitized_config_summary(config),
                "processChanges": False,
            }
        finally:
            api.k32.CloseHandle(handle)

    api = _load_winapi()
    handle, identity = _require_qq_identity(api, pid, expected_start, mutation=True)
    config_file: str | None = None
    leaked_remote: int | None = None
    keep_config_file = False
    try:
        modules = api.modules(pid)
        existing = _find_exact_module(modules, str(dll_path))
        if existing is None:
            _verify_handle_identity(api, handle, identity)
            load_library = _resolve_kernel32_loadlibrary(api, handle, modules)
            encoded_path = (str(dll_path) + "\0").encode("utf-16le")
            remote_path = api.alloc(handle, len(encoded_path))
            try:
                api.write_memory(handle, remote_path, encoded_path)
                try:
                    api.remote_thread(handle, load_library, remote_path, timeout_ms, "LoadLibraryW")
                except RemoteTimeout:
                    leaked_remote = remote_path
                    raise
            finally:
                if leaked_remote != remote_path:
                    api.free(handle, remote_path)
            modules = api.modules(pid)
            existing = _find_exact_module(modules, str(dll_path))
            if existing is None:
                raise ProbeError(f"LoadLibraryW completed but {module_name} was not present in the target module list")

        export_read = lambda address, size: _read_local_rva_from_loaded(api, handle, existing, address, size)
        start = resolve_remote_export(export_read, [existing], module_name, "QQCallShimStart", forwarded_owner=_local_forwarded_owner)
        config_file = _make_remote_config_file(config)
        config_bytes = (config_file + "\0").encode("utf-16le")
        _verify_handle_identity(api, handle, identity)
        start_result = _remote_call_with_buffer(api, handle, start, config_bytes, timeout_ms, "QQCallShimStart", config_file)
        status = query_status(api, handle, pid, timeout_ms, str(dll_path) if recovery_shim else None)
        return {
            "ok": start_result == 0,
            "operation": "inject",
            "target": {"pid": identity.pid, "imagePath": identity.image_path, "createdUtc": identity.created_utc},
            "startReturnCode": start_result,
            "startReturn": ERROR_CODES.get(start_result, "unknown"),
            "status": status,
            "shimPath": str(dll_path),
            "config": sanitized_config_summary(config),
        }
    except RemoteTimeout as exc:
        if config_file and exc.retained_path is None:
            exc.retained_path = config_file
        if config_file and exc.retained_path == config_file:
            keep_config_file = True
        raise
    finally:
        if config_file and leaked_remote is None and not keep_config_file:
            try:
                os.unlink(config_file)
            except OSError:
                pass
        api.k32.CloseHandle(handle)


def _read_local_rva_from_loaded(api: WinApi, handle: ctypes.c_void_p, module: ModuleInfo, address: int, size: int) -> bytes:
    return api.read_memory(handle, address, size)


def query_status(
    api: WinApi,
    handle: ctypes.c_void_p,
    pid: int,
    timeout_ms: int,
    shim_path: str | None = None,
) -> dict[str, object]:
    modules = api.modules(pid)
    recovery_shim = shim_path is not None
    selected_path = shim_path or str(_selected_shim_path(False))
    shim = _find_selected_shim(modules, selected_path, recovery_shim)
    if shim is None:
        return {"loaded": False}
    reader = lambda address, size: api.read_memory(handle, address, size)
    status_fn = resolve_remote_export(reader, [shim], shim.name, "QQCallShimGetStatus", forwarded_owner=_local_forwarded_owner)
    buffer = api.alloc(handle, 32)
    try:
        api.write_memory(handle, buffer, bytes(32))
        try:
            code = api.remote_thread(handle, status_fn, buffer, timeout_ms, "QQCallShimGetStatus")
        except RemoteTimeout:
            # The export writes through this pointer; retain memory until the
            # still-running remote thread has finished.
            buffer = 0
            raise
        if code != 0:
            raise ProbeError(f"QQCallShimGetStatus returned error {code}")
        snapshot = reader(buffer, 32)
        size, version, flags, error_code, process_id, _reserved, generation = struct.unpack("<6IQ", snapshot)
        if size != 32 or version != 1 or process_id != pid:
            raise ProbeError("QQCallShimGetStatus returned an invalid snapshot")
        return {
            "loaded": True,
            "pid": process_id,
            "flags": flags,
            "pending": bool(flags & FLAG_IAT_ARMED and not flags & (FLAG_CALLBACK_CAPTURED | FLAG_CANCELLED)),
            "capabilityScan": bool(flags & FLAG_CAPABILITY_SCAN),
            "iatArmed": bool(flags & FLAG_IAT_ARMED),
            "callbackCaptured": bool(flags & FLAG_CALLBACK_CAPTURED),
            "iatRestored": bool(flags & FLAG_IAT_RESTORED),
            "loaderAvailable": bool(flags & FLAG_LOADER_AVAILABLE),
            "scriptScheduled": bool(flags & FLAG_BOOTSTRAP_SCHEDULED),
            "scriptFailed": bool(flags & FLAG_SCRIPT_FAILED),
            "shimExceptionCleared": bool(flags & FLAG_JS_EXCEPTION_CLEARED),
            "agentOutcomePending": bool(flags & FLAG_AGENT_OUTCOME_PENDING),
            "cancelled": bool(flags & FLAG_CANCELLED),
            "connectionGeneration": generation,
            "errorCode": error_code,
            "error": ERROR_CODES.get(error_code, "unknown"),
        }
    finally:
        if buffer:
            api.free(handle, buffer)


def cancel(
    pid: int,
    config_path: str,
    expected_start: str,
    timeout_ms: int,
    recovery_shim: bool = False,
    recovery_attempt: int = 1,
) -> dict[str, object]:
    config = validate_config(config_path, pid)
    api = _load_winapi()
    handle, identity = _require_qq_identity(api, pid, expected_start, mutation=True)
    config_file: str | None = None
    keep_config_file = False
    try:
        modules = api.modules(pid)
        dll_path = _selected_shim_path(recovery_shim, recovery_attempt)
        shim = _find_selected_shim(modules, str(dll_path), recovery_shim)
        if shim is None:
            raise ProbeError(f"{dll_path.name} is not loaded in the requested QQ process")
        reader = lambda address, size: api.read_memory(handle, address, size)
        cancel_fn = resolve_remote_export(reader, [shim], shim.name, "QQCallShimCancel", forwarded_owner=_local_forwarded_owner)
        config_file = _make_remote_config_file(config)
        _verify_handle_identity(api, handle, identity)
        result = _remote_call_with_buffer(api, handle, cancel_fn, (config_file + "\0").encode("utf-16le"), timeout_ms, "QQCallShimCancel", config_file)
        result = {
            "ok": result == 0,
            "operation": "cancel",
            "target": {"pid": identity.pid, "imagePath": identity.image_path, "createdUtc": identity.created_utc},
            "cancelReturnCode": result,
            "cancelReturn": ERROR_CODES.get(result, "unknown"),
            "status": query_status(api, handle, pid, timeout_ms, str(dll_path) if recovery_shim else None),
        }
        if recovery_shim:
            result["shimPath"] = str(dll_path)
        return result
    except RemoteTimeout as exc:
        if config_file and exc.retained_path is None:
            exc.retained_path = config_file
        if config_file and exc.retained_path == config_file:
            keep_config_file = True
        raise
    finally:
        if config_file and not keep_config_file:
            try:
                os.unlink(config_file)
            except OSError:
                pass
        api.k32.CloseHandle(handle)


def _read_config_report_path(config_path: str | None) -> str | None:
    if not config_path:
        return None
    path = Path(config_path)
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    report_path = obj.get("reportPath") if isinstance(obj, dict) else None
    return report_path if _is_windows_absolute(report_path) else None


def _load_redacted_report(path: str | None, suffix: str) -> dict[str, object] | None:
    if not path:
        return None
    report_path = Path(path + suffix)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(report, dict):
        return None
    allowed = {
        "pid", "processPid", "phase", "status", "errorCode", "connectionGeneration",
        "pending", "callbackCaptured", "capabilityScan", "iatArmed", "iatRestored",
        "loaderAvailable", "bootstrapScheduled", "scriptFailed", "agentOutcomePending",
        "cancelled", "capabilities", "nodeVersion", "electronVersion",
    }
    return {key: report[key] for key in report if key in allowed}


def status(
    pid: int,
    timeout_ms: int,
    config_path: str | None = None,
    recovery_shim: bool = False,
    recovery_attempt: int = 1,
) -> dict[str, object]:
    api = _load_winapi()
    handle, identity = _require_qq_identity(api, pid, None, mutation=True)
    try:
        shim_path = _selected_shim_path(recovery_shim, recovery_attempt)
        result = query_status(api, handle, pid, timeout_ms, str(shim_path) if recovery_shim else None)
        if recovery_shim:
            result["shimPath"] = str(shim_path)
        report_path = _read_config_report_path(config_path)
        if report_path:
            result["shimReport"] = _load_redacted_report(report_path, ".shim.json")
            result["agentReport"] = _load_redacted_report(report_path, ".agent.json")
            result["bootstrapReport"] = _load_redacted_report(report_path, ".bootstrap.json")
        result["target"] = {"pid": identity.pid, "imagePath": identity.image_path, "createdUtc": identity.created_utc}
        return result
    finally:
        api.k32.CloseHandle(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--discover", action="store_true", help="list QQ.exe PID, image path, and process start only")
    action.add_argument("--inject", action="store_true", help="load the shim and arm its one-shot bootstrap")
    action.add_argument("--cancel", action="store_true", help="restore this shim's slot before callback capture")
    action.add_argument("--status", action="store_true", help="read a loaded shim status snapshot")
    action.add_argument("--dry-run", "--check", dest="dry_run", action="store_true", help="validate config and target with no process changes")
    parser.add_argument("--pid", type=int, help="explicit QQ.exe process ID")
    parser.add_argument("--config", help="broker-generated UTF-8 JSON config")
    parser.add_argument(
        "--recovery-shim",
        action="store_true",
        help="select a fixed recovery shim for inject, cancel, status, or dry-run",
    )
    parser.add_argument("--recovery-attempt", type=int, choices=(1, 2), help="recovery shim instance (requires --recovery-shim)")
    parser.add_argument("--expected-start", help="exact createdUtc timestamp from --discover; required for mutation")
    parser.add_argument("--timeout-ms", type=int, default=10000)
    args = parser.parse_args(argv)
    try:
        if args.recovery_attempt is not None and not args.recovery_shim:
            raise ProbeError("--recovery-attempt requires --recovery-shim")
        recovery_attempt = args.recovery_attempt or 1
        if args.discover:
            items = discover()
            print(json.dumps({"ok": True, "processes": [
                {"pid": item.pid, "imagePath": item.image_path, "createdUtc": item.created_utc, "startFileTime": str(item.creation_filetime)}
                for item in items
            ]}, ensure_ascii=False))
            return 0
        if args.pid is None or args.pid <= 0:
            raise ProbeError("--pid is required for this operation")
        if args.timeout_ms < 100 or args.timeout_ms > 120000:
            raise ProbeError("--timeout-ms must be between 100 and 120000")
        if args.status:
            print(json.dumps(status(
                args.pid,
                args.timeout_ms,
                args.config,
                recovery_shim=args.recovery_shim,
                recovery_attempt=recovery_attempt,
            ), ensure_ascii=False))
            return 0
        if not args.config:
            raise ProbeError("--config is required for inject, cancel, and dry-run")
        if args.inject or args.cancel:
            if not args.expected_start:
                raise ProbeError("--expected-start from a fresh --discover result is required for process mutation")
        if args.inject or args.dry_run:
            result = inject(
                args.pid,
                args.config,
                args.expected_start,
                args.timeout_ms,
                dry_run=args.dry_run,
                recovery_shim=args.recovery_shim,
                recovery_attempt=recovery_attempt,
            )
        else:
            result = cancel(
                args.pid,
                args.config,
                args.expected_start,
                args.timeout_ms,
                recovery_shim=args.recovery_shim,
                recovery_attempt=recovery_attempt,
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except RemoteTimeout as exc:
        payload: dict[str, object] = {"ok": False, "error": str(exc), "remoteMemoryRetained": True}
        if exc.retained_path:
            payload["configFileRetained"] = exc.retained_path
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 3
    except ProbeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

