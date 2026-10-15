"""Shared QQCall service configuration and platform-binding heartbeat.

The optional adapter reference is validated by the fixed in-repository
supervisor schema module. Nothing from the referenced configuration directory
is imported as Python code.
"""

from __future__ import annotations

import asyncio
import ctypes
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import importlib.util
import json
import logging
import os
import stat
import threading
import time
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .qqcall import QQCallConfig


_LOGGER = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VALIDATOR_PATH = _REPO_ROOT / "NachoBot-QQCall" / "service" / "config.py"
_SERVICE_PROJECT_ROOT = _REPO_ROOT / "NachoBot-QQCall"
_PRIVATE_FILES_PATH = _SERVICE_PROJECT_ROOT / "service" / "private_files.py"
_VALIDATOR_MODULE_NAME = "_nachobot_qqcall_fixed_service_config"
_PRIVATE_FILES_MODULE_NAME = "_nachobot_qqcall_fixed_private_files"
_VALIDATOR_LOCK = threading.Lock()
_VALIDATOR: Any | None = None
_PRIVATE_FILES: Any | None = None
_MAX_PUBLIC_CONFIG_BYTES = 64 * 1024
_MAX_AUDIO_PLAN_BYTES = 16 * 1024
_AUDIO_PLAN_KEYS = frozenset({
    "schema", "accountId", "serviceInstance", "hostGeneration", "connectionGeneration",
    "kernelPid", "kernelCreationTime100ns", "captureEndpointId", "renderEndpointId", "audio",
})
_AUDIO_PLAN_AUDIO_KEYS = frozenset({
    "enabled", "capture_device", "playback_device", "capture_mode", "device_scope",
})
_SAFE_SERVICE_ERROR_CODES = frozenset({
    "adapter_invalid", "adapter_mismatch", "adapter_config_invalid",
    "adapter_qqcall_config_invalid", "service_config_reference_invalid",
    "runtime_path_invalid", "runtime_must_be_private_project_path",
    "shared_config_invalid", "control_token_unavailable", "control_token_write_failed",
    "token_file_invalid", "token_file_unavailable", "token_file_too_large",
    "audio_config_invalid", "audio_mode_unsupported", "audio_scope_invalid",
    "audio_scope_unsupported", "audio_device_invalid", "audio_config_incomplete",
    "policy_invalid", "heartbeat_interval_invalid", "heartbeat_failed",
    "process_creation_time_unavailable", "process_creation_time_invalid",
    "process_open_failed",
})


@dataclass(frozen=True, slots=True)
class QQCallAdapterSettings:
    config: QQCallConfig
    platform_manifest_file: Path | None = None
    shared_config_file: Path | None = None
    error_code: str | None = None


def _safe_error_code(exc: BaseException, fallback: str) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _SAFE_SERVICE_ERROR_CODES:
        return code
    if isinstance(exc, ValueError) and str(exc) in _SAFE_SERVICE_ERROR_CODES:
        return str(exc)
    return fallback if fallback in _SAFE_SERVICE_ERROR_CODES else "shared_config_invalid"


