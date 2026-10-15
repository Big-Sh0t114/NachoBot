"""Isolated, candidate-only physical keeper for the pinned QQNT build.

This module is launched as a short-lived --stdio helper. It is not imported by
the agent, bootstrap, broker, or admission path. Its only filesystem behavior
is opening the fixed app tree read-only, component by component, and hashing
three pinned files through handles retained until a matching finish request.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import ntpath
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable


SCHEMA = "asar.physical.keeper.v1"
APP_PATH = r"C:\Program Files\Tencent\QQNT\versions\9.9.26-44343\resources\app"
KERNEL_IMAGE_PATH = r"C:\Program Files\Tencent\QQNT\QQ.exe"
APP_COMPONENTS = (
    "Program Files", "Tencent", "QQNT", "versions", "9.9.26-44343", "resources", "app",
)
PINNED_SHA256 = {
    "application.asar": "9C9959CFA351E8597C008C944DD482F826A24967ABA607F56350B06AC56200E2",
    "package.json": "15557B5A5A49A9C866D21048316DB50723642E13067CA12AFB36AF35ED3ECDC9",
    "wrapper.node": "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2",
}
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024
MAX_WRAPPER_BYTES = 128 * 1024 * 1024
READ_BUFFER_BYTES = 1024 * 1024
MAX_INPUT_LINE_BYTES = 4096
MAX_OUTPUT_LINE_BYTES = 64 * 1024
MAX_RUNTIME_SECONDS = 15.0
MAX_FILETIME = (1 << 64) - 1
MAX_SAFE_INTEGER = (1 << 53) - 1
ZERO_NONCE = "0" * 64
NONCE_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
FILETIME_RE = re.compile(r"^[1-9][0-9]{0,19}$")
FILE_SPECS = (
    ("application.asar", MAX_ARCHIVE_BYTES),
    ("package.json", MAX_PACKAGE_BYTES),
    ("wrapper.node", MAX_WRAPPER_BYTES),
)

REJECTION_CODES = frozenset(
    {
        "input_too_large", "input_eof", "input_timeout", "input_read_failed", "input_invalid",
        "input_schema_rejected", "finish_schema_rejected", "unknown_operation", "nonce_invalid",
        "nonce_mismatch", "binding_invalid", "app_path_rejected", "process_query_unsupported",
        "process_identity_mismatch", "process_identity_changed", "native_query_unsupported",
        "component_open_failed", "component_reparse_rejected", "component_type_rejected",
        "canonical_path_mismatch", "file_type_rejected", "file_size_invalid", "file_size_exceeded",
        "file_metadata_changed", "file_hash_unavailable", "file_hash_mismatch", "output_too_large",
        "output_write_failed", "runtime_deadline_exceeded", "resource_close_failed", "internal_error",
    }
)


class KeeperReject(Exception):
    """An internal rejection containing only one fixed public code."""

    def __init__(self, code: str):
        super().__init__()
        self.code = code if code in REJECTION_CODES else "internal_error"


@dataclass(frozen=True)
class NativeMetadata:
    attributes: int
    is_directory: bool
    volume_serial: int
    file_id: bytes
    final_path: str
    size: int
    last_write_time_100ns: int


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_time_100ns: int
    image_path: str


class ResourceStack:
    """Close registered resources in reverse order, including partial opens."""

    def __init__(self) -> None:
        self._closers: list[Callable[[], None]] = []
        self.closed = False

    def add(self, close: Callable[[], None]) -> None:
        if self.closed:
            raise KeeperReject("internal_error")
        self._closers.append(close)

    def close_all(self) -> bool:
        clean = True
        if self.closed:
            return True
        self.closed = True
        while self._closers:
            close = self._closers.pop()
            try:
                close()
            except Exception:
                clean = False
        return clean


def _canonical_windows_path(value: str) -> str:
    if value.lower().startswith("\\\\?\\unc\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normpath(value)


def _same_path(left: str, right: str) -> bool:
    return ntpath.normcase(_canonical_windows_path(left)) == ntpath.normcase(_canonical_windows_path(right))


def _check_deadline(deadline: float, clock: Callable[[], float]) -> None:
    try:
        if clock() >= deadline:
            raise KeeperReject("runtime_deadline_exceeded")
    except KeeperReject:
        raise
    except Exception:
        raise KeeperReject("runtime_deadline_exceeded") from None


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KeeperReject("input_invalid")
        result[key] = value
    return result


def _read_line_with_deadline(
    stream: BinaryIO,
    max_bytes: int,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    """Read one bounded line without letting a silent parent hold the process."""
    _check_deadline(deadline, clock)
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def read_once() -> None:
        try:
            result.put((True, stream.readline(max_bytes + 1)))
        except BaseException:
            result.put((False, None))

    threading.Thread(target=read_once, name="asar-keeper-stdin", daemon=True).start()
    try:
        remaining = max(0.0, deadline - clock())
        ok, value = result.get(timeout=remaining)
    except queue.Empty:
        raise KeeperReject("input_timeout") from None
    if not ok or not isinstance(value, bytes):
        raise KeeperReject("input_read_failed")
    if len(value) > max_bytes:
        raise KeeperReject("input_too_large")
    return value


def _read_json_line(
    stream: BinaryIO,
    deadline: float,
    clock: Callable[[], float],
    reader: Callable[[BinaryIO, int, float, Callable[[], float]], bytes],
) -> dict[str, Any]:
    raw = reader(stream, MAX_INPUT_LINE_BYTES, deadline, clock)
    if not raw:
        raise KeeperReject("input_eof")
    if len(raw) > MAX_INPUT_LINE_BYTES:
        raise KeeperReject("input_too_large")
    if not raw.endswith(b"\n"):
        raise KeeperReject("input_invalid")
    raw = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1]
    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(KeeperReject("input_invalid")),
        )
    except KeeperReject:
        raise
    except Exception:
        raise KeeperReject("input_invalid") from None
    if not isinstance(value, dict):
        raise KeeperReject("input_invalid")
    return value


def _parse_initial_request(value: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "schema", "nonce", "kernelPid", "kernelCreationTime100ns", "connectionGeneration", "appPath",
    }
    if set(value) != expected_keys:
        raise KeeperReject("input_schema_rejected")
    if value.get("schema") != SCHEMA:
        raise KeeperReject("input_schema_rejected")
    nonce = value.get("nonce")
    if type(nonce) is not str or NONCE_RE.fullmatch(nonce) is None:
        raise KeeperReject("nonce_invalid")
    pid = value.get("kernelPid")
    if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
        raise KeeperReject("binding_invalid")
    creation = value.get("kernelCreationTime100ns")
    if type(creation) is not str or FILETIME_RE.fullmatch(creation) is None:
        raise KeeperReject("binding_invalid")
    if int(creation) > MAX_FILETIME or str(int(creation)) != creation:
        raise KeeperReject("binding_invalid")
    generation = value.get("connectionGeneration")
    if type(generation) is not int or not 1 <= generation <= MAX_SAFE_INTEGER:
        raise KeeperReject("binding_invalid")
    app_path = value.get("appPath")
    if (
        type(app_path) is not str
        or "\x00" in app_path
        or not ntpath.isabs(app_path)
        or ntpath.splitdrive(app_path)[0].upper() != "C:"
        or not _same_path(app_path, APP_PATH)
    ):
        raise KeeperReject("app_path_rejected")
    return {
        "nonce": nonce,
        "kernelPid": pid,
        "kernelCreationTime100ns": creation,
        "connectionGeneration": generation,
        "appPath": APP_PATH,
    }


def _parse_finish_request(value: dict[str, Any], nonce: str) -> None:
    if set(value) != {"op", "nonce"}:
        raise KeeperReject("finish_schema_rejected")
    if value.get("op") != "finish":
        raise KeeperReject("unknown_operation")
    if type(value.get("nonce")) is not str or NONCE_RE.fullmatch(value["nonce"]) is None:
        raise KeeperReject("nonce_invalid")
    if value["nonce"] != nonce:
        raise KeeperReject("nonce_mismatch")


def _record_process_identity(
    process: Any,
    expected_pid: int,
    deadline: float,
    clock: Callable[[], float],
    *,
    identity_failure: str = "process_identity_mismatch",
) -> ProcessIdentity:
    _check_deadline(deadline, clock)
    try:
        identity = process.identity()
    except KeeperReject:
        raise
    except Exception:
        raise KeeperReject("process_query_unsupported") from None
    if not isinstance(identity, ProcessIdentity):
        raise KeeperReject("process_query_unsupported")
    if (
        type(identity.pid) is not int
        or identity.pid != expected_pid
        or type(identity.creation_time_100ns) is not int
        or not 1 <= identity.creation_time_100ns <= MAX_FILETIME
        or type(identity.image_path) is not str
        or not identity.image_path
        or "\x00" in identity.image_path
    ):
        raise KeeperReject(identity_failure)
    return identity


def _same_process_identity(left: ProcessIdentity, right: ProcessIdentity) -> bool:
    return (
        left.pid == right.pid
        and left.creation_time_100ns == right.creation_time_100ns
        and ntpath.normcase(ntpath.normpath(left.image_path)) == ntpath.normcase(ntpath.normpath(right.image_path))
    )


def _metadata_identity(metadata: NativeMetadata) -> tuple[Any, ...]:
    return (
        metadata.attributes,
        metadata.is_directory,
        metadata.volume_serial,
        metadata.file_id,
        _canonical_windows_path(metadata.final_path),
        metadata.size,
        metadata.last_write_time_100ns,
    )


class PhysicalKeeperSession:
    def __init__(
        self,
        native: Any,
        process_handles: dict[str, Any],
        resource_stack: ResourceStack,
        request: dict[str, Any],
        keeper_identity: ProcessIdentity,
        kernel_identity: ProcessIdentity,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        self.native = native
        self.process_handles = process_handles
        self.resources = resource_stack
        self.request = request
        self.keeper_identity = keeper_identity
        self.kernel_identity = kernel_identity
        self.deadline = deadline
        self.clock = clock
        self.root_handles: list[int] = []
        self.file_handles: dict[str, int] = {}
        self.ready_records: list[dict[str, Any]] = []

    def _native_call(self, function: Callable[..., Any], *args: Any, failure: str = "native_query_unsupported", **kwargs: Any) -> Any:
        _check_deadline(self.deadline, self.clock)
        try:
            return function(*args, **kwargs)
        except KeeperReject:
            raise
        except Exception:
            raise KeeperReject(failure) from None

    def _keep_native_handle(self, handle: Any) -> Any:
        if handle is None:
            raise KeeperReject("component_open_failed")
        self.resources.add(lambda handle=handle: self.native.close(handle))
        return handle

    def _validate_metadata(self, metadata: Any, expected_path: str, *, directory: bool) -> NativeMetadata:
        if not isinstance(metadata, NativeMetadata):
            raise KeeperReject("native_query_unsupported")
        if metadata.attributes & WindowsPhysicalNative.FILE_ATTRIBUTE_REPARSE_POINT:
            raise KeeperReject("component_reparse_rejected")
        if metadata.is_directory is not directory:
            raise KeeperReject("component_type_rejected" if directory else "file_type_rejected")
        if not _same_path(metadata.final_path, expected_path):
            raise KeeperReject("canonical_path_mismatch")
        if (
            type(metadata.volume_serial) is not int
            or not 0 <= metadata.volume_serial <= MAX_FILETIME
            or type(metadata.file_id) is not bytes
            or len(metadata.file_id) != 16
            or type(metadata.size) is not int
            or metadata.size < 0
            or type(metadata.last_write_time_100ns) is not int
            or not 0 <= metadata.last_write_time_100ns <= MAX_FILETIME
        ):
            raise KeeperReject("native_query_unsupported")
        return NativeMetadata(
            metadata.attributes,
            metadata.is_directory,
            metadata.volume_serial,
            metadata.file_id,
            _canonical_windows_path(metadata.final_path),
            metadata.size,
            metadata.last_write_time_100ns,
        )

    def _measure_file(
        self,
        name: str,
        handle: Any,
        expected_path: str,
        max_bytes: int,
        expected_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        before_raw = self._native_call(self.native.metadata, handle)
        before = self._validate_metadata(before_raw, expected_path, directory=False)
        if before.size > max_bytes:
            raise KeeperReject("file_size_exceeded")
        if expected_record is not None and self._record_from_metadata(name, before, expected_record["sha256"]) != expected_record:
            raise KeeperReject("file_metadata_changed")

        self._native_call(self.native.seek_to_start, handle, failure="file_hash_unavailable")
        digest = hashlib.sha256()
        offset = 0
        while offset < before.size:
            _check_deadline(self.deadline, self.clock)
            requested = min(READ_BUFFER_BYTES, before.size - offset)
            chunk = self._native_call(self.native.read_chunk, handle, requested, failure="file_hash_unavailable")
            if type(chunk) is not bytes or not chunk or len(chunk) > requested:
                raise KeeperReject("file_hash_unavailable")
            digest.update(chunk)
            offset += len(chunk)
        if offset != before.size:
            raise KeeperReject("file_hash_unavailable")
        after_raw = self._native_call(self.native.metadata, handle)
        after = self._validate_metadata(after_raw, expected_path, directory=False)
        if _metadata_identity(before) != _metadata_identity(after):
            raise KeeperReject("file_metadata_changed")
        sha256 = digest.hexdigest().upper()
        if sha256 != PINNED_SHA256[name]:
            raise KeeperReject("file_hash_mismatch")
        if expected_record is not None and sha256 != expected_record["sha256"]:
            raise KeeperReject("file_metadata_changed")
        return self._record_from_metadata(name, after, sha256)

    @staticmethod
    def _record_from_metadata(name: str, metadata: NativeMetadata, sha256: str) -> dict[str, Any]:
        return {
            "name": name,
            "nativeVolumeSerial": str(metadata.volume_serial),
            "nativeFileId": metadata.file_id.hex().upper(),
            "canonicalFinalPath": metadata.final_path,
            "size": metadata.size,
            "lastWriteTime100ns": str(metadata.last_write_time_100ns),
            "sha256": sha256,
        }

    def open_and_verify(self) -> None:
        drive = ntpath.splitdrive(APP_PATH)[0]
        root_path = drive + "\\"
        root_handle = self._keep_native_handle(
            self._native_call(self.native.open_volume_root, drive, failure="component_open_failed")
        )
        self.root_handles.append(root_handle)
        root_meta = self._validate_metadata(
            self._native_call(self.native.metadata, root_handle), root_path, directory=True,
        )
        parent_handle = root_handle
        parent_path = root_meta.final_path
        for component in APP_COMPONENTS:
            expected_path = ntpath.join(parent_path, component)
            handle = self._keep_native_handle(
                self._native_call(
                    self.native.open_child,
                    parent_handle,
                    component,
                    directory=True,
                    share=WindowsPhysicalNative.FILE_SHARE_READ,
                    failure="component_open_failed",
                )
            )
            self.root_handles.append(handle)
            metadata = self._validate_metadata(
                self._native_call(self.native.metadata, handle), expected_path, directory=True,
            )
            parent_handle = handle
            parent_path = metadata.final_path

        app_handle = parent_handle
        for name, max_bytes in FILE_SPECS:
            expected_path = ntpath.join(APP_PATH, name)
            handle = self._keep_native_handle(
                self._native_call(
                    self.native.open_child,
                    app_handle,
                    name,
                    directory=False,
                    share=WindowsPhysicalNative.FILE_SHARE_READ,
                    failure="component_open_failed",
                )
            )
            self.file_handles[name] = handle
            record = self._measure_file(name, handle, expected_path, max_bytes)
            self.ready_records.append(record)

    def verify_processes_unchanged(self) -> None:
        keeper_now = _record_process_identity(
            self.process_handles["keeper"], self.keeper_identity.pid, self.deadline, self.clock,
            identity_failure="process_identity_changed",
        )
        kernel_now = _record_process_identity(
            self.process_handles["kernel"], self.kernel_identity.pid, self.deadline, self.clock,
            identity_failure="process_identity_changed",
        )
        if not _same_process_identity(self.keeper_identity, keeper_now):
            raise KeeperReject("process_identity_changed")
        if not _same_process_identity(self.kernel_identity, kernel_now):
            raise KeeperReject("process_identity_changed")

    def verify_held_files_unchanged(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for original, (name, max_bytes) in zip(self.ready_records, FILE_SPECS):
            _check_deadline(self.deadline, self.clock)
            record = self._measure_file(
                name,
                self.file_handles[name],
                ntpath.join(APP_PATH, name),
                max_bytes,
                expected_record=original,
            )
            if record != original:
                raise KeeperReject("file_metadata_changed")
            records.append(record)
        return records

    def response_frame(self, state: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "nonce": self.request["nonce"],
            "state": state,
            "keeperPid": self.keeper_identity.pid,
            "keeperCreationTime100ns": str(self.keeper_identity.creation_time_100ns),
            "kernelPid": self.request["kernelPid"],
            "kernelCreationTime100ns": self.request["kernelCreationTime100ns"],
            "connectionGeneration": self.request["connectionGeneration"],
            "appPath": APP_PATH,
            "files": records,
        }


def _write_frame(stream: BinaryIO, frame: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    except Exception:
        raise KeeperReject("output_write_failed") from None
    if len(encoded) > MAX_OUTPUT_LINE_BYTES:
        raise KeeperReject("output_too_large")
    try:
        if stream.write(encoded) != len(encoded):
            raise KeeperReject("output_write_failed")
        stream.flush()
    except KeeperReject:
        raise
    except Exception:
        raise KeeperReject("output_write_failed") from None


def _write_rejection(stream: BinaryIO, code: str) -> None:
    fixed_code = code if code in REJECTION_CODES else "internal_error"
    try:
        _write_frame(stream, {"schema": SCHEMA, "state": "rejected", "code": fixed_code})
    except KeeperReject:
        return


def run_stdio(
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    native_factory: Callable[[], Any] | None = None,
    process_factory: Callable[[int], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    reader: Callable[[BinaryIO, int, float, Callable[[], float]], bytes] = _read_line_with_deadline,
    current_pid: Callable[[], int] = os.getpid,
) -> int:
    """Serve one initialization and one finish frame; keep resources only in memory."""
    resources = ResourceStack()
    request_nonce = ZERO_NONCE
    deadline: float
    try:
        started = clock()
        deadline = started + MAX_RUNTIME_SECONDS
        initial_value = _read_json_line(stdin, deadline, clock, reader)
        if isinstance(initial_value.get("nonce"), str) and NONCE_RE.fullmatch(initial_value["nonce"]):
            request_nonce = initial_value["nonce"]
        request = _parse_initial_request(initial_value)
        request_nonce = request["nonce"]
        _check_deadline(deadline, clock)

        native_ctor = native_factory or WindowsPhysicalNative
        process_ctor = process_factory or WindowsProcessHandle
        native = native_ctor()
        keeper_pid = current_pid()
        if type(keeper_pid) is not int or not 1 <= keeper_pid <= 0xFFFFFFFF:
            raise KeeperReject("process_query_unsupported")

        keeper_process = process_ctor(keeper_pid)
        resources.add(keeper_process.close)
        kernel_process = process_ctor(request["kernelPid"])
        resources.add(kernel_process.close)
        keeper_identity = _record_process_identity(keeper_process, keeper_pid, deadline, clock)
        kernel_identity = _record_process_identity(kernel_process, request["kernelPid"], deadline, clock)
        if (
            str(kernel_identity.creation_time_100ns) != request["kernelCreationTime100ns"]
            or not _same_path(kernel_identity.image_path, KERNEL_IMAGE_PATH)
        ):
            raise KeeperReject("process_identity_mismatch")

        session = PhysicalKeeperSession(
            native,
            {"keeper": keeper_process, "kernel": kernel_process},
            resources,
            request,
            keeper_identity,
            kernel_identity,
            deadline,
            clock,
        )
        session.open_and_verify()
        session.verify_processes_unchanged()
        ready = session.response_frame("ready", session.ready_records)
        _write_frame(stdout, ready)

        finish_value = _read_json_line(stdin, deadline, clock, reader)
        _parse_finish_request(finish_value, request_nonce)
        _check_deadline(deadline, clock)
        session.verify_processes_unchanged()
        final_records = session.verify_held_files_unchanged()
        session.verify_processes_unchanged()
        _check_deadline(deadline, clock)
        final_frame = session.response_frame("final", final_records)
        if not resources.close_all():
            raise KeeperReject("resource_close_failed")
        _write_frame(stdout, final_frame)
        return 0
    except KeeperReject as failure:
        _write_rejection(stdout, failure.code)
        return 1
    except Exception:
        _write_rejection(stdout, "internal_error")
        return 1
    finally:
        resources.close_all()


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_uint32), ("ReparseTag", ctypes.c_uint32)]


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", ctypes.c_uint32),
    ]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", ctypes.c_uint32),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


class WindowsPhysicalNative:
    OBJ_CASE_INSENSITIVE = 0x00000040
    OBJ_DONT_REPARSE = 0x00001000
    FILE_SHARE_READ = 0x00000001
    FILE_OPEN = 0x00000001
    FILE_DIRECTORY_FILE = 0x00000001
    FILE_NON_DIRECTORY_FILE = 0x00000040
    FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    FILE_OPEN_REPARSE_POINT = 0x00200000
    FILE_READ_DATA = 0x00000001
    FILE_LIST_DIRECTORY = 0x00000001
    FILE_TRAVERSE = 0x00000020
    FILE_READ_ATTRIBUTES = 0x00000080
    SYNCHRONIZE = 0x00100000
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    FILE_BASIC_INFO_CLASS = 0
    FILE_STANDARD_INFO_CLASS = 1
    FILE_ID_INFO_CLASS = 18

    def __init__(self) -> None:
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_wchar) != 2 or ctypes.sizeof(ctypes.c_ulong) != 4:
            raise KeeperReject("native_query_unsupported")
        if (
            ctypes.sizeof(_UnicodeString) != 16
            or ctypes.sizeof(_ObjectAttributes) != 48
            or ctypes.sizeof(_IoStatusBlock) != 16
            or ctypes.sizeof(_FileAttributeTagInfo) != 8
            or ctypes.sizeof(_FileBasicInfo) != 40
            or ctypes.sizeof(_FileStandardInfo) != 24
            or ctypes.sizeof(_FileIdInfo) != 24
        ):
            raise KeeperReject("native_query_unsupported")
        try:
            self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._bind_functions()
        except Exception:
            raise KeeperReject("native_query_unsupported") from None

    def _bind_functions(self) -> None:
        handle = ctypes.c_void_p
        ulong = ctypes.c_ulong
        self.ntdll.NtCreateFile.restype = ctypes.c_long
        self.ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(handle), ulong, ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock), ctypes.POINTER(ctypes.c_longlong),
            ulong, ulong, ulong, ulong, handle, ulong,
        ]
        self.ntdll.NtClose.restype = ctypes.c_long
        self.ntdll.NtClose.argtypes = [handle]
        self.kernel32.GetFileInformationByHandleEx.restype = ctypes.c_int
        self.kernel32.GetFileInformationByHandleEx.argtypes = [handle, ctypes.c_int, handle, ulong]
        self.kernel32.GetFinalPathNameByHandleW.restype = ulong
        self.kernel32.GetFinalPathNameByHandleW.argtypes = [handle, ctypes.c_wchar_p, ulong, ulong]
        self.kernel32.SetFilePointerEx.restype = ctypes.c_int
        self.kernel32.SetFilePointerEx.argtypes = [handle, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), ulong]
        self.kernel32.ReadFile.restype = ctypes.c_int
        self.kernel32.ReadFile.argtypes = [handle, handle, ulong, ctypes.POINTER(ulong), handle]

    @staticmethod
    def _unicode(value: str) -> tuple[_UnicodeString, Any]:
        encoded = value.encode("utf-16-le")
        if not encoded or len(encoded) > 0xFFFC:
            raise KeeperReject("component_open_failed")
        buffer = ctypes.create_unicode_buffer(value)
        string = _UnicodeString(len(encoded), len(encoded) + 2, ctypes.cast(buffer, ctypes.c_void_p))
        return string, buffer

    def _open(self, name: str, root_handle: Any, *, directory: bool, share: int) -> int:
        name_string, name_buffer = self._unicode(name)
        object_attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            ctypes.c_void_p(root_handle) if root_handle is not None else None,
            ctypes.pointer(name_string),
            self.OBJ_CASE_INSENSITIVE | self.OBJ_DONT_REPARSE,
            None,
            None,
        )
        io_status = _IoStatusBlock()
        output = ctypes.c_void_p()
        access = self.FILE_READ_ATTRIBUTES | self.SYNCHRONIZE
        options = self.FILE_SYNCHRONOUS_IO_NONALERT | self.FILE_OPEN_REPARSE_POINT
        if directory:
            access |= self.FILE_TRAVERSE | self.FILE_LIST_DIRECTORY
            options |= self.FILE_DIRECTORY_FILE
        else:
            access |= self.FILE_READ_DATA
            options |= self.FILE_NON_DIRECTORY_FILE
        status = self.ntdll.NtCreateFile(
            ctypes.byref(output), access, ctypes.byref(object_attributes), ctypes.byref(io_status),
            None, 0, share, self.FILE_OPEN, options, None, 0,
        )
        _ = name_buffer  # keep the UNICODE_STRING buffer alive through NtCreateFile
        if status < 0 or not output.value:
            raise KeeperReject("component_open_failed")
        return int(output.value)

    def open_volume_root(self, drive: str) -> int:
        if drive.upper() != "C:":
            raise KeeperReject("app_path_rejected")
        return self._open("\\??\\C:\\", None, directory=True, share=self.FILE_SHARE_READ)

    def open_child(self, parent: int, component: str, *, directory: bool, share: int) -> int:
        if (
            type(component) is not str
            or not component
            or component in {".", ".."}
            or any(ch in component for ch in ("\\", "/", ":", "\x00"))
            or share != self.FILE_SHARE_READ
        ):
            raise KeeperReject("component_open_failed")
        return self._open(component, parent, directory=directory, share=share)

    def _query(self, handle: int, info_class: int, target: ctypes.Structure) -> ctypes.Structure:
        ok = self.kernel32.GetFileInformationByHandleEx(
            ctypes.c_void_p(handle), info_class, ctypes.byref(target), ctypes.sizeof(target),
        )
        if not ok:
            raise KeeperReject("native_query_unsupported")
        return target

    def _final_path(self, handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        size = self.kernel32.GetFinalPathNameByHandleW(ctypes.c_void_p(handle), buffer, len(buffer), 0)
        if size == 0 or size >= len(buffer):
            raise KeeperReject("native_query_unsupported")
        return _canonical_windows_path(buffer.value)

    def metadata(self, handle: int) -> NativeMetadata:
        attributes = self._query(handle, self.FILE_ATTRIBUTE_TAG_INFO_CLASS, _FileAttributeTagInfo())
        basic = self._query(handle, self.FILE_BASIC_INFO_CLASS, _FileBasicInfo())
        standard = self._query(handle, self.FILE_STANDARD_INFO_CLASS, _FileStandardInfo())
        file_id = self._query(handle, self.FILE_ID_INFO_CLASS, _FileIdInfo())
        return NativeMetadata(
            int(attributes.FileAttributes),
            bool(standard.Directory),
            int(file_id.VolumeSerialNumber),
            bytes(file_id.FileId),
            self._final_path(handle),
            int(standard.EndOfFile),
            int(basic.LastWriteTime),
        )

    def seek_to_start(self, handle: int) -> None:
        position = ctypes.c_longlong()
        if not self.kernel32.SetFilePointerEx(ctypes.c_void_p(handle), 0, ctypes.byref(position), 0):
            raise KeeperReject("file_hash_unavailable")

    def read_chunk(self, handle: int, count: int) -> bytes:
        if type(count) is not int or not 1 <= count <= READ_BUFFER_BYTES:
            raise KeeperReject("file_hash_unavailable")
        # The caller resets the same held file handle once for each bounded
        # whole-file digest, then ReadFile advances that handle's byte position.
        buffer = ctypes.create_string_buffer(count)
        read_count = ctypes.c_ulong()
        if not self.kernel32.ReadFile(
            ctypes.c_void_p(handle), buffer, count, ctypes.byref(read_count), None,
        ):
            raise KeeperReject("file_hash_unavailable")
        if read_count.value > count:
            raise KeeperReject("file_hash_unavailable")
        return bytes(buffer.raw[: read_count.value])

    def close(self, handle: int) -> None:
        status = self.ntdll.NtClose(ctypes.c_void_p(handle))
        if status < 0:
            raise KeeperReject("native_query_unsupported")


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class WindowsProcessHandle:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
    SYNCHRONIZE = 0x00100000

    def __init__(self, pid: int) -> None:
        if os.name != "nt" or type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
            raise KeeperReject("process_query_unsupported")
        try:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.OpenProcess.restype = ctypes.c_void_p
            self.kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            self.kernel32.GetProcessId.restype = ctypes.c_uint32
            self.kernel32.GetProcessId.argtypes = [ctypes.c_void_p]
            self.kernel32.GetProcessTimes.restype = ctypes.c_int
            self.kernel32.GetProcessTimes.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
            ]
            self.kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
            self.kernel32.QueryFullProcessImageNameW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32),
            ]
            self.kernel32.CloseHandle.restype = ctypes.c_int
            self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            self.handle = self.kernel32.OpenProcess(
                self.PROCESS_QUERY_LIMITED_INFORMATION | self.SYNCHRONIZE, 0, pid,
            )
        except Exception:
            raise KeeperReject("process_query_unsupported") from None
        if not self.handle:
            raise KeeperReject("process_query_unsupported")
        self.expected_pid = pid

    def identity(self) -> ProcessIdentity:
        try:
            pid = int(self.kernel32.GetProcessId(self.handle))
            created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
            if not self.kernel32.GetProcessTimes(
                self.handle,
                ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
            ):
                raise KeeperReject("process_query_unsupported")
            creation = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            exit_time = (int(exited.dwHighDateTime) << 32) | int(exited.dwLowDateTime)
            if pid == 0 or exit_time != 0:
                raise KeeperReject("process_identity_changed")
            image = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(len(image))
            if not self.kernel32.QueryFullProcessImageNameW(self.handle, 0, image, ctypes.byref(size)):
                raise KeeperReject("process_query_unsupported")
            if size.value == 0 or size.value >= len(image):
                raise KeeperReject("process_query_unsupported")
            return ProcessIdentity(pid, creation, image.value)
        except KeeperReject:
            raise
        except Exception:
            raise KeeperReject("process_query_unsupported") from None

    def close(self) -> None:
        if getattr(self, "handle", None):
            handle, self.handle = self.handle, None
            if not self.kernel32.CloseHandle(handle):
                raise KeeperReject("process_query_unsupported")


def main() -> int:
    if sys.argv[1:] != ["--stdio"]:
        return 2
    input_buffer = getattr(sys.stdin, "buffer", None)
    output_stream = getattr(sys.stdout, "buffer", None)
    # Read the unbuffered pipe endpoint. The bounded deadline reader runs in a
    # daemon thread; using BufferedReader here could leave its internal lock
    # held if a silent parent times out and the interpreter starts shutdown.
    input_stream = getattr(input_buffer, "raw", None)
    if input_stream is None or output_stream is None or not callable(getattr(input_stream, "readline", None)):
        if output_stream is not None:
            _write_rejection(output_stream, "input_read_failed")
        return 1
    return run_stdio(input_stream, output_stream)


if __name__ == "__main__":
    raise SystemExit(main())
