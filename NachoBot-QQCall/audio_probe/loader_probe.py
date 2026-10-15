"""Pinned, isolated AVSDK DLL/export probe. Exported AVSDK functions are never called."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

EXPECTED_DLL_SHA256 = "757c48ddc0b5fdff101c36bf309bf9f79fe8618a64fbf0d059347ad3834a9dbf"
EXPECTED_MACHINE = 0x8664  # AMD64 PE32+
EXPECTED_EXPORT_RVAS = {
    "StartToDumpAudio": 0x32FFC0,
    "StopDumpingAudio": 0x32FFE0,
}
EXPORT_NAMES = (
    "StartToDumpAudio",
    "StopDumpingAudio",
    "PPP_InitializeModule",
    "PPP_GetInterface",
    "PPP_ShutdownModule",
)
LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR = 0x00000100
LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
LOAD_FLAGS = LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32
MAX_REPORT_BYTES = 8192
MAX_TIMEOUT_SEC = 60.0
CHILD_RESULT_NAME = "child-result.json"
FINAL_REPORT_NAME = "report.json"
CHILD_STAGES = ("dll_loaded", "export_resolved", "pcm_unverified")


class ProbeFailure(Exception):
    """An allowlisted failure that never carries an input path or native error text."""

    def __init__(self, code: str, *, dll_sha256: str | None = None, machine: str | None = None):
        super().__init__(code)
        self.code = code
        self.dll_sha256 = dll_sha256
        self.machine = machine


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pe_identity(path: Path) -> tuple[int, int]:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            dos = stream.read(64)
            if len(dos) != 64 or dos[:2] != b"MZ":
                raise ProbeFailure("invalid_pe_image")
            pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
            if pe_offset < 64 or pe_offset > size - 26:
                raise ProbeFailure("invalid_pe_image")
            stream.seek(pe_offset)
            headers = stream.read(24)
            if len(headers) != 24 or headers[:4] != b"PE\0\0":
                raise ProbeFailure("invalid_pe_image")
            machine, _sections, _timestamp, _symbols, _count, optional_size, _flags = struct.unpack_from(
                "<HHIIIHH", headers, 4
            )
            if optional_size < 60 or optional_size > 4096 or pe_offset + 24 + optional_size > size:
                raise ProbeFailure("invalid_pe_image")
            optional = stream.read(optional_size)
            if len(optional) != optional_size or struct.unpack_from("<H", optional, 0)[0] != 0x20B:
                raise ProbeFailure("invalid_pe_image")
            image_size = struct.unpack_from("<I", optional, 56)[0]
            if image_size == 0:
                raise ProbeFailure("invalid_pe_image")
            return machine, image_size
    except ProbeFailure:
        raise
    except (OSError, OverflowError, struct.error):
        raise ProbeFailure("invalid_pe_image") from None


def _verified_dll(dll: str | os.PathLike[str]) -> tuple[Path, str, int]:
    try:
        supplied = Path(dll)
    except (TypeError, ValueError):
        raise ProbeFailure("dll_path_invalid") from None
    if not supplied.is_absolute():
        raise ProbeFailure("dll_path_must_be_absolute")
    try:
        path = supplied.resolve(strict=True)
        if not path.is_file():
            raise ProbeFailure("dll_unavailable")
        digest = _sha256(path)
        machine, image_size = _pe_identity(path)
    except ProbeFailure as error:
        if error.dll_sha256 is None:
            error.dll_sha256 = locals().get("digest")
        raise
    except (OSError, RuntimeError):
        raise ProbeFailure("dll_unavailable") from None
    if machine != EXPECTED_MACHINE:
        raise ProbeFailure("unsupported_pe_machine", dll_sha256=digest, machine="unsupported")
    if digest != EXPECTED_DLL_SHA256:
        raise ProbeFailure("dll_sha256_mismatch", dll_sha256=digest, machine="AMD64")
    return path, digest, image_size


def _blank_exports() -> dict[str, dict[str, Any]]:
    return {name: {"resolved": False, "rva": None} for name in EXPORT_NAMES}


def _child_result(
    *,
    status: str = "failed",
    failure_code: str | None = None,
    dll_sha256: str | None = None,
    machine: str = "unknown",
    stages: list[str] | None = None,
    exports: dict[str, dict[str, Any]] | None = None,
    win32_error_code: int | None = None,
) -> dict[str, Any]:
    resolved = exports if exports is not None else _blank_exports()
    expected_resolved = {
        name: resolved[name]["resolved"] is True and resolved[name]["rva"] == rva
        for name, rva in EXPECTED_EXPORT_RVAS.items()
    }
    complete = all(item["resolved"] is True and isinstance(item["rva"], int) for item in resolved.values())
    metadata_resolved = complete and all(expected_resolved.values())
    if status == "ok" and not metadata_resolved:
        status = "failed"
        failure_code = failure_code or ("expected_export_rva_mismatch" if complete else "required_export_missing")
    return {
        "schema_version": 1,
        "status": status,
        "failure_code": failure_code,
        "dll_sha256": dll_sha256,
        "machine": machine,
        "stages": list(stages or []),
        "win32_error_code": win32_error_code,
        "exports": resolved,
        "expected_resolved": expected_resolved,
        "loader_metadata_resolved": metadata_resolved,
        "pcm_verified": False,
    }


def _kernel32_for_child() -> Any:
    if os.name != "nt":
        raise ProbeFailure("unsupported_platform")
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root:
        raise ProbeFailure("system_root_unavailable")
    kernel32_path = Path(system_root) / "System32" / "kernel32.dll"
    try:
        return ctypes.WinDLL(str(kernel32_path), use_last_error=True)
    except (AttributeError, OSError):
        raise ProbeFailure("kernel32_unavailable") from None


def _pointer_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, ctypes.c_void_p):
        return value.value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _last_error_code() -> int | None:
    """Return only a bounded Win32 numeric code; never expose native error text."""
    try:
        value = ctypes.get_last_error()
    except (AttributeError, OSError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 0xFFFFFFFF else None


def _probe_child(
    dll: str | os.PathLike[str],
    *,
    kernel32: Any | None = None,
) -> dict[str, Any]:
    try:
        path, digest, image_size = _verified_dll(dll)
    except ProbeFailure as error:
        return _child_result(
            failure_code=error.code,
            dll_sha256=error.dll_sha256,
            machine=error.machine or "unknown",
        )

    try:
        native = kernel32 if kernel32 is not None else _kernel32_for_child()
        load_library = native.LoadLibraryExW
        get_proc_address = native.GetProcAddress
        load_library.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
        load_library.restype = ctypes.c_void_p
        get_proc_address.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        get_proc_address.restype = ctypes.c_void_p
        module_base = _pointer_value(load_library(str(path), None, LOAD_FLAGS))
    except ProbeFailure as error:
        return _child_result(
            failure_code=error.code,
            dll_sha256=digest,
            machine="AMD64",
        )
    except (AttributeError, OSError, TypeError, ValueError, OverflowError, ctypes.ArgumentError):
        return _child_result(
            failure_code="dll_load_failed",
            dll_sha256=digest,
            machine="AMD64",
            win32_error_code=_last_error_code(),
        )
    if not module_base:
        return _child_result(
            failure_code="dll_load_failed",
            dll_sha256=digest,
            machine="AMD64",
            win32_error_code=_last_error_code(),
        )

    stages = ["dll_loaded"]
    exports = _blank_exports()
    resolution_failed = False
    for name in EXPORT_NAMES:
        try:
            address = _pointer_value(get_proc_address(module_base, name.encode("ascii")))
        except (OSError, TypeError, ValueError, OverflowError, ctypes.ArgumentError):
            resolution_failed = True
            continue
        if not address:
            continue
        rva = address - module_base
        if rva < 0 or rva >= image_size:
            # GetProcAddress can resolve a forwarded export outside this image.
            exports[name] = {"resolved": True, "rva": None}
        else:
            exports[name] = {"resolved": True, "rva": rva}

    stages.append("export_resolved")
    stages.append("pcm_unverified")
    if resolution_failed:
        return _child_result(
            failure_code="export_resolution_failed",
            dll_sha256=digest,
            machine="AMD64",
            stages=stages,
            exports=exports,
        )

    return _child_result(
        status="ok",
        dll_sha256=digest,
        machine="AMD64",
        stages=stages,
        exports=exports,
    )


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_REPORT_BYTES:
        raise ProbeFailure("report_too_large")
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProbeFailure("report_write_failed") from None


def _prepare_output_root(output_dir: str | os.PathLike[str]) -> Path:
    try:
        supplied = Path(output_dir)
    except (TypeError, ValueError):
        raise ProbeFailure("output_dir_invalid") from None
    if not supplied.is_absolute():
        raise ProbeFailure("output_dir_must_be_absolute")
    try:
        root = supplied.resolve(strict=False)
        if root.exists():
            if not root.is_dir() or next(root.iterdir(), None) is not None:
                raise ProbeFailure("output_dir_not_fresh")
        else:
            root.mkdir(parents=True, exist_ok=False)
        return root
    except ProbeFailure:
        raise
    except (OSError, RuntimeError):
        raise ProbeFailure("output_dir_unavailable") from None


def _create_child_layout(output_root: Path, source_environment: Mapping[str, str]) -> tuple[Path, dict[str, str]]:
    runtime = output_root / "runtime"
    paths = {
        "cwd": runtime / "cwd",
        "temp": runtime / "temp",
        "profile": runtime / "profile",
        "appdata": runtime / "appdata",
        "localappdata": runtime / "localappdata",
    }
    try:
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ProbeFailure("child_profile_unavailable") from None

    system_root = source_environment.get("SystemRoot") or source_environment.get("WINDIR")
    if not system_root:
        raise ProbeFailure("system_root_unavailable")
    system32 = Path(system_root) / "System32"
    environment = {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "PATH": str(system32),
        "TEMP": str(paths["temp"]),
        "TMP": str(paths["temp"]),
        "USERPROFILE": str(paths["profile"]),
        "APPDATA": str(paths["appdata"]),
        "LOCALAPPDATA": str(paths["localappdata"]),
    }
    system_drive = Path(system_root).drive
    if system_drive:
        environment["SystemDrive"] = system_drive
    return paths["cwd"], environment


def _empty_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "failed",
        "failure_code": None,
        "dll_sha256": None,
        "machine": "unknown",
        "stages": [],
        "win32_error_code": None,
        "child_exit_code": None,
        "child_timed_out": False,
        "exports": _blank_exports(),
        "expected_rvas": dict(EXPECTED_EXPORT_RVAS),
        "expected_resolved": {name: False for name in EXPECTED_EXPORT_RVAS},
        "loader_metadata_resolved": False,
        "pcm_verified": False,
    }


_CHILD_FAILURE_CODES = {
    "dll_path_invalid",
    "dll_path_must_be_absolute",
    "dll_unavailable",
    "invalid_pe_image",
    "unsupported_pe_machine",
    "dll_sha256_mismatch",
    "unsupported_platform",
    "system_root_unavailable",
    "kernel32_unavailable",
    "dll_load_failed",
    "export_resolution_failed",
    "required_export_missing",
    "expected_export_rva_mismatch",
}


def _valid_child_result(value: Any, parent_sha256: str) -> bool:
    expected_keys = {
        "schema_version", "status", "failure_code", "dll_sha256", "machine", "stages",
        "exports", "expected_resolved", "loader_metadata_resolved", "pcm_verified", "win32_error_code",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        return False
    if (
        value["schema_version"] != 1
        or value["status"] not in {"ok", "failed"}
        or value["machine"] not in {"AMD64", "unsupported", "unknown"}
        or value["pcm_verified"] is not False
        or not isinstance(value["stages"], list)
        or value["stages"] not in [list(CHILD_STAGES[:index]) for index in range(len(CHILD_STAGES) + 1)]
        or not isinstance(value["exports"], dict)
        or set(value["exports"]) != set(EXPORT_NAMES)
        or not isinstance(value["expected_resolved"], dict)
        or set(value["expected_resolved"]) != set(EXPECTED_EXPORT_RVAS)
        or not isinstance(value["loader_metadata_resolved"], bool)
    ):
        return False
    child_digest = value["dll_sha256"]
    if child_digest is not None and (
        not isinstance(child_digest, str)
        or len(child_digest) != 64
        or any(character not in "0123456789abcdef" for character in child_digest)
    ):
        return False
    error_code = value["win32_error_code"]
    if error_code is not None and (
        not isinstance(error_code, int) or isinstance(error_code, bool) or not 0 < error_code <= 0xFFFFFFFF
    ):
        return False
    exports_complete = True
    for item in value["exports"].values():
        if not isinstance(item, dict) or set(item) != {"resolved", "rva"} or not isinstance(item["resolved"], bool):
            return False
        rva = item["rva"]
        if rva is not None and (not isinstance(rva, int) or isinstance(rva, bool) or rva < 0):
            return False
        if item["resolved"] is not True or rva is None:
            exports_complete = False
    expected_resolved = {
        name: value["exports"][name]["resolved"] is True and value["exports"][name]["rva"] == expected_rva
        for name, expected_rva in EXPECTED_EXPORT_RVAS.items()
    }
    if value["expected_resolved"] != expected_resolved:
        return False
    should_be_complete = exports_complete and all(expected_resolved.values())
    if value["loader_metadata_resolved"] is not should_be_complete:
        return False
    if value["status"] == "ok":
        return (
            child_digest == parent_sha256
            and value["machine"] == "AMD64"
            and value["stages"] == list(CHILD_STAGES)
            and should_be_complete
            and value["failure_code"] is None
            and error_code is None
        )

    failure_code = value["failure_code"]
    if not isinstance(failure_code, str) or failure_code not in _CHILD_FAILURE_CODES:
        return False
    if value["loader_metadata_resolved"] is not False:
        return False
    if failure_code == "unsupported_pe_machine":
        return not value["stages"] and value["machine"] == "unsupported" and child_digest is not None
    if failure_code == "dll_sha256_mismatch":
        return (
            not value["stages"] and value["machine"] == "AMD64" and child_digest is not None
            and child_digest != parent_sha256
        )
    if failure_code == "invalid_pe_image":
        return not value["stages"] and value["machine"] == "unknown" and error_code is None
    if failure_code in {"dll_path_invalid", "dll_path_must_be_absolute", "dll_unavailable"}:
        return not value["stages"] and value["machine"] == "unknown" and error_code is None
    if failure_code in {"unsupported_platform", "system_root_unavailable", "kernel32_unavailable", "dll_load_failed"}:
        if value["stages"] or child_digest != parent_sha256 or value["machine"] != "AMD64":
            return False
        return error_code is None or failure_code == "dll_load_failed"
    if failure_code == "export_resolution_failed":
        return (
            value["stages"] == list(CHILD_STAGES)
            and child_digest == parent_sha256
            and value["machine"] == "AMD64"
            and not should_be_complete
            and error_code is None
        )
    return (
        value["stages"] == list(CHILD_STAGES)
        and child_digest == parent_sha256
        and value["machine"] == "AMD64"
        and not should_be_complete
        and error_code is None
    )


def _read_child_result(output_root: Path, parent_sha256: str) -> dict[str, Any] | None:
    path = output_root / CHILD_RESULT_NAME
    try:
        if not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if _valid_child_result(value, parent_sha256) else None


def _report_from_child(report: dict[str, Any], child: dict[str, Any]) -> None:
    report.update(
        {
            "status": "pcm_unverified" if child["status"] == "ok" else "probe_failed",
            "failure_code": child["failure_code"],
            "dll_sha256": child["dll_sha256"],
            "machine": child["machine"],
            "stages": list(child["stages"]),
            "win32_error_code": child["win32_error_code"],
            "exports": child["exports"],
            "expected_resolved": child["expected_resolved"],
            "loader_metadata_resolved": child["loader_metadata_resolved"],
            "pcm_verified": False,
        }
    )


def _kill_and_reap(process: Any, timeout: float) -> int | None:
    """Stop and reap only the child this invocation created; keep waits bounded."""
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        return process.wait(timeout=min(max(timeout, 0.1), 2.0))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            return process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            return None
    except OSError:
        # A wait API failure must not strand a child that this call started.
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            return process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            return None


def run_probe(
    dll: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    timeout_sec: float = 10.0,
    *,
    _popen_factory: Callable[..., Any] | None = None,
    _is_windows: bool | None = None,
    _source_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run the pinned loader check in a bounded, isolated child process.

    Private keyword arguments are deterministic unit-test seams; the CLI never accepts them.
    """

    if isinstance(timeout_sec, bool):
        raise ProbeFailure("invalid_timeout")
    try:
        timeout = float(timeout_sec)
    except (TypeError, ValueError, OverflowError):
        raise ProbeFailure("invalid_timeout") from None
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_SEC:
        raise ProbeFailure("invalid_timeout")
    output_root = _prepare_output_root(output_dir)
    report = _empty_report()
    try:
        dll_path, parent_sha256, _image_size = _verified_dll(dll)
    except ProbeFailure as error:
        report.update(
            {
                "status": "preflight_rejected",
                "failure_code": error.code,
                "dll_sha256": error.dll_sha256,
                "machine": error.machine or "unknown",
            }
        )
        _write_json_atomic(output_root / FINAL_REPORT_NAME, report)
        return report

    report.update({"dll_sha256": parent_sha256, "machine": "AMD64"})
    is_windows = os.name == "nt" if _is_windows is None else _is_windows
    if not is_windows:
        report.update({"status": "preflight_rejected", "failure_code": "unsupported_platform"})
        _write_json_atomic(output_root / FINAL_REPORT_NAME, report)
        return report

    try:
        child_cwd, child_environment = _create_child_layout(
            output_root,
            os.environ if _source_environment is None else _source_environment,
        )
    except ProbeFailure as error:
        report.update({"status": "preflight_rejected", "failure_code": error.code})
        _write_json_atomic(output_root / FINAL_REPORT_NAME, report)
        return report

    script_path = Path(__file__).resolve()
    command = [
        sys.executable,
        "-I",
        "-B",
        str(script_path),
        "--_child",
        str(dll_path),
        str(output_root),
    ]
    process_factory = subprocess.Popen if _popen_factory is None else _popen_factory
    try:
        process = process_factory(
            command,
            cwd=str(child_cwd),
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except (OSError, ValueError):
        report.update({"status": "child_start_failed", "failure_code": "child_start_failed"})
        _write_json_atomic(output_root / FINAL_REPORT_NAME, report)
        return report

    try:
        child_exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        report.update(
            {
                "status": "child_timed_out",
                "failure_code": "child_timeout",
                "child_timed_out": True,
            }
        )
        report["child_exit_code"] = _kill_and_reap(process, timeout)
        _write_json_atomic(output_root / FINAL_REPORT_NAME, report)
        return report
    except OSError:
        report.update({"status": "child_failed", "failure_code": "child_wait_failed"})
        report["child_exit_code"] = _kill_and_reap(process, timeout)
        _write_json_atomic(output_root / FINAL_REPORT_NAME, report)
        return report

    report["child_exit_code"] = child_exit_code
    if child_exit_code != 0:
        report.update({"status": "child_failed", "failure_code": "child_exit_nonzero"})
        _write_json_atomic(output_root / FINAL_REPORT_NAME, report)
        return report

    child_result = _read_child_result(output_root, parent_sha256)
    if child_result is None:
        report.update({"status": "child_report_invalid", "failure_code": "child_report_invalid"})
        _write_json_atomic(output_root / FINAL_REPORT_NAME, report)
        return report
    _report_from_child(report, child_result)
    _write_json_atomic(output_root / FINAL_REPORT_NAME, report)
    return report


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ProbeFailure("invalid_arguments")


def _usage() -> str:
    return (
        "Pinned AVSDK loader/export probe (never invokes exports)\n"
        "Usage: loader_probe.py --dll <absolute-vendor-dll> --output-dir <absolute-fresh-directory> "
        "[--timeout-sec 10]\n"
    )


def _child_main(dll: str, output_dir: str) -> int:
    try:
        root = Path(output_dir)
        if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
            return 2
        result = _probe_child(dll)
        _write_json_atomic(root / CHILD_RESULT_NAME, result)
        return 0
    except ProbeFailure:
        return 3


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--help"], ["-h"]):
        sys.stdout.write(_usage())
        return 0
    if args and args[0] == "--_child":
        if len(args) != 3:
            return 2
        return _child_main(args[1], args[2])

    parser = _RedactedArgumentParser(add_help=False)
    parser.add_argument("--dll", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout-sec", default="10")
    try:
        options = parser.parse_args(args)
        report = run_probe(options.dll, options.output_dir, options.timeout_sec)
    except ProbeFailure as error:
        sys.stderr.write(json.dumps({"status": "failed", "failure_code": error.code}) + "\n")
        return 2
    sys.stdout.write(json.dumps(report, ensure_ascii=True, sort_keys=True) + "\n")
    return 0 if report["loader_metadata_resolved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