def _fixed_validator() -> Any:
    """Load only the fixed validator shipped in this repository."""
    global _VALIDATOR
    if _VALIDATOR is not None:
        return _VALIDATOR
    with _VALIDATOR_LOCK:
        if _VALIDATOR is not None:
            return _VALIDATOR
        if not _VALIDATOR_PATH.is_file():
            raise RuntimeError("shared_validator_unavailable")
        spec = importlib.util.spec_from_file_location(_VALIDATOR_MODULE_NAME, _VALIDATOR_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("shared_validator_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VALIDATOR = module
        return module


def _fixed_private_files() -> Any:
    """Load private-file primitives only from the fixed QQCall installation."""
    global _PRIVATE_FILES
    if _PRIVATE_FILES is not None:
        return _PRIVATE_FILES
    with _VALIDATOR_LOCK:
        if _PRIVATE_FILES is not None:
            return _PRIVATE_FILES
        if not _PRIVATE_FILES_PATH.is_file():
            raise RuntimeError("private_files_unavailable")
        spec = importlib.util.spec_from_file_location(_PRIVATE_FILES_MODULE_NAME, _PRIVATE_FILES_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("private_files_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PRIVATE_FILES = module
        return module


def _has_reparse_attribute(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError("runtime_path_invalid") from None
    if stat.S_ISLNK(metadata.st_mode) or (getattr(metadata, "st_file_attributes", 0) & 0x400):
        return True
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("runtime_path_invalid")
    return False


def _validate_runtime_directory(runtime_dir: str | os.PathLike[str]) -> Path:
    """Require a real child runtime beneath the fixed private project root."""
    try:
        service_root = Path(os.path.abspath(_SERVICE_PROJECT_ROOT))
        configured = Path(runtime_dir)
        if not configured.is_absolute():
            raise ValueError("runtime_path_invalid")
        target = Path(os.path.abspath(configured))
        private_root = service_root / ".runtime"
        try:
            relative = target.relative_to(private_root)
        except ValueError:
            raise ValueError("runtime_must_be_private_project_path") from None
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("runtime_must_be_private_project_path")

        # The fixed project and every existing component below it must be an
        # ordinary directory. Windows junctions and other reparse points are
        # rejected along with POSIX symlinks before any ACL or file operation.
        if _has_reparse_attribute(service_root):
            raise ValueError("runtime_path_invalid")
        if not service_root.is_dir():
            raise ValueError("runtime_path_invalid")
        current = private_root
        components = [current]
        for part in relative.parts:
            current = current / part
            components.append(current)
        for component in components:
            if _has_reparse_attribute(component):
                raise ValueError("runtime_path_invalid")
        return target
    except ValueError:
        raise
    except (OSError, RuntimeError, TypeError):
        raise ValueError("runtime_path_invalid") from None


def _validate_manifest_path(path: Path, adapter: str) -> Path:
    target = Path(os.path.abspath(path))
    if target.name != f"platform-{adapter}.private.json":
        raise ValueError("runtime_path_invalid")
    _validate_runtime_directory(target.parent)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return target
    except OSError:
        raise ValueError("runtime_path_invalid") from None
    if stat.S_ISLNK(metadata.st_mode) or (getattr(metadata, "st_file_attributes", 0) & 0x400):
        raise ValueError("runtime_path_invalid")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("runtime_path_invalid")
    return target


def _validate_runtime_private_file(path: Path, expected_name: str) -> Path:
    target = Path(os.path.abspath(path))
    if target.name != expected_name:
        raise ValueError("runtime_path_invalid")
    runtime_dir = _validate_runtime_directory(target.parent)
    if target.parent != runtime_dir:
        raise ValueError("runtime_path_invalid")
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return target
    except OSError:
        raise ValueError("runtime_path_invalid") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or (getattr(metadata, "st_file_attributes", 0) & 0x400)
        or not stat.S_ISREG(metadata.st_mode)
        or getattr(metadata, "st_nlink", 1) != 1
    ):
        raise ValueError("runtime_path_invalid")
    return target


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("audio_plan_invalid")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("audio_plan_invalid")


def read_qqcall_audio_plan(
    peer_map_file: str | os.PathLike[str],
    *,
    account_id: str,
    service_meta: Mapping[str, Any],
    kernel_identity: tuple[int, int],
) -> dict[str, Any]:
    """Read a current-generation supervisor route plan from its private runtime sibling."""
    if (
        not isinstance(service_meta, Mapping)
        or not isinstance(kernel_identity, tuple)
        or len(kernel_identity) != 2
        or any(type(value) is not int or value <= 0 for value in kernel_identity)
    ):
        raise ValueError("audio_plan_invalid")
    try:
        peer_path = Path(os.path.abspath(peer_map_file))
        if peer_path.name != "adapter-runtime.private.json":
            raise ValueError("runtime_path_invalid")
        _validate_runtime_private_file(peer_path, "adapter-runtime.private.json")
        plan_path = _validate_runtime_private_file(
            peer_path.with_name("audio-plan.private.json"), "audio-plan.private.json"
        )
        with plan_path.open("rb") as stream:
            encoded = stream.read(_MAX_AUDIO_PLAN_BYTES + 1)
    except ValueError:
        raise
    except (OSError, TypeError):
        raise ValueError("audio_plan_unavailable") from None
    if len(encoded) > _MAX_AUDIO_PLAN_BYTES:
        raise ValueError("audio_plan_invalid")
    try:
        plan = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except ValueError:
        raise ValueError("audio_plan_invalid") from None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise ValueError("audio_plan_invalid") from None

    if not isinstance(plan, dict) or set(plan) != _AUDIO_PLAN_KEYS:
        raise ValueError("audio_plan_invalid")
    expected_meta_keys = ("serviceInstance", "hostGeneration", "connectionGeneration")
    if (
        type(plan.get("schema")) is not int
        or plan["schema"] != 1
        or plan.get("accountId") != account_id
        or not isinstance(account_id, str)
        or not isinstance(service_meta.get("serviceInstance"), str)
        or any(type(service_meta.get(key)) is not int or service_meta[key] <= 0 for key in expected_meta_keys[1:])
        or any(plan.get(key) != service_meta.get(key) for key in expected_meta_keys)
        or any(type(plan.get(key)) is not int or plan[key] <= 0 for key in expected_meta_keys[1:])
        or not isinstance(plan.get("serviceInstance"), str)
        or not plan["serviceInstance"]
    ):
        raise ValueError("audio_plan_stale")

    kernel_pid, kernel_creation = kernel_identity
    creation_text = plan.get("kernelCreationTime100ns")
    if (
        type(plan.get("kernelPid")) is not int
        or not 1 <= plan["kernelPid"] <= 2**31 - 1
        or not isinstance(creation_text, str)
        or not creation_text
        or any(character < "0" or character > "9" for character in creation_text)
        or not 1 <= len(creation_text) <= 20
        or not 1 <= int(creation_text) <= 2**64 - 1
        or (plan["kernelPid"], int(creation_text)) != (kernel_pid, kernel_creation)
    ):
        raise ValueError("audio_plan_stale")

    def valid_private_identifier(value: Any) -> bool:
        if not isinstance(value, str) or not value or len(value) >= 1024:
            return False
        try:
            value.encode("utf-16-le")
        except UnicodeEncodeError:
            return False
        return not any(ord(character) < 0x20 for character in value)

    capture_endpoint_id = plan.get("captureEndpointId")
    render_endpoint_id = plan.get("renderEndpointId")
    if (
        not valid_private_identifier(capture_endpoint_id)
        or not valid_private_identifier(render_endpoint_id)
        or capture_endpoint_id == render_endpoint_id
    ):
        raise ValueError("audio_plan_invalid")

    audio = plan.get("audio")
    if not isinstance(audio, dict) or set(audio) != _AUDIO_PLAN_AUDIO_KEYS:
        raise ValueError("audio_plan_invalid")
    scope = audio.get("device_scope")
    if (
        audio.get("enabled") is not True
        or audio.get("capture_mode") != "loopback"
        or not isinstance(scope, dict)
        or set(scope) != {"capture_render_name", "playback_render_name"}
    ):
        raise ValueError("audio_plan_invalid")

    def valid_device_name(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and value
            and len(value) <= 256
            and value == value.strip()
            and not any(ord(character) < 0x20 for character in value)
        )

    capture_device = audio.get("capture_device")
    playback_device = audio.get("playback_device")
    capture_render_name = scope.get("capture_render_name")
    playback_render_name = scope.get("playback_render_name")
    suffix = " [loopback]"
    if (
        not valid_device_name(capture_device)
        or not valid_device_name(playback_device)
        or not valid_device_name(capture_render_name)
        or not valid_device_name(playback_render_name)
        or not capture_device.casefold().endswith(suffix.casefold())
        or capture_device[:-len(suffix)].casefold() != capture_render_name.casefold()
        or playback_device.casefold() != playback_render_name.casefold()
        or capture_render_name.casefold() == playback_render_name.casefold()
    ):
        raise ValueError("audio_plan_invalid")

    return plan


def _validate_token_path(path: Path) -> Path:
    target = Path(os.path.abspath(path))
    if target.name != "control.token.private":
        raise ValueError("runtime_path_invalid")
    runtime_dir = _validate_runtime_directory(target.parent)
    if target.parent != runtime_dir:
        raise ValueError("runtime_path_invalid")
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return target
    except OSError:
        raise ValueError("token_file_invalid") from None
    if (stat.S_ISLNK(metadata.st_mode)
            or (getattr(metadata, "st_file_attributes", 0) & 0x400)
            or not stat.S_ISREG(metadata.st_mode)
            or getattr(metadata, "st_nlink", 1) != 1):
        raise ValueError("token_file_invalid")
    return target


def _configured_runtime_directory(
    public_path: Path,
    normalized_runtime_dir: str | os.PathLike[str],
) -> Path:
    """Reject paths whose original TOML spelling crossed a link or alias."""
    try:
        with public_path.open("rb") as stream:
            encoded = stream.read(_MAX_PUBLIC_CONFIG_BYTES + 1)
        if len(encoded) > _MAX_PUBLIC_CONFIG_BYTES:
            raise ValueError("runtime_path_invalid")
        source = tomllib.loads(encoded.decode("utf-8"))
        qqcall = source.get("qqcall") if isinstance(source, dict) else None
        raw_runtime = qqcall.get("runtime_dir") if isinstance(qqcall, dict) else None
        if not isinstance(raw_runtime, str) or not raw_runtime or "\x00" in raw_runtime:
            raise ValueError("runtime_path_invalid")
        lexical = Path(raw_runtime)
        if not lexical.is_absolute():
            lexical = public_path.parent / lexical
        lexical = Path(os.path.abspath(lexical))
        normalized = Path(os.path.abspath(normalized_runtime_dir))
        if os.path.normcase(str(lexical)) != os.path.normcase(str(normalized)):
            raise ValueError("runtime_path_invalid")
        return _validate_runtime_directory(normalized)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ValueError("runtime_path_invalid") from None
    except ValueError:
        raise
    except (OSError, TypeError, RuntimeError):
        raise ValueError("runtime_path_invalid") from None


def read_shared_qqcall_config(path: str | os.PathLike[str], adapter: str) -> dict[str, Any]:
    """Compatibility reader that uses the canonical plugin TOML."""
    del path
    if adapter not in {"napcat", "snowluma"}:
        raise ValueError("adapter_invalid")
    normalized = read_qqcall_public_config()
    if not isinstance(normalized, dict) or normalized.get("adapter") not in ("auto", adapter):
        raise ValueError("adapter_mismatch")
    return normalized


def canonical_qqcall_config_path() -> Path:
    """Return the one public QQCall configuration location."""
    return _SERVICE_PROJECT_ROOT.parent / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"


def read_qqcall_public_config() -> dict[str, Any]:
    """Read the canonical schema-5 TOML without creating runtime files."""
    config_path = canonical_qqcall_config_path().resolve(strict=False)
    normalized = _fixed_validator().load_config(config_path)
    _configured_runtime_directory(config_path, normalized.get("runtime_dir", ""))
    return normalized


def is_qqcall_enabled() -> bool:
    """Fresh-read the canonical root switch; all read/validation errors fail closed."""
    try:
        return read_qqcall_public_config().get("enabled") is True
    except Exception:
        return False


def _ensure_control_token(path: Path) -> None:
    """Create the shared local control token once, preserving existing bytes."""
    try:
        target = _validate_token_path(path)
        _fixed_private_files().read_or_create_private_token(target)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == "control_token_invalid":
            raise ValueError("token_file_invalid") from None
        if isinstance(exc, ValueError) and str(exc) == "token_file_invalid":
            raise
        if code in {"runtime_path_invalid", "runtime_must_be_private_project_path"}:
            raise ValueError(code) from None
        if code == "private_acl_unavailable":
            raise ValueError("control_token_unavailable") from None
        if isinstance(exc, ValueError) and str(exc) in {"runtime_path_invalid", "runtime_must_be_private_project_path"}:
            raise
        raise ValueError("control_token_unavailable") from None


def _runtime_audio(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("audio_config_invalid")
    enabled = raw.get("enabled")
    if type(enabled) is not bool:
        raise ValueError("audio_config_invalid")
    capture_mode = raw.get("capture_mode")
    if capture_mode != "loopback":
        raise ValueError("audio_mode_unsupported")
    scope = raw.get("device_scope")
    if not isinstance(scope, Mapping) or set(scope) != {"capture", "playback"}:
        raise ValueError("audio_scope_invalid")
    if scope.get("capture") != "virtual" or scope.get("playback") != "virtual":
        raise ValueError("audio_scope_unsupported")

    capture_device = raw.get("capture_device")
    playback_device = raw.get("playback_device")
    if not isinstance(capture_device, str) or not isinstance(playback_device, str):
        raise ValueError("audio_device_invalid")
    capture = capture_device.strip()
    playback = playback_device.strip()
    for suffix in (" [loopback]", " (loopback)"):
        if capture.casefold().endswith(suffix.casefold()):
            capture = capture[:-len(suffix)].rstrip()
            break
    return {
        "enabled": enabled,
        "capture_device": capture,
        "playback_device": playback,
        "capture_mode": "loopback",
        "device_scope": {"capture": "virtual", "playback": "virtual"},
    }


def _settings_from_public_config(
    raw: dict[str, Any],
    public_path: Path,
    adapter: str,
) -> QQCallAdapterSettings:
    try:
        runtime_dir = _configured_runtime_directory(public_path, raw["runtime_dir"])
        token_file = runtime_dir / "control.token.private"
        platform_manifest = runtime_dir / f"platform-{adapter}.private.json" if raw["enabled"] else None
        peer_manifest = runtime_dir / "adapter-runtime.private.json"
        audio = _runtime_audio(raw["audio"])
        # This is an internal runtime hint derived from the fixed shared
        # config paths; it is never parsed from the public TOML.
        audio["auto_resolve"] = bool(raw["enabled"] and audio["enabled"])
        peer_map = raw.get("peer_map")
        if not isinstance(peer_map, Mapping):
            raise ValueError("peer_map_invalid")
        auto_accept = raw["policy"]["auto_accept"]
        if type(auto_accept) is not bool:
            raise ValueError("policy_invalid")
        if raw["enabled"]:
            _ensure_control_token(token_file)
        config = QQCallConfig.from_mapping({
            "enabled": raw["enabled"],
            "endpoint": f"http://127.0.0.1:{raw['ports']['control']}",
            "token_file": str(token_file) if raw["enabled"] else "",
            "account_id": raw["account_id"],
            "peer_map": dict(peer_map),
            "audio": audio,
            "universal_api": dict(raw["universal_api"]),
            "peer_map_file": str(peer_manifest) if raw["enabled"] else "",
            "shared_config_file": str(public_path),
            "adapter_name": adapter,
            "auto_select_adapter": raw["adapter"] == "auto",
            "auto_accept": auto_accept,
            "platform_manifest_file": str(platform_manifest) if platform_manifest is not None else "",
        })
        return QQCallAdapterSettings(
            config=config,
            platform_manifest_file=platform_manifest,
            shared_config_file=public_path,
        )
    except Exception as exc:
        code = _safe_error_code(exc, "shared_config_invalid")
        _LOGGER.warning("QQCall shared configuration disabled code=%s", code)
        try:
            offline_config = QQCallConfig.from_mapping({
                "account_id": raw.get("account_id", ""),
                "adapter_name": adapter,
                "shared_config_file": str(public_path),
            })
        except Exception:
            offline_config = QQCallConfig()
        return QQCallAdapterSettings(
            config=offline_config,
            shared_config_file=public_path,
            error_code=code,
        )


def load_qqcall_adapter_settings(
    adapter: str,
    adapter_config_path: str | os.PathLike[str],
    legacy_config: QQCallConfig | Mapping[str, Any],
) -> QQCallAdapterSettings:
    """Build runtime settings from the fixed root config; legacy values are ignored."""
    if adapter not in {"napcat", "snowluma"}:
        return QQCallAdapterSettings(QQCallConfig(), error_code="adapter_invalid")
    try:
        # Keep the historical parameters for callers during migration. Their
        # contents are deliberately never parsed or converted into runtime state.
        del adapter_config_path, legacy_config
        public_path = canonical_qqcall_config_path().resolve(strict=False)
        normalized = read_qqcall_public_config()
        if normalized.get("adapter") not in ("auto", adapter):
            raise ValueError("adapter_mismatch")
        return _settings_from_public_config(normalized, public_path, adapter)
    except Exception as exc:
        code = _safe_error_code(exc, "shared_config_invalid")
        _LOGGER.warning("QQCall shared configuration disabled code=%s", code)
        return QQCallAdapterSettings(QQCallConfig(), error_code=code)


def _windows_process_creation_time_100ns(pid: int) -> int:
    """Return the exact Windows FILETIME creation timestamp for a process."""
    if os.name != "nt":
        raise OSError("process_creation_time_unavailable")

    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise OSError("process_open_failed")
    try:
        creation = FILETIME()
        exit_time = FILETIME()
        kernel_time = FILETIME()
        user_time = FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise OSError("process_creation_time_unavailable")
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


class QQCallPlatformBindingHeartbeat:
    """Publish the verified ordinary OneBot identity for the supervisor."""

    def __init__(
        self,
        *,
        adapter: str,
        manifest_file: str | os.PathLike[str],
        account_id: str,
        interval_sec: float = 0.5,
        pid: int | None = None,
        creation_time_provider: Callable[[int], int] | None = None,
    ) -> None:
        if adapter not in {"napcat", "snowluma"}:
            raise ValueError("adapter_invalid")
        if isinstance(interval_sec, bool) or not isinstance(interval_sec, (int, float)) or not 0.1 <= interval_sec <= 5:
            raise ValueError("heartbeat_interval_invalid")
        self.adapter = adapter
        self.manifest_file = _validate_manifest_path(Path(manifest_file), adapter)
        self.account_id = account_id
        self.interval_sec = float(interval_sec)
        self.pid = os.getpid() if pid is None else pid
        self._creation_time_provider = creation_time_provider or _windows_process_creation_time_100ns
        self._creation_time: int | None = None
        self._stop = asyncio.Event()
        self._last_identity: tuple[Any, ...] | None = None
        self._last_generation: str | int = ""
        self._publish_lock = asyncio.Lock()
        self._writer: ThreadPoolExecutor | None = None
        self._writer_closed = False
        self._stopping = False
        self._finalizer_task: asyncio.Task[None] | None = None

    def _ensure_creation_time(self) -> int:
        if self._creation_time is None:
            value = self._creation_time_provider(self.pid)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise OSError("process_creation_time_invalid")
            self._creation_time = value
        return self._creation_time

    def _write(self, *, connected: bool, account_id: str, generation: str | int) -> None:
        payload = {
            "schema": 1,
            "adapter": self.adapter,
            "accountId": account_id if connected else "",
            "connected": connected,
            "adapterPid": self.pid,
            # FILETIME values exceed JavaScript's exact integer range. Keep
            # their decimal representation lossless across the Node bridge.
            "adapterCreationTime100ns": str(self._ensure_creation_time()),
            "connectionGeneration": generation,
            "updatedAtMs": time.time_ns() // 1_000_000,
        }
        path = _validate_manifest_path(self.manifest_file, self.adapter)
        _fixed_private_files().atomic_private_json(path, payload)

    def _ensure_writer(self) -> ThreadPoolExecutor:
        if self._writer_closed:
            raise RuntimeError("heartbeat_writer_closed")
        if self._writer is None:
            self._writer = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="qqcall-platform-heartbeat",
            )
        return self._writer

    @staticmethod
    async def _await_uncancellable(awaitable: Awaitable[Any]) -> Any:
        """Drain one owned operation before propagating caller cancellation."""
        task = asyncio.ensure_future(awaitable)
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _write_async(self, *, connected: bool, account_id: str, generation: str | int) -> None:
        writer = self._ensure_writer()
        loop = asyncio.get_running_loop()
        operation = loop.run_in_executor(
            writer,
            partial(self._write, connected=connected, account_id=account_id, generation=generation),
        )
        await self._await_uncancellable(operation)

    def _ensure_finalizer(self) -> asyncio.Task[None]:
        if self._finalizer_task is None:
            self._stopping = True
            self._stop.set()
            self._finalizer_task = asyncio.create_task(
                self._finalize_writer(),
                name="qqcall-platform-heartbeat-finalizer",
            )
        return self._finalizer_task

    async def _finalize_writer(self) -> None:
        async with self._publish_lock:
            if self._writer_closed:
                return
            try:
                await self._write_async(
                    connected=False,
                    account_id="",
                    generation=self._last_generation,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "QQCall offline heartbeat unavailable code=%s",
                    _safe_error_code(exc, "heartbeat_failed"),
                )
            finally:
                writer, self._writer = self._writer, None
                self._writer_closed = True
                if writer is not None:
                    # The final write has completed before this join, so this
                    # only reaps the one owned idle worker thread.
                    writer.shutdown(wait=True, cancel_futures=True)

    async def publish_state(self, state: Mapping[str, Any] | None) -> None:
        generation: Any = 0
        identity: tuple[Any, ...] = ()
        connected = False
        account_id = ""
        if isinstance(state, Mapping):
            raw_generation = state.get("connection_generation")
            if isinstance(raw_generation, (str, int)) and not isinstance(raw_generation, bool):
                generation = raw_generation
            raw_identity = state.get("identity")
            if isinstance(raw_identity, (tuple, list)):
                identity = tuple(raw_identity)
            raw_account = state.get("account_id")
            connected = (
                state.get("connected") is True
                and isinstance(raw_account, str)
                and raw_account == self.account_id
                and bool(identity)
            )
            account_id = raw_account if connected else ""

        async with self._publish_lock:
            if self._stopping or self._writer_closed:
                return
            if self._last_identity is not None and identity != self._last_identity:
                # Quarantine the previous socket identity before publishing a
                # replacement, even when the replacement reports the same account.
                await self._write_async(connected=False, account_id="", generation=generation)
                self._last_generation = generation
                if self._stopping:
                    return
            await self._write_async(connected=connected, account_id=account_id, generation=generation)
            self._last_identity = identity
            self._last_generation = generation

    async def run(
        self,
        get_state: Callable[[], Mapping[str, Any] | Awaitable[Mapping[str, Any]]],
    ) -> None:
        try:
            while not self._stop.is_set():
                try:
                    value = get_state()
                    state = await value if isinstance(value, Awaitable) else value
                    await self.publish_state(state if isinstance(state, Mapping) else None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOGGER.warning("QQCall platform heartbeat unavailable code=%s", _safe_error_code(exc, "heartbeat_failed"))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_sec)
                except TimeoutError:
                    pass
        finally:
            await self._await_uncancellable(self._ensure_finalizer())

    async def stop(self) -> None:
        self._stop.set()
        self._stopping = True
        await self._await_uncancellable(self._ensure_finalizer())


__all__ = [
    "QQCallAdapterSettings",
    "QQCallPlatformBindingHeartbeat",
    "canonical_qqcall_config_path",
    "is_qqcall_enabled",
    "read_qqcall_audio_plan",
    "load_qqcall_adapter_settings",
    "read_qqcall_public_config",
    "read_shared_qqcall_config",
]
