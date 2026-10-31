"""Windows supervisor for the independent QQCall Host and Bridge processes.

`plan` is read-only. `run` is the only command that launches the dedicated Host
and the official Bridge CLI. It never starts or restarts the ordinary adapter
or a user's QQ process and never invokes the admin attach helper.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import ntpath
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.windows_routes import EndpointInventoryError, enumerate_active_endpoints

from service.audio_routes import (
    AudioRouteError,
    audio_route_identity,
    resolve_audio_route,
)
from service.config import ConfigError, load_config
from service.private_files import (
    PrivateFileError,
    ProcessLock,
    atomic_private_json,
    atomic_private_json_if_absent,
    is_reparse_point,
    read_or_create_private_token,
    read_bounded_bytes,
    read_private_json,
    secure_directory,
)
from service.rpc import OneBotClient, OneBotError
from service.snowluma import safe_auth_failure_stage
from service.snowluma_identity import SnowLumaIdentityError, SnowLumaOneBotClient
from service.universal_service import UniversalServiceSupervisor
from service.status_history import StatusHistory
from service.exception_diagnostics import validated_exception_details
from service.process_job import ProcessJobError, ensure_process_job
from service.windows import (
    HostLaunch,
    WindowsServiceError,
    WindowsSystem,
)

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "NachoBot"
    / "src"
    / "plugins"
    / "built_in"
    / "qqcall_plugin"
    / "config.toml"
)

# Keep an import-time type reference for policy checks even in tests that
# replace the constructor symbol with a factory.
_SNOWLUMA_ONEBOT_CLIENT_TYPE = SnowLumaOneBotClient

SCHEMA = 1
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_DIAGNOSTIC_INTEGER = 2_147_483_647
MAX_FILETIME = (1 << 64) - 1
_UIN = re.compile(r"^[1-9][0-9]{0,19}$")
_UID = re.compile(r"^u_[A-Za-z0-9_-]{22}$")
_SAFE_GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTROL_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_MAX_DISCOVERED_FRIENDS = 64
_MAX_CONCURRENT_PEER_IDENTITY_RPCS = 4
_MAX_CONCURRENT_ONEBOT_HTTP_EXCHANGES = 4
_IDENTITY_DISCOVERY_TIMEOUT_SECONDS = 15.0
_ATTACH_STATUS_MAX_AGE_MS = 60_000
_PEER_CACHE_ENTRIES = 8
# Only transport/process absence is recoverable. Identity, malformed input and
# target ambiguity must still tear down the generation and fail closed.
_ADAPTER_OUTAGE_CODES = frozenset({
    "adapter_not_connected", "adapter_manifest_stale", "adapter_process_unavailable", "adapter_listener_unavailable",
    "onebot_unavailable", "onebot_timeout", "onebot_offline", "onebot_websocket_closed",
    "target_binding_changed",
})
_GUARD_FIELDS = {
    "version", "qqBuild", "hostGeneration", "nodeArtifact", "nodeSha256",
    "guardDllPath", "dllSha256", "captureEndpointId", "renderEndpointId",
}
_RECOVERY_SHIM_SHA256 = "147BD253F08BCFF910DE4D63FAB94DB3B2501413C613A543E61F994DABF5ED93"
_HOST_RUNTIME_FAILURE_CODES = frozenset({
    "output_callback_not_ready", "output_forward_failed", "host_disconnected", "host_request_failed",
    "host_request_timeout", "host_response_aborted", "host_response_too_large", "host_not_ready",
    "invalid_host_response", "host_stopped", "renderer_crashed", "renderer_unresponsive", "renderer_closed",
    "plugin_post_failed", "invoke_timeout", "host_failed", "host_runtime_unavailable",
})
_HOST_BUSINESS_OPERATIONS = frozenset({
    "group_room_query", "group_start", "group_quit", "audio_device_selection", "invoke",
})
_HOST_FAILURE_STAGES = frozenset({"host_health", "host_availability", "host_terminal_report", "host_transport"})
_AUTO_BOOTSTRAP_FAILURE_CODES = frozenset({
    "account_path_unavailable", "auto_bootstrap_failed", "auto_login_account_path_mismatch",
    "auto_login_account_path_unavailable", "auto_login_generation_mismatch", "bootstrap_evidence_stale",
    "configured_account_mismatch", "identity_file_changed", "identity_file_unavailable",
    "identity_output_exists", "identity_output_matches_input", "identity_output_write_failed",
    "invalid_identity_file", "invalid_onebot_identity_file", "kernel_peer_identities_binding_mismatch",
    "kernel_peer_identities_unavailable", "listener_attach_unconfirmed", "login_ack_timeout",
    "login_already_pending", "login_post_receipt_invalid", "native_login_failed",
})
_CONTROLLER_TERMINATION_TRIGGERS = frozenset({
    "explicit_stop", "controller_exit", "evaluation_changed", "binding_changed",
    "bootstrap_failed", "service_error", "service_runtime_unavailable",
    "supervisor_finally", "generation_start_failed", "supervisor_stop_active",
})
_CONTROLLER_EXIT_REPORT_FIELDS = frozenset({
    "schema", "errorClass", "failureCode", "origin", "observedAtMs", "exitedAtMs", "exitCode",
})
_CONTROLLER_EXIT_ORIGINS = frozenset({"uncaughtException", "unhandledRejection"})
_PEER_IDENTITY_TIMEOUT_STAGES = frozenset({
    "cached_binding_fence_busy",
    "peer_discovery_fence_busy",
    "onebot_login_rpc_wait_timeout",
    "onebot_self_rpc_wait_timeout",
    "identity_deadline_exhausted",
    "peer_discovery_deadline_exhausted",
    "peer_discovery_rpc_wait_timeout",
    "peer_identity_rpc_slot_timeout",
    "peer_identity_rpc_wait_timeout",
    "final_binding_fence_busy",
    "final_binding_fence_wait_timeout",
    "final_binding_deadline_exhausted",
})
_ONEBOT_TIMEOUT_STAGES = frozenset({
    "onebot_status_rpc_wait_timeout",
    "onebot_login_rpc_wait_timeout",
    "onebot_self_rpc_wait_timeout",
})
_BINDING_RECHECK_STAGES = frozenset({
    "binding_manifest_unavailable", "binding_manifest_changed",
    "binding_target_changed", "binding_final_manifest_unavailable",
    "binding_final_manifest_changed", "binding_recheck_exception",
})
_PHASES = {
    "disabled", "starting", "waiting_adapter", "waiting_login", "waiting_identity", "waiting_target",
    "waiting_kernel_restart", "needs_attachment", "ready", "quarantined", "stopping", "stopped", "error",
}
_PHASE_LABELS = {
    "disabled": "服务未启用", "starting": "正在启动",
    "waiting_adapter": "等待适配器连接或恢复", "waiting_login": "等待机器人 QQ 登录",
    "waiting_identity": "等待身份验证", "waiting_target": "等待可绑定的 QQ 进程",
    "waiting_kernel_restart": "请重启机器人 QQ 后重新绑定",
    "needs_attachment": "等待管理员注入", "ready": "通话控制链已就绪",
    "quarantined": "绑定校验未通过", "stopping": "正在停止",
    "stopped": "服务已停止", "error": "服务异常",
}


class ServiceError(RuntimeError):
    def __init__(self, code: str, phase: str = "error", *, diagnostic_stage: Any = None):
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", code) else "service_error"
        self.phase = phase if phase in _PHASES else "error"
        self._diagnostic_stage = _safe_controller_failure_stage(self.code, diagnostic_stage)
        super().__init__(self.code)


def _safe_peer_identity_timeout_stage(value: Any) -> str | None:
    if type(value) is str and value in _PEER_IDENTITY_TIMEOUT_STAGES:
        return value
    return None


def _safe_controller_failure_stage(failure_code: Any, value: Any) -> str | None:
    if type(failure_code) is not str:
        return None
    if failure_code in _HOST_RUNTIME_FAILURE_CODES and type(value) is str and value in _HOST_FAILURE_STAGES:
        return value
    if failure_code == "peer_identity_timeout":
        return _safe_peer_identity_timeout_stage(value)
    if failure_code == "onebot_timeout" and type(value) is str and value in _ONEBOT_TIMEOUT_STAGES:
        return value
    if failure_code == "adapter_auth_unavailable":
        return safe_auth_failure_stage(failure_code, value)
    if failure_code == "adapter_binding_changed" and type(value) is str and value in _BINDING_RECHECK_STAGES:
        return value
    return None


def _safe_diagnostic_code(value: Any) -> str | None:
    if type(value) is str and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value):
        return value
    return None


def _safe_diagnostic_timestamp(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= MAX_SAFE_INTEGER:
        return value
    return None


def _status_supervisor_is_current(system: Any, value: dict[str, Any]) -> bool:
    """Confirm a ready status belongs to the live supervisor process instance."""
    pid = value.get("supervisorPid")
    creation_text = value.get("supervisorCreationTime100ns")
    if type(pid) is not int or not 1 <= pid <= 0x7FFFFFFF:
        return False
    if type(creation_text) is not str or re.fullmatch(r"[1-9][0-9]{0,19}", creation_text) is None:
        return False
    is_current = getattr(system, "process_is_current", None)
    if not callable(is_current):
        return False
    try:
        # This checks the PID and its creation time together, so a reused PID
        # cannot inherit the old supervisor's ready status.
        return is_current(pid, int(creation_text)) is True
    except Exception:
        # Status is diagnostic. Any identity-query failure must fail closed.
        return False


@dataclass(frozen=True, slots=True)
class Evaluation:
    manifest: dict[str, Any]
    binding: Any
    identity: dict[str, str]
    identity_source: dict[str, str]
    full_identity_path: str | None
    peer_map: dict[str, str]
    guard_source: dict[str, Any] | None

    @property
    def kernel_pid(self) -> int:
        return int(_get(self.binding, "kernel_pid", "kernelPid"))

    @property
    def kernel_filetime(self) -> int:
        return int(_get(self.binding, "kernel_creation_time_100ns", "kernelCreationTime100ns"))

    @property
    def adapter_pid(self) -> int:
        return int(_get(self.binding, "adapter_pid", "adapterPid"))

    @property
    def adapter_filetime(self) -> int:
        return int(_get(self.binding, "adapter_creation_time_100ns", "adapterCreationTime100ns"))

    @property
    def app_path(self) -> str:
        return str(_get(self.binding, "app_path", "appPath"))

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.manifest["adapter"], self.manifest["accountId"], self.manifest["adapterPid"],
            self.manifest["adapterCreationTime100ns"], self.manifest["connectionGeneration"],
            self.kernel_pid, self.kernel_filetime, self.app_path,
            audio_route_identity(self.guard_source),
        )

    @property
    def native_fingerprint(self) -> tuple[Any, ...]:
        return (
            self.manifest["adapter"], self.manifest["accountId"],
            self.kernel_pid, self.kernel_filetime, ntpath.normcase(ntpath.normpath(self.app_path)),
            self.identity, self.identity_source, self.full_identity_path,
            self.peer_map, audio_route_identity(self.guard_source),
        )


@dataclass(frozen=True, slots=True)
class PeerResolution:
    peer_map: dict[str, str]
    cache_key: tuple[Any, ...] | None = None
    deadline: float | None = None
    pending_cache: bool = False


@dataclass(slots=True)
class ActiveGeneration:
    evaluation: Evaluation
    host_generation: int
    connection_generation: int
    directory: Path
    launch: HostLaunch
    owned_hosts: list[dict[str, Any]]
    host_pid: int
    host_filetime: str
    sidecar_path: str
    bridge: BridgeProcess
    service_instance: str | None = None
    shim_consumed: bool = False
    account_path_verified: bool = False
    login_ready: bool = False
    peer_snapshot_written: bool = False
    last_event: str = ""
    bootstrap_failure_code: str = ""
    bridge_startup_code: str = ""
    host_runtime_failure_code: str = ""
    host_runtime_failure_stage: str | None = None
    host_runtime_ready: bool = False
    first_rejected_output: dict[str, Any] | None = None
    adapter_suspended: bool = False


class BridgeProcess:
    """Drain CLI pipes without retaining raw status or error lines on disk."""

    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=512)
        self.last_failure_code: str | None = None
        self._stdout_eof = threading.Event()
        self._overflow_quit_ack_lock = threading.Lock()
        self._overflow_quit_ack: dict[str, Any] | None = None
        self._reader = threading.Thread(target=self._read_stdout, name="qqcall-bridge-output", daemon=True)
        self._stderr = threading.Thread(target=self._discard_stderr, name="qqcall-bridge-errors", daemon=True)
        self._reader.start()
        self._stderr.start()

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        if stream is None:
            self._stdout_eof.set()
            return
        try:
            for line in stream:
                if len(line.encode("utf-8", errors="replace")) > 64 * 1024:
                    item = {"kind": "service_output_error", "code": "bridge_output_too_large"}
                else:
                    try:
                        value = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                        item = value if isinstance(value, dict) else {"kind": "service_output_error", "code": "bridge_output_invalid"}
                    except (ValueError, TypeError, json.JSONDecodeError):
                        item = {"kind": "service_output_error", "code": "bridge_output_invalid"}
                try:
                    self.events.put_nowait(item)
                except queue.Full:
                    if self._is_confirmed_quit_ack(item):
                        # Preserve only the one bounded fact needed by stop();
                        # the ordinary event queue's overflow policy is unchanged.
                        with self._overflow_quit_ack_lock:
                            if self._overflow_quit_ack is None:
                                self._overflow_quit_ack = item
                        continue
                    try:
                        self.events.get_nowait()
                        self.events.put_nowait({"kind": "service_output_error", "code": "bridge_event_overflow"})
                    except queue.Empty:
                        pass
        except (OSError, UnicodeError):
            try:
                self.events.put_nowait({"kind": "service_output_error", "code": "bridge_output_unavailable"})
            except queue.Full:
                pass
        finally:
            self._stdout_eof.set()

    @staticmethod
    def _is_confirmed_quit_ack(event: Any) -> bool:
        if not isinstance(event, dict) or event.get("command") != "quit":
            return False
        result = event.get("result")
        return isinstance(result, dict) and result.get("detachedConfirmed") is True

    def _take_overflow_quit_ack(self) -> dict[str, Any] | None:
        with self._overflow_quit_ack_lock:
            event = self._overflow_quit_ack
            self._overflow_quit_ack = None
            return event

    def _discard_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        buffer = ""
        dropping = False
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    if buffer and not dropping:
                        self._read_safe_failure_line(buffer)
                    return
                for character in chunk:
                    if character == "\n":
                        if not dropping:
                            self._read_safe_failure_line(buffer.rstrip("\r"))
                        buffer = ""
                        dropping = False
                    elif not dropping:
                        buffer += character
                        if len(buffer) > 4096:
                            buffer = ""
                            dropping = True
        except OSError:
            return

    def _read_safe_failure_line(self, line: str) -> None:
        prefix = "QQCall Bridge CLI failed: "
        if line.startswith(prefix):
            code = line[len(prefix):].strip()
            if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", code):
                if getattr(self, "last_failure_code", None) is None:
                    self.last_failure_code = code
                try:
                    self.events.put_nowait({"kind": "bridge_startup_error", "code": code})
                except queue.Full:
                    pass
            return
        try:
            value = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (ValueError, TypeError, json.JSONDecodeError):
            return
        if not isinstance(value, dict) or set(value) != {"kind", "code"} or value.get("kind") != "auto_bootstrap_failed":
            return
        code = value.get("code")
        if type(code) is not str:
            return
        safe_code = code if code in _AUTO_BOOTSTRAP_FAILURE_CODES else "auto_bootstrap_failed"
        if getattr(self, "last_failure_code", None) is None:
            self.last_failure_code = safe_code
        try:
            self.events.put_nowait({"kind": "auto_bootstrap_failed", "code": safe_code})
        except queue.Full:
            return

    def send(self, command: str) -> bool:
        if command not in ("status", "quit", "adapter-pause", "adapter-resume") or self.process.stdin is None or self.process.poll() is not None:
            return False
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def poll_event(self) -> dict[str, Any] | None:
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None

    def wait_event(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self, timeout: float = 10.0) -> bool:
        if self.process.poll() is not None:
            return False
        if not self.send("quit"):
            return False
        deadline = time.monotonic() + timeout
        detached_confirmed = False
        while time.monotonic() < deadline:
            event = self.poll_event()
            if event is None:
                event = self._take_overflow_quit_ack()
            if event is None:
                event = self.wait_event(min(0.25, max(0.0, deadline - time.monotonic())))
            if event and event.get("command") == "quit":
                result = event.get("result")
                detached_confirmed = isinstance(result, dict) and result.get("detachedConfirmed") is True
                break
            if self.process.poll() is not None:
                # poll() can beat the stdout reader to a final line already
                # written by the child. Trust exit only after the reader has
                # observed EOF and all of its bounded event queue is drained.
                if self._stdout_eof.is_set() and self.events.empty():
                    overflow_ack = self._take_overflow_quit_ack()
                    if overflow_ack is not None:
                        result = overflow_ack.get("result")
                        detached_confirmed = isinstance(result, dict) and result.get("detachedConfirmed") is True
                    break
        try:
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            # This is our child Node controller, never a QQ/adapter process.
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2.0)
        return detached_confirmed


def _get(value: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(value, dict) and key in value:
            return value[key]
        if hasattr(value, key):
            return getattr(value, key)
    raise ServiceError("target_binding_invalid", "waiting_target")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite")


def _json_file(path: Path, maximum: int) -> Any:
    try:
        raw = read_bounded_bytes(path, maximum)
    except OSError:
        raise ServiceError("file_unavailable", "waiting_identity") from None
    if len(raw) > maximum:
        raise ServiceError("file_too_large", "waiting_identity")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise ServiceError("file_invalid", "waiting_identity") from None


def _canonical_uin(value: Any) -> str | None:
    if type(value) is str and _UIN.fullmatch(value):
        return value
    if type(value) is int and value > 0:
        result = str(value)
        return result if len(result) <= 20 and _UIN.fullmatch(result) else None
    return None


def _uid_from(data: dict[str, Any]) -> str | None:
    for key in ("uid", "user_uid", "userUid", "peer_uid"):
        value = data.get(key)
        if type(value) is str and _UID.fullmatch(value):
            return value
    return None


def _response_uin(data: dict[str, Any]) -> str | None:
    for key in ("user_id", "self_id", "account_id"):
        result = _canonical_uin(data.get(key))
        if result:
            return result
    return None


def _strict_identity_uin(data: dict[str, Any]) -> str | None:
    present = [key for key in ("user_id", "self_id", "account_id") if key in data]
    if not present:
        return None
    values = [_canonical_uin(data[key]) for key in present]
    if any(value is None for value in values):
        return None
    if len(set(values)) != 1:
        raise ServiceError("peer_identity_conflict", "quarantined")
    return values[0]


def _strict_identity_uid(data: dict[str, Any]) -> str | None:
    present = [key for key in ("uid", "user_uid", "userUid", "peer_uid") if key in data]
    if not present:
        return None
    values = [data[key] for key in present]
    if any(type(value) is not str or _UID.fullmatch(value) is None for value in values):
        return None
    if len(set(values)) != 1:
        raise ServiceError("peer_identity_conflict", "quarantined")
    return values[0]


def _connection_generation(value: Any, *, connected: bool) -> bool:
    if type(value) is int:
        return 0 <= value <= MAX_SAFE_INTEGER if not connected else 0 < value <= MAX_SAFE_INTEGER
    if type(value) is str:
        if not connected and value == "":
            return True
        return bool(_SAFE_GENERATION.fullmatch(value))
    return False


def _validate_manifest(value: Any, adapter: str) -> dict[str, Any]:
    expected = {
        "schema", "adapter", "accountId", "connected", "adapterPid",
        "adapterCreationTime100ns", "connectionGeneration", "updatedAtMs",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ServiceError("adapter_manifest_invalid", "quarantined")
    if type(value["schema"]) is not int or value["schema"] != SCHEMA or value["adapter"] != adapter or type(value["connected"]) is not bool:
        raise ServiceError("adapter_manifest_invalid", "quarantined")
    account = value["accountId"]
    if type(account) is not str or len(account) > 20 or (account and _UIN.fullmatch(account) is None):
        raise ServiceError("adapter_manifest_invalid", "quarantined")
    pid = value["adapterPid"]
    filetime = value["adapterCreationTime100ns"]
    updated = value["updatedAtMs"]
    if type(updated) is not int or not 0 <= updated <= MAX_SAFE_INTEGER:
        raise ServiceError("adapter_manifest_invalid", "quarantined")
    if value["connected"]:
        if type(pid) is not int or not 0 < pid <= 0x7FFFFFFF or type(filetime) is not str or not re.fullmatch(r"[1-9][0-9]{0,19}", filetime):
            raise ServiceError("adapter_manifest_invalid", "quarantined")
        parsed_filetime = int(filetime)
        if parsed_filetime > MAX_FILETIME or not account or _connection_generation(value["connectionGeneration"], connected=True) is False:
            raise ServiceError("adapter_manifest_invalid", "quarantined")
    else:
        if (type(pid) is not int or pid < 0 or pid > 0x7FFFFFFF or type(filetime) is not str or
                (filetime and (not filetime.isdecimal() or len(filetime) > 20)) or
                (filetime and int(filetime) > MAX_FILETIME) or not _connection_generation(value["connectionGeneration"], connected=False)):
            raise ServiceError("adapter_manifest_invalid", "quarantined")
        if account:
            raise ServiceError("adapter_manifest_invalid", "quarantined")
    return value


def _manifest_fingerprint(value: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(value[key] for key in (
        "adapter", "accountId", "connected", "adapterPid", "adapterCreationTime100ns", "connectionGeneration",
    ))


def _read_manifest(config: dict[str, Any]) -> dict[str, Any] | None:
    path = Path(config["runtime_dir"]) / f"platform-{config['adapter']}.private.json"
    if is_reparse_point(path):
        raise ServiceError("adapter_manifest_invalid", "quarantined")
    if not path.exists():
        return None
    try:
        value = _json_file(path, 16 * 1024)
    except ServiceError as exc:
        if exc.code == "file_unavailable":
            return None
        raise ServiceError("adapter_manifest_invalid", "quarantined") from None
    return _validate_manifest(value, config["adapter"])


def _read_token_file(path: str) -> str | None:
    if not path:
        return None
    value = _json_file(Path(path), 4096)
    if isinstance(value, dict):
        if set(value) not in ({"token"}, {"access_token"}):
            raise ServiceError("onebot_token_invalid", "waiting_login")
        value = next(iter(value.values()))
    if type(value) is not str or not value or len(value) > 4096 or any(ch in value for ch in "\r\n\x00"):
        raise ServiceError("onebot_token_invalid", "waiting_login")
    return value


def _read_identity_file(path: str, expected_account: str) -> dict[str, str]:
    value = _json_file(Path(path), 64 * 1024)
    fields = {"selfUid", "selfUin", "accountPath"}
    if not isinstance(value, dict) or not fields <= set(value):
        raise ServiceError("identity_file_invalid", "waiting_identity")
    uid, uin, account_path = value.get("selfUid"), value.get("selfUin"), value.get("accountPath")
    if (type(uid) is not str or not uid or len(uid) > 512 or any(ord(ch) < 0x20 for ch in uid) or
            type(uin) is not str or _UIN.fullmatch(uin) is None or uin != expected_account or
            type(account_path) is not str or len(account_path) > 32768 or not ntpath.isabs(account_path) or "\x00" in account_path):
        raise ServiceError("identity_file_invalid", "waiting_identity")
    return {"selfUid": uid, "selfUin": uin, "accountPath": account_path}


def _status_online(value: dict[str, Any]) -> bool:
    online = value.get("online")
    good = value.get("good")
    if "online" in value and type(online) is not bool:
        return False
    if "good" in value and type(good) is not bool:
        return False
    if online is False or good is False:
        return False
    return online is True or ("online" not in value and good is True)


def _snowluma_status_online(value: dict[str, Any]) -> bool:
    """Use SnowLuma's account-online field; ``good`` only describes path health."""
    online = value.get("online")
    good = value.get("good")
    if type(online) is not bool or ("good" in value and type(good) is not bool):
        return False
    return online is True


def _safe_status(
    *, enabled: bool, adapter: str, phase: str, code: str, updated_at_ms: int,
    supervisor_pid: int = 0, supervisor_creation_time_100ns: str = "0",
    universal_service: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if phase not in _PHASES:
        phase = "error"
    safe_code = code if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", code or "") else "service_error"
    result = {
        "schema": SCHEMA,
        "phase": phase,
        "ready": phase == "ready" and not code,
        "updatedAtMs": max(0, min(int(updated_at_ms), MAX_SAFE_INTEGER)),
        "supervisorPid": supervisor_pid if type(supervisor_pid) is int and 0 <= supervisor_pid <= 0x7FFFFFFF else 0,
        "supervisorCreationTime100ns": supervisor_creation_time_100ns if type(supervisor_creation_time_100ns) is str and re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", supervisor_creation_time_100ns) else "0",
    }
    if code:
        result["code"] = safe_code
    component = universal_service if isinstance(universal_service, dict) else {}
    state = component.get("state")
    component_code = component.get("code")
    reused = component.get("reused")
    safe_component_codes = {
        "universal_api_service_ready", "universal_api_service_unavailable",
        "universal_api_auth_failed", "universal_api_health_invalid",
        "universal_api_route_mismatch", "universal_api_url_unsupported",
        "universal_api_https_autostart_unsupported", "universal_api_token_invalid",
        "universal_api_token_mismatch", "universal_api_service_config_invalid",
        "universal_api_service_cli_unavailable", "universal_api_start_lock_unavailable",
        "universal_api_start_in_progress_timeout", "universal_api_start_process_exited",
        "universal_api_start_timeout", "universal_api_start_cancelled",
        "universal_api_process_cleanup_unconfirmed",
    }
    if state not in ("ready", "unavailable", "not_checked"):
        state, component_code, reused = "not_checked", "", False
    if type(component_code) is not str or (component_code and component_code not in safe_component_codes):
        state, component_code, reused = "unavailable", "universal_api_service_unavailable", False
    if type(reused) is not bool:
        reused = False
    if state == "ready" and component_code != "universal_api_service_ready":
        state, component_code, reused = "unavailable", "universal_api_service_unavailable", False
    if state == "not_checked":
        component_code = ""
    if state == "unavailable" and not component_code:
        component_code = "universal_api_service_unavailable"
    result["universalService"] = {"state": state, "code": component_code, "reused": reused}
    _ = enabled, adapter
    return result


def _filetime_utc(filetime: int) -> str:
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    return (epoch + timedelta(microseconds=filetime // 10)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_file(path: Path, max_bytes: int = 32 * 1024 * 1024) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 1 or path.stat().st_size > max_bytes:
            raise ServiceError("audio_guard_artifact_invalid", "quarantined")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except ServiceError:
        raise
    except OSError:
        raise ServiceError("audio_guard_artifact_unavailable", "quarantined") from None


def _validate_guard(
    config: dict[str, Any],
    endpoint_inventory: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    if not config["audio"]["enabled"]:
        return None
    path_text = config["native"]["guard_config_file"]
    if not path_text:
        raise ServiceError("audio_guard_config_missing", "quarantined")
    if config.get("audio_guard_config_file_defaulted") and not Path(path_text).exists():
        raise ServiceError("audio_guard_config_missing", "quarantined")
    try:
        value = _json_file(Path(path_text), 16 * 1024)
    except ServiceError:
        raise ServiceError("audio_guard_config_unavailable", "quarantined") from None
    if not isinstance(value, dict) or set(value) != _GUARD_FIELDS:
        raise ServiceError("audio_guard_config_invalid", "quarantined")
    if (type(value.get("version")) is not int or value["version"] != 1 or value.get("qqBuild") != "9.9.26-44343" or
            type(value.get("hostGeneration")) is not int or value["hostGeneration"] <= 0):
        raise ServiceError("audio_guard_config_invalid", "quarantined")
    for field, expected_name in (("nodeArtifact", "QQCallAudioRouteGuard-v2.node"), ("guardDllPath", "QQCallAudioRouteGuard-v2.dll")):
        raw = value.get(field)
        if type(raw) is not str or len(raw) > 1024 or not ntpath.isabs(raw) or ntpath.basename(raw).lower() != expected_name.lower():
            raise ServiceError("audio_guard_artifact_invalid", "quarantined")
    for field in ("nodeSha256", "dllSha256"):
        if type(value.get(field)) is not str or re.fullmatch(r"[a-f0-9]{64}", value[field]) is None:
            raise ServiceError("audio_guard_digest_invalid", "quarantined")
    for field in ("captureEndpointId", "renderEndpointId"):
        endpoint = value.get(field)
        if type(endpoint) is not str or not endpoint or len(endpoint) >= 1024 or any(ord(ch) < 0x20 for ch in endpoint):
            raise ServiceError("audio_guard_endpoint_invalid", "quarantined")
    if value["captureEndpointId"] == value["renderEndpointId"]:
        raise ServiceError("audio_guard_endpoint_invalid", "quarantined")
    node_path = Path(value["nodeArtifact"])
    dll_path = Path(value["guardDllPath"])
    if _sha256_file(node_path) != value["nodeSha256"] or _sha256_file(dll_path) != value["dllSha256"]:
        raise ServiceError("audio_guard_artifact_mismatch", "quarantined")
    try:
        inventory = (endpoint_inventory or enumerate_active_endpoints)()
    except EndpointInventoryError:
        raise ServiceError("audio_endpoint_inventory_unavailable", "quarantined") from None
    except Exception:
        raise ServiceError("audio_endpoint_inventory_unavailable", "quarantined") from None
    try:
        return resolve_audio_route(
            value,
            inventory,
            {key: config["audio"][key] for key in ("capture_device", "playback_device")},
        )
    except AudioRouteError as exc:
        raise ServiceError(exc.code, "quarantined") from None


class Supervisor:
    """Single-instance lifecycle coordinator with injectable process/RPC boundaries."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        system: Any | None = None,
        rpc_factory: Callable[[str, str | None], Any] = OneBotClient,
        endpoint_inventory: Callable[[], Any] | None = None,
        clock_ms: Callable[[], int] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        identity_discovery_timeout_seconds: float = _IDENTITY_DISCOVERY_TIMEOUT_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        project_root: str | os.PathLike[str] | None = None,
        universal_service_factory: Callable[..., Any] = UniversalServiceSupervisor,
    ):
        self.config = config
        self._auto_adapter = config["adapter"] == "auto"
        self._discovery_config = copy.deepcopy(config)
        self._discovered_onebot_token: str | None = None
        self.runtime_dir = Path(config["runtime_dir"])
        self._status_history = StatusHistory(self.runtime_dir / "status-history.private.json")
        self.project_root = Path(project_root).resolve(strict=False) if project_root is not None else Path(__file__).resolve().parents[1]
        self.system = system if system is not None else WindowsSystem()
        self.universal_service_factory = universal_service_factory
        self.universal_service_status = {"state": "not_checked", "code": "", "reused": False}
        self.rpc_factory = rpc_factory
        self.endpoint_inventory = endpoint_inventory or enumerate_active_endpoints
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        if type(identity_discovery_timeout_seconds) not in (int, float) or identity_discovery_timeout_seconds <= 0:
            raise ValueError("identity discovery timeout must be positive")
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.identity_discovery_timeout_seconds = float(identity_discovery_timeout_seconds)
        self.sleeper = sleeper
        self.active: ActiveGeneration | None = None
        self.run_identity: dict[str, Any] | None = None
        self.failures: dict[tuple[int, int], int] = {}
        self._console_logging = False
        self._last_console_status: tuple[Any, ...] | None = None
        self.supervisor_pid = 0
        self.supervisor_creation_time_100ns = "0"
        try:
            self.supervisor_pid, self.supervisor_creation_time_100ns = self._current_supervisor_identity()
        except (WindowsServiceError, OSError, ValueError):
            pass
        self.last_status = self._result_status("disabled", "service_disabled")
        self._resolved_peers: OrderedDict[tuple[Any, ...], dict[str, str]] = OrderedDict()
        self._resolved_peers_lock = threading.RLock()
        self._binding_fence_lock = threading.Lock()
        self._peer_rpc_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_PEER_IDENTITY_RPCS)
        self._onebot_http_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_ONEBOT_HTTP_EXCHANGES)
        self._peer_rpc_futures_lock = threading.Lock()
        self._peer_rpc_futures: set[Future[dict[str, Any]]] = set()

    @property
    def status_path(self) -> Path:
        return self.runtime_dir / "status.json"

    @property
    def manager_path(self) -> Path:
        return self.runtime_dir / "manager.private.json"

    @property
    def generation_path(self) -> Path:
        return self.runtime_dir / "generation.private.json"

    @property
    def quarantine_path(self) -> Path:
        return self.runtime_dir / "quarantine.private.json"

    @property
    def audio_plan_path(self) -> Path:
        return self.runtime_dir / "audio-plan.private.json"

    def _status(self, phase: str, code: str, *, persist: bool = True) -> dict[str, Any]:
        if phase == "ready":
            code = ""
        self.last_status = self._result_status(phase, code)
        self._log_status(self.last_status)
        if persist:
            try:
                self._validate_private_runtime_location()
                try:
                    self._status_history.write(self.last_status)
                except (PrivateFileError, OSError, ValueError):
                    pass
                atomic_private_json(self.status_path, self.last_status)
            except (ServiceError, PrivateFileError):
                pass
        return self.last_status

    def _log_status(self, status: dict[str, Any]) -> None:
        """Emit changed public status only; leave stdout available for CLI JSON."""
        if not self._console_logging:
            return
        component = status["universalService"]
        adapter = self.config["adapter"]
        adapter = adapter if adapter in ("auto", "napcat", "snowluma") else "unknown"
        key = (status["phase"], status.get("code", ""), adapter,
               component["state"], component["code"], component["reused"])
        if key == self._last_console_status:
            return
        phase, code = key[:2]
        level = "ERROR" if phase in ("error", "quarantined") else (
            "WARN" if phase == "waiting_kernel_restart" or component["state"] == "unavailable" else "INFO"
        )
        universal = {"not_checked": "未检查", "unavailable": "不可用",
                     "ready": "已就绪（复用）" if component["reused"] else "已就绪"}[component["state"]]
        line = (f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} [{level}] [QQCall] "
                f"{_PHASE_LABELS[phase]} phase={phase} adapter={adapter} "
                f"pid={status['supervisorPid']} UniversalVC={universal}")
        if code:
            line += f" code={code}"
        if component["code"] and component["state"] == "unavailable":
            line += f" universal_code={component['code']}"
        try:
            print(line, file=sys.stderr, flush=True)
        except (OSError, ValueError, UnicodeError):
            # Console teardown/encoding must never break supervision or cleanup.
            return
        self._last_console_status = key

    def _result_status(self, phase: str, code: str) -> dict[str, Any]:
        return _safe_status(
            enabled=self.config["enabled"], adapter=self.config["adapter"], phase=phase,
            code=code, updated_at_ms=self.clock_ms(), supervisor_pid=self.supervisor_pid,
            supervisor_creation_time_100ns=self.supervisor_creation_time_100ns,
            universal_service=self.universal_service_status,
        )

    def _ensure_universal_service(self) -> dict[str, Any]:
        api = self.config["universal_api"]
        helper = self.universal_service_factory(
            url=api["url"],
            token_file=api["token_file"],
            token_file_defaulted=bool(self.config.get("universal_api_token_file_defaulted", False)),
            runtime_dir=self.runtime_dir,
            universal_root=self.project_root.parent / "NachoBot-UniversalVC-Adapter",
            system=self.system,
        )
        value = helper.ensure_ready(checkpoint=lambda: self._startup_checkpoint("universal_api_service_starting"))
        normalized = _safe_status(
            enabled=True,
            adapter=self.config["adapter"],
            phase="starting",
            code="service_starting",
            updated_at_ms=self.clock_ms(),
            universal_service=value,
        )["universalService"]
        self.universal_service_status = normalized
        return normalized

    def _read_manifest(self) -> dict[str, Any] | None:
        reader = getattr(self.system, "read_manifest", None)
        if reader:
            value = reader(self.config)
            return None if value is None else _validate_manifest(value, self.config["adapter"])
        return _read_manifest(self.config)

    def _onebot_identity(self) -> tuple[Any, dict[str, Any], str]:
        action_stage: str | None = None
        try:
            token = (self._discovered_onebot_token if self._discovered_onebot_token is not None
                     else _read_token_file(self.config["onebot"]["token_file"]))
            if self.config["adapter"] == "snowluma" and self.rpc_factory is OneBotClient:
                # evaluate() calls this only after the live SnowLuma account
                # and QQ-process binding have been verified.
                client = SnowLumaOneBotClient(
                    self.config["onebot"]["url"],
                    token,
                    account_id=self.config["account_id"],
                    project_root=Path(__file__).resolve().parents[2],
                )
            else:
                client = self.rpc_factory(self.config["onebot"]["url"], token)
            if isinstance(client, OneBotClient):
                # A timed-out manager future may leave its HTTP exchange
                # thread draining a blocked response. Keep the transport cap
                # on the Supervisor so every newly-created retry client shares
                # those still-owned exchanges.
                client.set_http_transport_limiter(self._onebot_http_slots)
            action_stage = "onebot_status_rpc_wait_timeout"
            status_data = client.call("get_status")
            if not isinstance(status_data, dict):
                raise ServiceError("onebot_offline", "waiting_login")
            status_online = (
                _snowluma_status_online(status_data)
                if self.config["adapter"] == "snowluma"
                else _status_online(status_data)
            )
            if not status_online:
                raise ServiceError("onebot_offline", "waiting_login")
            action_stage = "onebot_login_rpc_wait_timeout"
            login_data = client.call("get_login_info")
            account = _response_uin(login_data) if isinstance(login_data, dict) else None
            if account is None:
                raise ServiceError("onebot_identity_unavailable", "waiting_login")
            if account != self.config["account_id"]:
                raise ServiceError("onebot_account_mismatch", "quarantined")
            if isinstance(client, _SNOWLUMA_ONEBOT_CLIENT_TYPE):
                uid = client.resolve_self_uid()
                if _uid_from({"uid": uid}) is None:
                    raise ServiceError("self_uid_unavailable", "waiting_identity")
                return client, {"user_id": account, "uid": uid}, uid
            # For SnowLuma this tags the get_stranger_info action, not which
            # internal identity-cache or HTTP step may have timed out. The
            # Snow client above resolves only its own authenticated account
            # from the local cache; generic and NapCat clients keep this RPC.
            action_stage = "onebot_self_rpc_wait_timeout"
            self_data = client.call("get_stranger_info", {"user_id": account})
            if not isinstance(self_data, dict) or _response_uin(self_data) != account:
                raise ServiceError("self_identity_unverified", "waiting_identity")
            uid = _uid_from(self_data)
            if uid is None:
                raise ServiceError("self_uid_unavailable", "waiting_identity")
            return client, self_data, uid
        except ServiceError:
            raise
        except SnowLumaIdentityError:
            raise ServiceError("self_uid_unavailable", "waiting_identity") from None
        except OneBotError as exc:
            diagnostic_stage = (
                action_stage if type(exc.code) is str and exc.code == "onebot_timeout" else None
            )
            raise ServiceError(exc.code, "waiting_login", diagnostic_stage=diagnostic_stage) from None
        except (OSError, ValueError, TypeError):
            raise ServiceError("onebot_unavailable", "waiting_login") from None

    def _identity(self, self_uid: str) -> tuple[dict[str, str], dict[str, str], str | None]:
        configured_path = self.config["identity_file"]
        if configured_path:
            identity = _read_identity_file(configured_path, self.config["account_id"])
            if identity["selfUid"] != self_uid:
                raise ServiceError("identity_uid_mismatch", "waiting_identity")
            return identity, {"selfUid": identity["selfUid"], "selfUin": identity["selfUin"]}, configured_path
        source = {"selfUid": self_uid, "selfUin": self.config["account_id"]}
        identity = {**source, "accountPath": ""}
        return identity, source, None

    def _peer_cache_key(self, manifest: dict[str, Any], binding: Any, self_uid: str) -> tuple[Any, ...]:
        app_path = str(_get(binding, "app_path", "appPath"))
        canonical_app_path = os.path.normcase(os.path.realpath(app_path)).rstrip("\\/")
        explicit = tuple(sorted(self.config["peer_map"].items()))
        return (
            self.config["adapter"], self.config["account_id"],
            int(_get(binding, "adapter_pid", "adapterPid")),
            int(_get(binding, "adapter_creation_time_100ns", "adapterCreationTime100ns")),
            manifest["connectionGeneration"],
            int(_get(binding, "kernel_pid", "kernelPid")),
            int(_get(binding, "kernel_creation_time_100ns", "kernelCreationTime100ns")),
            canonical_app_path, self_uid,
            "explicit" if explicit else "discovered", explicit,
        )

    def _cached_peer_map(self, key: tuple[Any, ...]) -> tuple[bool, dict[str, str]]:
        with self._resolved_peers_lock:
            if key not in self._resolved_peers:
                return False, {}
            self._resolved_peers.move_to_end(key)
            return True, dict(self._resolved_peers[key])

    def _remember_peer_map(self, key: tuple[Any, ...], peer_map: dict[str, str]) -> None:
        with self._resolved_peers_lock:
            self._resolved_peers[key] = dict(peer_map)
            self._resolved_peers.move_to_end(key)
            while len(self._resolved_peers) > _PEER_CACHE_ENTRIES:
                self._resolved_peers.popitem(last=False)

    def _forget_peer_map(self, key: tuple[Any, ...] | None) -> None:
        if key is None:
            return
        with self._resolved_peers_lock:
            self._resolved_peers.pop(key, None)

    def _identity_time_remaining(
        self,
        deadline: float,
        *,
        diagnostic_stage: str = "identity_deadline_exhausted",
    ) -> float:
        remaining = deadline - self.monotonic_clock()
        if remaining <= 0:
            raise ServiceError(
                "peer_identity_timeout", "waiting_identity", diagnostic_stage=diagnostic_stage,
            )
        return remaining

    def _identity_rpc(
        self,
        client: Any,
        action: str,
        params: dict[str, Any],
        deadline: float,
        *,
        timeout_stage: str,
        deadline_stage: str = "identity_deadline_exhausted",
    ) -> dict[str, Any]:
        timeout = self._identity_time_remaining(deadline, diagnostic_stage=deadline_stage)
        try:
            value = client.call(action, params, timeout=timeout)
        except OneBotError as exc:
            code = "peer_identity_timeout" if exc.code == "onebot_timeout" else "peer_identity_unavailable"
            raise ServiceError(
                code,
                "waiting_identity",
                diagnostic_stage=timeout_stage if code == "peer_identity_timeout" else None,
            ) from None
        except (OSError, TimeoutError, TypeError, ValueError):
            raise ServiceError("peer_identity_unavailable", "waiting_identity") from None
        self._identity_time_remaining(deadline, diagnostic_stage=deadline_stage)
        if type(value) is not dict:
            raise ServiceError("peer_identity_unavailable", "waiting_identity")
        return value

    def _peer_rpc_finished(self, future: Future[dict[str, Any]]) -> None:
        with self._peer_rpc_futures_lock:
            self._peer_rpc_futures.discard(future)
        self._peer_rpc_slots.release()

    def _submit_peer_identity_rpc(
        self,
        executor: ThreadPoolExecutor,
        client: Any,
        uin: str,
        deadline: float,
    ) -> Future[dict[str, Any]]:
        remaining = self._identity_time_remaining(
            deadline, diagnostic_stage="peer_discovery_deadline_exhausted",
        )
        if not self._peer_rpc_slots.acquire(timeout=remaining):
            raise ServiceError(
                "peer_identity_timeout", "waiting_identity",
                diagnostic_stage="peer_identity_rpc_slot_timeout",
            )
        try:
            future = executor.submit(
                self._identity_rpc,
                client,
                "get_stranger_info",
                {"user_id": uin},
                deadline,
                timeout_stage="peer_discovery_rpc_wait_timeout",
                deadline_stage="peer_discovery_deadline_exhausted",
            )
        except (OSError, RuntimeError):
            self._peer_rpc_slots.release()
            raise ServiceError("peer_identity_unavailable", "waiting_identity") from None
        with self._peer_rpc_futures_lock:
            self._peer_rpc_futures.add(future)
        future.add_done_callback(self._peer_rpc_finished)
        return future

    def _verify_onebot_identity(self, client: Any, self_uid: str, deadline: float) -> None:
        login_data = self._identity_rpc(
            client, "get_login_info", {}, deadline,
            timeout_stage="onebot_login_rpc_wait_timeout",
        )
        account = _strict_identity_uin(login_data)
        if account is None:
            raise ServiceError("onebot_identity_unavailable", "waiting_identity")
        if account != self.config["account_id"]:
            raise ServiceError("onebot_account_mismatch", "quarantined")
        if isinstance(client, _SNOWLUMA_ONEBOT_CLIENT_TYPE):
            try:
                live_uid = client.resolve_self_uid(
                    timeout=self._identity_time_remaining(deadline),
                )
            except SnowLumaIdentityError:
                raise ServiceError("self_uid_unavailable", "waiting_identity") from None
            self._identity_time_remaining(deadline)
            if _uid_from({"uid": live_uid}) is None:
                raise ServiceError("self_identity_unverified", "waiting_identity")
            if live_uid != self_uid:
                raise ServiceError("self_uid_changed", "waiting_identity")
            return
        self_data = self._identity_rpc(
            client, "get_stranger_info", {"user_id": account}, deadline,
            timeout_stage="onebot_self_rpc_wait_timeout",
        )
        self_account = _strict_identity_uin(self_data)
        if self_account is None:
            raise ServiceError("self_identity_unverified", "waiting_identity")
        if self_account != account:
            raise ServiceError("self_identity_unverified", "waiting_identity")
        live_uid = _strict_identity_uid(self_data)
        if live_uid is None or live_uid != self_uid:
            raise ServiceError("self_uid_changed", "waiting_identity")

    def _discover_peer_map(self, client: Any, self_uid: str, deadline: float) -> dict[str, str]:
        friend_list_method = getattr(client, "get_friend_list", None)
        if not callable(friend_list_method):
            raise ServiceError("friend_list_unavailable", "waiting_identity")
        timeout = self._identity_time_remaining(
            deadline, diagnostic_stage="peer_discovery_deadline_exhausted",
        )
        try:
            friends = friend_list_method(timeout=timeout)
        except OneBotError as exc:
            code = "peer_identity_timeout" if exc.code == "onebot_timeout" else "friend_list_unavailable"
            raise ServiceError(
                code,
                "waiting_identity",
                diagnostic_stage="peer_discovery_rpc_wait_timeout" if code == "peer_identity_timeout" else None,
            ) from None
        except (OSError, TimeoutError, TypeError, ValueError):
            raise ServiceError("friend_list_unavailable", "waiting_identity") from None
        self._identity_time_remaining(deadline, diagnostic_stage="peer_discovery_deadline_exhausted")
        if type(friends) is not list:
            raise ServiceError("friend_list_invalid", "waiting_identity")
        if len(friends) > _MAX_DISCOVERED_FRIENDS:
            raise ServiceError("friend_list_too_large", "waiting_identity")

        friend_uins: list[str] = []
        seen_uins: set[str] = set()
        for friend in friends:
            if type(friend) is not dict:
                raise ServiceError("friend_list_invalid", "waiting_identity")
            uin = _strict_identity_uin(friend)
            if uin is None:
                raise ServiceError("friend_list_invalid", "waiting_identity")
            if uin in seen_uins:
                raise ServiceError("friend_identity_ambiguous", "quarantined")
            seen_uins.add(uin)
            if uin == self.config["account_id"]:
                # OneBot may include the already-verified account in its friend list.
                # Exclude that single exact account row; never treat it as a peer.
                continue
            friend_uins.append(uin)

        result: dict[str, str] = {}
        uin_by_uid: dict[str, str] = {}
        try:
            executor = ThreadPoolExecutor(
                max_workers=_MAX_CONCURRENT_PEER_IDENTITY_RPCS,
                thread_name_prefix="qqcall-peer-identity",
            )
        except (OSError, RuntimeError):
            raise ServiceError("peer_identity_unavailable", "waiting_identity") from None
        pending: list[tuple[str, Future[dict[str, Any]]]] = []

        def consume_pending() -> None:
            for uin, future in pending:
                remaining = self._identity_time_remaining(
                    deadline, diagnostic_stage="peer_discovery_deadline_exhausted",
                )
                try:
                    data = future.result(timeout=remaining)
                except FutureTimeoutError:
                    raise ServiceError(
                        "peer_identity_timeout", "waiting_identity",
                        diagnostic_stage="peer_identity_rpc_wait_timeout",
                    ) from None
                self._identity_time_remaining(deadline, diagnostic_stage="peer_discovery_deadline_exhausted")

                # Consume results in friend-list order even though their RPCs
                # run concurrently, keeping validation and error precedence
                # deterministic.
                live_uin = _strict_identity_uin(data)
                if live_uin is None:
                    raise ServiceError("friend_identity_unverified", "waiting_identity")
                if live_uin != uin:
                    raise ServiceError("friend_identity_conflict", "quarantined")
                uid = _strict_identity_uid(data)
                if uid is None:
                    raise ServiceError("friend_uid_unavailable", "waiting_identity")
                if uid == self_uid:
                    raise ServiceError("friend_identity_ambiguous", "quarantined")
                previous_uin = uin_by_uid.get(uid)
                if previous_uin is not None and previous_uin != uin:
                    raise ServiceError("friend_identity_ambiguous", "quarantined")
                uin_by_uid[uid] = uin
                result[uid] = uin
            pending.clear()

        try:
            for uin in friend_uins:
                pending.append((
                    uin,
                    self._submit_peer_identity_rpc(executor, client, uin, deadline),
                ))
                if len(pending) >= _MAX_CONCURRENT_PEER_IDENTITY_RPCS:
                    # ``pending`` is kept in input order, which also makes
                    # failure selection independent of RPC completion order.
                    consume_pending()
            if pending:
                consume_pending()
            return result
        except BaseException:
            for _uin, future in pending:
                future.cancel()
            raise
        finally:
            # Running RPCs retain their semaphore slots until their completion
            # callbacks run. A retry therefore cannot create more than four
            # simultaneous workers even when an earlier request timed out.
            executor.shutdown(wait=False, cancel_futures=True)

    def _peer_map(
        self,
        client: Any,
        manifest: dict[str, Any],
        binding: Any,
        self_uid: str,
    ) -> PeerResolution:
        key = self._peer_cache_key(manifest, binding, self_uid)
        found, cached = self._cached_peer_map(key)
        if found:
            if self.config["peer_map"]:
                return PeerResolution(cached, key)
            if self._binding_fence_lock.locked():
                raise ServiceError(
                    "peer_identity_timeout", "waiting_identity",
                    diagnostic_stage="cached_binding_fence_busy",
                )
            deadline = self.monotonic_clock() + self.identity_discovery_timeout_seconds
            return PeerResolution(cached, key, deadline)

        configured_by_uin = {uin: uid for uid, uin in self.config["peer_map"].items()}
        result: dict[str, str] = {}
        if configured_by_uin:
            # Explicit peer_map remains lookup-only; adapter admission stays
            # independent and missing UIDs may fall back only to configured IDs.
            for user in dict.fromkeys(self.config["peer_map"].values()):
                try:
                    data = client.call("get_stranger_info", {"user_id": user})
                except OneBotError:
                    continue
                if not isinstance(data, dict) or _response_uin(data) != user:
                    continue
                live_uid = _uid_from(data)
                explicit_uid = configured_by_uin.get(user)
                if live_uid and explicit_uid and live_uid != explicit_uid:
                    raise ServiceError("peer_identity_mismatch", "quarantined")
                uid = live_uid or explicit_uid
                if uid is None:
                    continue
                if uid in result and result[uid] != user:
                    raise ServiceError("peer_identity_ambiguous", "quarantined")
                result[uid] = user
            self._remember_peer_map(key, result)
            return PeerResolution(result, key)

        if self._binding_fence_lock.locked():
            raise ServiceError(
                "peer_identity_timeout", "waiting_identity",
                diagnostic_stage="peer_discovery_fence_busy",
            )
        deadline = self.monotonic_clock() + self.identity_discovery_timeout_seconds
        discovered = self._discover_peer_map(client, self_uid, deadline)
        self._identity_time_remaining(deadline, diagnostic_stage="peer_discovery_deadline_exhausted")
        return PeerResolution(discovered, key, deadline, pending_cache=True)

    def _validate_ports(self) -> None:
        method = getattr(self.system, "listener_owners", None)
        if method is None:
            return
        for port in self.config["ports"].values():
            owners = method(port)
            if owners:
                raise ServiceError("service_port_in_use", "waiting_target")

    def evaluate(self) -> Evaluation:
        if not self._auto_adapter:
            return self._evaluate_selected_adapter()
        # An attached/native generation is pinned. A newly appearing preferred
        # adapter must not consume another Shim or interrupt an existing call.
        if self.active is not None:
            return self._evaluate_selected_adapter()
        previous_config = self.config
        errors: list[ServiceError] = []
        for adapter in ("napcat", "snowluma"):
            self.config = {**self._discovery_config, "adapter": adapter}
            try:
                evaluation = self._evaluate_selected_adapter()
                if self._kernel_quarantined(evaluation):
                    raise ServiceError("fixed_shim_kernel_consumed", "waiting_kernel_restart")
                return evaluation
            except ServiceError as exc:
                errors.append(exc)
            except Exception:
                self.config = previous_config
                raise
        self.config = previous_config
        # Prefer the first substantive failure over an absent second adapter.
        raise next((error for error in errors if error.code != "adapter_not_connected"), errors[0])

    def _evaluate_selected_adapter(self) -> Evaluation:
        if not self.config["enabled"]:
            raise ServiceError("service_disabled", "disabled")
        self._validate_private_runtime_location()
        if not getattr(self.system, "supported", os.name == "nt") and os.name != "nt":
            raise ServiceError("platform_unsupported", "error")
        try:
            manifest = self._read_manifest()
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("adapter_manifest_invalid", "quarantined") from None
        if manifest is None or manifest["connected"] is not True:
            raise ServiceError("adapter_not_connected", "waiting_adapter")
        now_ms = self.clock_ms()
        manifest_age = now_ms - manifest["updatedAtMs"]
        if manifest_age < -5_000 or manifest_age > 10_000:
            raise ServiceError("adapter_manifest_stale", "waiting_adapter")
        if manifest["accountId"] != self.config["account_id"]:
            raise ServiceError("adapter_account_mismatch", "quarantined")

        try:
            discover = getattr(self.system, "discover_onebot_connection", None)
            self._discovered_onebot_token = None
            if self._auto_adapter and discover:
                url, token = discover(self.config)
                self.config = {**self.config, "onebot": {**self.config["onebot"], "url": url}}
                self._discovered_onebot_token = token
            binding = self.system.bind_adapter(self.config, manifest)
        except WindowsServiceError as exc:
            phase = "waiting_target" if exc.code in ("target_ambiguous", "node_service_unavailable", "target_app_path_unverified") else "waiting_adapter"
            raise ServiceError(exc.code, phase, diagnostic_stage=exc._diagnostic_stage) from None
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("adapter_binding_unavailable", "waiting_target") from None

        client, _self_data, self_uid = self._onebot_identity()
        identity, identity_source, full_path = self._identity(self_uid)
        resolution = self._peer_map(client, manifest, binding, self_uid)
        guard = _validate_guard(self.config, self.endpoint_inventory)

        try:
            if resolution.deadline is not None:
                self._verify_onebot_identity(client, self_uid, resolution.deadline)
                self._identity_time_remaining(
                    resolution.deadline, diagnostic_stage="identity_deadline_exhausted",
                )
            if resolution.deadline is not None:
                binding_is_current = self._binding_still_current_until(
                    manifest, binding, resolution.deadline,
                )
            else:
                binding_is_current = self._binding_still_current(manifest, binding)
            if resolution.deadline is not None:
                self._identity_time_remaining(
                    resolution.deadline, diagnostic_stage="final_binding_deadline_exhausted",
                )
        except ServiceError:
            self._forget_peer_map(resolution.cache_key)
            raise
        if not binding_is_current:
            self._forget_peer_map(resolution.cache_key)
            raise ServiceError("target_binding_changed", "waiting_target")
        if resolution.pending_cache and resolution.cache_key is not None:
            self._remember_peer_map(resolution.cache_key, resolution.peer_map)
        return Evaluation(manifest, binding, identity, identity_source, full_path, resolution.peer_map, guard)

    def _binding_still_current(
        self,
        manifest: dict[str, Any],
        binding: Any,
        *,
        verify_manifest_after: bool = False,
        explain_failure: bool = False,
    ) -> bool:
        def reject(stage: str) -> bool:
            if explain_failure:
                raise ServiceError("adapter_binding_changed", "waiting_target", diagnostic_stage=stage)
            return False

        try:
            current_manifest = self._read_manifest()
            if current_manifest is None:
                return reject("binding_manifest_unavailable")
            if _manifest_fingerprint(current_manifest) != _manifest_fingerprint(manifest):
                return reject("binding_manifest_changed")
            method = getattr(self.system, "revalidate_binding_with_error", None)
            if method is None:
                method = getattr(self.system, "revalidate_binding", None)
            if method and not method(self.config, current_manifest, binding):
                return reject("binding_target_changed")
            if verify_manifest_after:
                final_manifest = self._read_manifest()
                if final_manifest is None:
                    return reject("binding_final_manifest_unavailable")
                if _manifest_fingerprint(final_manifest) != _manifest_fingerprint(manifest):
                    return reject("binding_final_manifest_changed")
            return True
        except WindowsServiceError as exc:
            phase = "waiting_adapter" if exc.code == "adapter_auth_unavailable" else "waiting_target"
            raise ServiceError(exc.code, phase, diagnostic_stage=exc._diagnostic_stage) from None
        except ServiceError:
            raise
        except Exception:
            return reject("binding_recheck_exception")

    def _binding_still_current_until(
        self,
        manifest: dict[str, Any],
        binding: Any,
        deadline: float,
    ) -> bool:
        self._identity_time_remaining(deadline, diagnostic_stage="final_binding_deadline_exhausted")
        if not self._binding_fence_lock.acquire(blocking=False):
            raise ServiceError(
                "peer_identity_timeout", "waiting_identity",
                diagnostic_stage="final_binding_fence_busy",
            )
        finished = threading.Event()
        outcome: dict[str, Any] = {}

        def validate() -> None:
            try:
                outcome["current"] = self._binding_still_current(
                    manifest, binding, verify_manifest_after=True,
                )
            except ServiceError as exc:
                outcome["error"] = exc
            finally:
                self._binding_fence_lock.release()
                finished.set()

        worker = threading.Thread(target=validate, name="qqcall-peer-binding-fence", daemon=True)
        try:
            worker.start()
        except RuntimeError:
            self._binding_fence_lock.release()
            raise ServiceError("adapter_binding_unavailable", "waiting_target") from None
        wait_timeout = self._identity_time_remaining(
            deadline, diagnostic_stage="final_binding_deadline_exhausted",
        )
        if not finished.wait(wait_timeout):
            raise ServiceError(
                "peer_identity_timeout", "waiting_identity",
                diagnostic_stage="final_binding_fence_wait_timeout",
            )
        self._identity_time_remaining(deadline, diagnostic_stage="final_binding_deadline_exhausted")
        if isinstance(outcome.get("error"), ServiceError):
            raise outcome["error"]
        return bool(outcome.get("current", False))

    def _revalidate_audio_route(self, evaluation: Evaluation) -> None:
        if not self.config["audio"]["enabled"]:
            if evaluation.guard_source is not None:
                raise ServiceError("audio_route_changed", "quarantined")
            return
        current = _validate_guard(self.config, self.endpoint_inventory)
        if audio_route_identity(current) != audio_route_identity(evaluation.guard_source):
            raise ServiceError("audio_route_changed", "quarantined")

    def inspect(self) -> dict[str, Any]:
        if not self.config["enabled"]:
            return self._result_status("disabled", "service_disabled")
        try:
            self._validate_private_runtime_location()
            self.evaluate()
            self._validate_ports()
        except ServiceError as exc:
            return self._result_status(exc.phase, exc.code)
        return self._result_status("needs_attachment", "plan_ready_no_changes")

    def plan(self) -> dict[str, Any]:
        return self.inspect()

    def _read_generation(self) -> int:
        if not self.generation_path.exists():
            return 0
        try:
            value = read_private_json(self.generation_path, max_bytes=4096)
        except PrivateFileError:
            raise ServiceError("generation_state_invalid", "quarantined") from None
        if not isinstance(value, dict) or set(value) != {"schema", "lastGeneration"} or type(value["schema"]) is not int or value["schema"] != SCHEMA:
            raise ServiceError("generation_state_invalid", "quarantined")
        last = value["lastGeneration"]
        if type(last) is not int or not 0 <= last < MAX_SAFE_INTEGER:
            raise ServiceError("generation_state_invalid", "quarantined")
        return last

    def _allocate_generation(self) -> int:
        generation = self._read_generation() + 1
        atomic_private_json(self.generation_path, {"schema": SCHEMA, "lastGeneration": generation})
        return generation

    def _ensure_private_runtime(self) -> None:
        self._validate_private_runtime_location()
        secure_directory(Path(os.path.abspath(self.runtime_dir)))

    def _validate_private_runtime_location(self) -> None:
        project_runtime = self.project_root / ".runtime"
        runtime = Path(os.path.abspath(self.runtime_dir))
        try:
            runtime.relative_to(project_runtime)
        except ValueError:
            raise ServiceError("runtime_must_be_private_project_path", "error") from None
        if runtime == project_runtime:
            raise ServiceError("runtime_must_be_private_project_path", "error")
        current = project_runtime
        if is_reparse_point(current):
            raise ServiceError("runtime_path_invalid", "error")
        try:
            for part in runtime.relative_to(project_runtime).parts:
                current = current / part
                if is_reparse_point(current):
                    raise ServiceError("runtime_path_invalid", "error")
        except OSError:
            raise ServiceError("runtime_path_invalid", "error") from None

    def _read_quarantine(self) -> list[dict[str, Any]]:
        if not self.quarantine_path.exists():
            return []
        try:
            value = read_private_json(self.quarantine_path, max_bytes=16 * 1024)
        except PrivateFileError:
            raise ServiceError("quarantine_state_invalid", "quarantined") from None
        if not isinstance(value, dict) or set(value) != {"schema", "kernels"} or value.get("schema") != SCHEMA or type(value.get("schema")) is not int or not isinstance(value.get("kernels"), list):
            raise ServiceError("quarantine_state_invalid", "quarantined")
        result = []
        for item in value["kernels"]:
            if not isinstance(item, dict) or set(item) != {"pid", "creationTime100ns", "reason"}:
                raise ServiceError("quarantine_state_invalid", "quarantined")
            if (type(item["pid"]) is not int or not 0 < item["pid"] <= 0x7FFFFFFF or type(item["creationTime100ns"]) is not str or
                    not re.fullmatch(r"[1-9][0-9]{0,19}", item["creationTime100ns"]) or int(item["creationTime100ns"]) > MAX_FILETIME or
                    item["reason"] not in ("shim_consumed", "detach_unconfirmed", "controller_failed")):
                raise ServiceError("quarantine_state_invalid", "quarantined")
            result.append(item)
        return result

    def _kernel_quarantined(self, evaluation: Evaluation) -> bool:
        records = self._read_quarantine()
        retained: list[dict[str, Any]] = []
        blocked = False
        process_current = getattr(self.system, "process_is_current", None)
        for record in records:
            live = process_current(record["pid"], int(record["creationTime100ns"])) if process_current else None
            matches_evaluation = (
                record["pid"] == evaluation.kernel_pid
                and int(record["creationTime100ns"]) == evaluation.kernel_filetime
            )
            if live is None:
                retained.append(record)
                if matches_evaluation:
                    blocked = True
                continue
            if live is True:
                retained.append(record)
                if matches_evaluation:
                    blocked = True
        if retained != records:
            atomic_private_json(self.quarantine_path, {"schema": SCHEMA, "kernels": retained})
        return blocked

    def _quarantine_kernel(self, evaluation: Evaluation, reason: str) -> None:
        self._quarantine_process(evaluation.kernel_pid, evaluation.kernel_filetime, reason)

    def _quarantine_process(self, pid: int, filetime: int, reason: str) -> None:
        if (type(pid) is not int or not 0 < pid <= 0x7FFFFFFF or type(filetime) is not int or
                not 0 < filetime <= MAX_FILETIME or reason not in ("shim_consumed", "detach_unconfirmed", "controller_failed")):
            raise ServiceError("quarantine_binding_invalid", "quarantined")
        records = self._read_quarantine()
        item = {
            "pid": pid,
            "creationTime100ns": str(filetime),
            "reason": reason,
        }
        key = (item["pid"], item["creationTime100ns"])
        records = [entry for entry in records if (entry["pid"], entry["creationTime100ns"]) != key]
        records.append(item)
        atomic_private_json(self.quarantine_path, {"schema": SCHEMA, "kernels": records[-128:]})

    def _recover_prior_attach_attempt(self) -> None:
        if not self.manager_path.exists() and not self.manager_path.is_symlink():
            return
        if self.manager_path.is_symlink():
            raise ServiceError("manager_record_invalid", "quarantined")
        try:
            previous = read_private_json(self.manager_path, max_bytes=32 * 1024)
        except PrivateFileError:
            raise ServiceError("manager_record_invalid", "quarantined") from None
        if not isinstance(previous, dict) or "attachAttemptPath" not in previous:
            return
        path_text = previous.get("attachAttemptPath")
        pid = previous.get("kernelPid")
        filetime_text = previous.get("kernelCreationTime100ns")
        run_generation = previous.get("runGeneration")
        controller_generation = previous.get("controllerGeneration")
        host_generation = previous.get("hostGeneration")
        if (type(path_text) is not str or type(pid) is not int or not 0 < pid <= 0x7FFFFFFF or
                type(filetime_text) is not str or re.fullmatch(r"[1-9][0-9]{0,19}", filetime_text) is None or
                int(filetime_text) > MAX_FILETIME or type(run_generation) is not int or run_generation <= 0 or
                type(controller_generation) is not int or controller_generation <= 0 or
                type(host_generation) is not int or host_generation <= 0):
            raise ServiceError("manager_record_invalid", "quarantined")
        marker = Path(os.path.abspath(path_text))
        generation_root = Path(os.path.abspath(self.runtime_dir / "generations"))
        try:
            relative_marker = marker.relative_to(generation_root)
        except ValueError:
            raise ServiceError("manager_record_invalid", "quarantined") from None
        if ".." in relative_marker.parts or marker.name != "attach-attempt.private.json" or marker.is_symlink():
            if marker.is_symlink():
                self._quarantine_process(pid, int(filetime_text), "shim_consumed")
                return
            raise ServiceError("manager_record_invalid", "quarantined")
        if not marker.exists():
            return
        # A marker is atomically created by the helper immediately before its
        # first attach call. Its presence is sufficient; malformed contents are
        # still treated as a possible native attach attempt.
        try:
            evidence = read_private_json(marker, max_bytes=4096)
            valid = (
                isinstance(evidence, dict) and
                set(evidence) == {
                    "schema", "supervisorPid", "supervisorCreationTime100ns", "runGeneration",
                    "controllerGeneration", "hostGeneration", "kernelPid", "kernelCreationTime100ns", "attemptedAtMs",
                } and evidence.get("schema") == SCHEMA and type(evidence.get("schema")) is int and
                evidence.get("pid", pid) == pid and evidence.get("kernelPid") == pid and
                evidence.get("kernelCreationTime100ns") == filetime_text and
                evidence.get("runGeneration") == run_generation and evidence.get("controllerGeneration") == controller_generation and
                evidence.get("hostGeneration") == host_generation and type(evidence.get("attemptedAtMs")) is int
            )
        except PrivateFileError:
            valid = False
        _ = valid
        process_is_current = getattr(self.system, "process_is_current", None)
        current = process_is_current(pid, int(filetime_text)) if process_is_current else None
        if current is not False:
            self._quarantine_process(pid, int(filetime_text), "shim_consumed")

    def _manager_identity(self, run_generation: int) -> dict[str, Any]:
        method = getattr(self.system, "current_process_identity", None)
        if method:
            pid, filetime = method()
        elif os.name == "nt":
            pid, filetime = WindowsSystem().current_process_identity()
        else:
            pid, filetime = os.getpid(), max(1, time.time_ns() // 100)
        return {
            "schema": SCHEMA,
            "pid": pid,
            "creationTime100ns": str(filetime),
            "runGeneration": run_generation,
            "startedAtMs": self.clock_ms(),
        }

    def _recover_prior_owned_hosts(self) -> bool:
        """Reap recorded Hosts only after the previous supervisor/controller exited.

        Caller holds service.lock. Never overwrite the ownership receipt on a
        failed cleanup, and never infer ownership from a listening port.
        """
        if not self.manager_path.exists():
            return False
        if is_reparse_point(self.manager_path):
            raise ServiceError("manager_record_invalid", "quarantined")
        previous = read_private_json(self.manager_path, max_bytes=32 * 1024)
        if not self._valid_manager_record(previous):
            raise ServiceError("manager_record_invalid", "quarantined")
        records = previous.get("ownedHosts", [])
        if not records:
            return False
        # Unlike the UI's boolean presence helper, unknown/access-denied is
        # not proof of exit and must not authorize reclaiming a process tree.
        if self.system.process_is_current(previous["pid"], int(previous["creationTime100ns"])) is not False:
            raise ServiceError("prior_supervisor_still_running", "waiting_target")
        controller_pid = previous.get("controllerPid")
        controller_time = previous.get("controllerCreationTime100ns")
        if (type(controller_pid) is not int or not 0 < controller_pid <= 0x7fffffff
                or type(controller_time) is not str or not re.fullmatch(r"[1-9][0-9]{0,19}", controller_time)
                or int(controller_time) > MAX_FILETIME):
            raise ServiceError("manager_record_invalid", "quarantined")
        if self.system.process_is_current(controller_pid, int(controller_time)) is not False:
            raise ServiceError("prior_controller_still_running", "waiting_target")
        generation = previous.get("hostGeneration")
        if (type(generation) is not int or not 0 < generation <= MAX_SAFE_INTEGER
                or type(records) is not list or not 1 <= len(records) <= 128):
            raise ServiceError("manager_record_invalid", "quarantined")
        profile = self.runtime_dir / "hosts" / f"host-{generation}" / "profile"
        for directory in (self.runtime_dir / "hosts", profile.parent, profile):
            if is_reparse_point(directory):
                raise ServiceError("manager_record_invalid", "quarantined")
        path_key = lambda value: ntpath.normcase(ntpath.normpath(value))
        launcher_image = path_key(str(Path(self.config["paths"]["loader_root"]) / "NapCatWinBootMain.exe"))
        qq_image = self.config["paths"]["qq_exe"]
        seen = set()
        for record in records:
            if type(record) is not dict:
                raise ServiceError("manager_record_invalid", "quarantined")
            pid, creation = record.get("pid"), record.get("creationTime100ns")
            image, stored_profile, role = record.get("imagePath"), record.get("profilePath"), record.get("role")
            if (type(pid) is not int or not 0 < pid <= 0x7fffffff or pid in seen
                    or pid in (previous["pid"], controller_pid, previous.get("kernelPid"), os.getpid())
                    or type(creation) is not str or not re.fullmatch(r"[1-9][0-9]{0,19}", creation)
                    or int(creation) > MAX_FILETIME
                    or type(image) is not str or not ntpath.isabs(image)
                    or type(stored_profile) is not str or path_key(stored_profile) != path_key(str(profile))
                    or role not in ("launcher", "host", "host_child")):
                raise ServiceError("manager_record_invalid", "quarantined")
            seen.add(pid)
            if role == "launcher":
                valid_image = path_key(image) == launcher_image
            else:
                valid_image = (ntpath.basename(image).lower() == "qq.exe"
                               and (not qq_image or path_key(image) == path_key(qq_image)))
            if not valid_image:
                raise ServiceError("manager_record_invalid", "quarantined")
        if (sum(record["role"] == "host" for record in records) != 1
                or sum(record["role"] == "launcher" for record in records) != 1):
            raise ServiceError("manager_record_invalid", "quarantined")
        # terminate_owned checks PID, FILETIME and image again on the opened
        # process handle. PID reuse is left untouched. Retain all saved children
        # even if their launcher has already exited.
        for _attempt in range(2):
            if self.system.terminate_owned(records):
                atomic_private_json(self.manager_path, {**previous, "ownedHosts": []})
                return True
        raise ServiceError("prior_host_cleanup_unconfirmed", "quarantined")

    def _current_supervisor_identity(self) -> tuple[int, str]:
        method = getattr(self.system, "current_process_identity", None)
        if method:
            pid, filetime = method()
            return int(pid), str(filetime)
        if os.name == "nt":
            pid, filetime = WindowsSystem().current_process_identity()
            return pid, str(filetime)
        return os.getpid(), str(max(1, time.time_ns() // 100))

    def _make_guard_for_generation(self, source: dict[str, Any] | None, host_generation: int, directory: Path) -> str | None:
        if source is None:
            return None
        value = dict(source["guard"])
        value["hostGeneration"] = host_generation
        path = directory / "audio-guard.private.json"
        atomic_private_json(path, value)
        return str(path)

    def _write_attach_helper(
        self, evaluation: Evaluation, generation_dir: Path, broker_config: Path,
        host_generation: int, connection_generation: int,
        controller_pid: int, controller_filetime: str,
    ) -> Path:
        injector = self.project_root / "scripts" / "qqcall_injector.py"
        shim = self.project_root / "native" / "build" / "x64" / "QQCallRecoveryShim2.dll"
        identity_probe = self.project_root / "service" / "process_identity.py"
        if _sha256_file(shim) != _RECOVERY_SHIM_SHA256.lower():
            raise ServiceError("recovery_shim_hash_mismatch", "quarantined")
        if not identity_probe.is_file():
            raise ServiceError("service_dependency_missing")
        expected_start = _filetime_utc(evaluation.kernel_filetime)
        if self.run_identity is None:
            raise ServiceError("supervisor_identity_unavailable")
        run_generation = self.run_identity["runGeneration"]
        controller_generation = connection_generation
        marker_path = generation_dir / "attach-attempt.private.json"

        def ps_literal(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        script = f'''[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
$TargetPid = {evaluation.kernel_pid}
$ExpectedFileTime = {ps_literal(str(evaluation.kernel_filetime))}
$ExpectedStart = {ps_literal(expected_start)}
$ExpectedImage = {ps_literal(self.config["paths"]["qq_exe"])}
$PythonExe = {ps_literal(self.config["paths"]["python_exe"])}
$Injector = {ps_literal(str(injector))}
$Shim = {ps_literal(str(shim))}
$IdentityProbe = {ps_literal(str(identity_probe))}
$ExpectedShimSha256 = "{_RECOVERY_SHIM_SHA256}"
$BrokerConfig = {ps_literal(str(broker_config))}
$ManagerFile = {ps_literal(str(self.manager_path))}
$StatusFile = {ps_literal(str(self.status_path))}
$MarkerFile = {ps_literal(str(marker_path))}
$ExpectedSupervisorPid = {self.supervisor_pid}
$ExpectedSupervisorFileTime = {ps_literal(self.supervisor_creation_time_100ns)}
$ExpectedRunGeneration = {run_generation}
$ExpectedControllerGeneration = {controller_generation}
$ExpectedControllerPid = {controller_pid}
$ExpectedControllerFileTime = {ps_literal(controller_filetime)}
$ExpectedHostGeneration = {host_generation}
$AttachStatusMaxAgeMs = {_ATTACH_STATUS_MAX_AGE_MS}
$ExpectedKernelPid = {evaluation.kernel_pid}
$ExpectedKernelFileTime = {ps_literal(str(evaluation.kernel_filetime))}
function ConvertFrom-InventoryJson {{
    param([Parameter(Mandatory)][string]$Json)
    $converter = Get-Command ConvertFrom-Json -ErrorAction Stop
    if ($converter.Parameters.ContainsKey("DateKind")) {{
        return ConvertFrom-Json -InputObject $Json -DateKind String -ErrorAction Stop
    }}
    $datePrefix = "__QQCALL_CREATED_UTC__:"
    $protectedJson = [regex]::Replace($Json, '("createdUtc"\\s*:\\s*")([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}\\.[0-9]{{6}}Z)(")', ('$1' + $datePrefix + '$2$3'))
    $inventory = ConvertFrom-Json -InputObject $protectedJson -ErrorAction Stop
    foreach ($process in @($inventory.processes)) {{
        if ($process.createdUtc -is [string] -and $process.createdUtc.StartsWith($datePrefix, [StringComparison]::Ordinal)) {{
            $process.createdUtc = $process.createdUtc.Substring($datePrefix.Length)
        }}
    }}
    return $inventory
}}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{ throw "Run this script from an elevated PowerShell window." }}
$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$status = Get-Content -LiteralPath $StatusFile -Raw | ConvertFrom-Json
if ($status.phase -ne "needs_attachment" -or $status.ready -ne $false -or [long]$status.updatedAtMs -gt $now -or ($now - [long]$status.updatedAtMs) -gt $AttachStatusMaxAgeMs) {{ throw "The service generation is no longer awaiting attachment." }}
$manager = Get-Content -LiteralPath $ManagerFile -Raw | ConvertFrom-Json
if ([long]$manager.pid -ne $ExpectedSupervisorPid -or [string]$manager.creationTime100ns -ne $ExpectedSupervisorFileTime -or
    [long]$manager.runGeneration -ne $ExpectedRunGeneration -or [long]$manager.controllerGeneration -ne $ExpectedControllerGeneration -or [long]$manager.hostGeneration -ne $ExpectedHostGeneration -or
    [long]$manager.controllerPid -ne $ExpectedControllerPid -or [string]$manager.controllerCreationTime100ns -ne $ExpectedControllerFileTime -or
    [long]$manager.kernelPid -ne $ExpectedKernelPid -or [string]$manager.kernelCreationTime100ns -ne $ExpectedKernelFileTime -or
    [string]$manager.attachAttemptPath -ne $MarkerFile) {{ throw "The service controller generation changed." }}
$supervisorIdentity = (& $PythonExe $IdentityProbe $ExpectedSupervisorPid | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or [long]$supervisorIdentity.pid -ne $ExpectedSupervisorPid -or [string]$supervisorIdentity.creationTime100ns -ne $ExpectedSupervisorFileTime) {{ throw "The service controller process changed." }}
$controllerIdentity = (& $PythonExe $IdentityProbe $ExpectedControllerPid | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or [long]$controllerIdentity.pid -ne $ExpectedControllerPid -or [string]$controllerIdentity.creationTime100ns -ne $ExpectedControllerFileTime) {{ throw "The Bridge controller process changed." }}
$targetIdentity = (& $PythonExe $IdentityProbe $ExpectedKernelPid | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or [long]$targetIdentity.pid -ne $ExpectedKernelPid -or [string]$targetIdentity.creationTime100ns -ne $ExpectedKernelFileTime) {{ throw "The selected QQ process changed." }}
$actualHash = (Get-FileHash -LiteralPath $Shim -Algorithm SHA256).Hash
if ($actualHash -ne $ExpectedShimSha256) {{ throw "The fixed recovery shim hash changed." }}
$inventoryRaw = & $PythonExe $Injector --discover
if ($LASTEXITCODE -ne 0) {{ throw "QQ process discovery failed." }}
$inventory = ConvertFrom-InventoryJson -Json ($inventoryRaw -join "`n")
$selected = @($inventory.processes | Where-Object {{ $_.pid -eq $TargetPid -and $_.startFileTime -eq $ExpectedFileTime -and ($_.createdUtc -is [string]) -and ($_.createdUtc -ceq $ExpectedStart) -and [string]::Equals($_.imagePath, $ExpectedImage, [StringComparison]::OrdinalIgnoreCase) }})
if ($selected.Count -ne 1) {{ throw "The selected QQ process identity changed or is ambiguous." }}
$attempt = [ordered]@{{ schema = 1; supervisorPid = $ExpectedSupervisorPid; supervisorCreationTime100ns = $ExpectedSupervisorFileTime; runGeneration = $ExpectedRunGeneration; controllerGeneration = $ExpectedControllerGeneration; hostGeneration = $ExpectedHostGeneration; kernelPid = $ExpectedKernelPid; kernelCreationTime100ns = $ExpectedKernelFileTime; attemptedAtMs = $now }}
$attemptBytes = [Text.UTF8Encoding]::new($false).GetBytes(($attempt | ConvertTo-Json -Compress))
$markerTemp = $MarkerFile + "." + [guid]::NewGuid().ToString("N") + ".tmp"
$markerStream = [IO.File]::Open($markerTemp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try {{ $markerStream.Write($attemptBytes, 0, $attemptBytes.Length); $markerStream.Flush($true) }} finally {{ $markerStream.Dispose() }}
try {{ [IO.File]::Move($markerTemp, $MarkerFile) }} catch {{ Remove-Item -LiteralPath $markerTemp -Force -ErrorAction SilentlyContinue; throw "An attach attempt is already recorded for this generation." }}
& $PythonExe $Injector --pid $TargetPid --config $BrokerConfig --expected-start $ExpectedStart --recovery-shim --recovery-attempt 2 --inject
if ($LASTEXITCODE -ne 0) {{ throw "The requested attach failed; inspect the injector's fixed error code." }}
'''
        path = generation_dir / "admin" / "attach.ps1"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_private_json(path.with_suffix(".metadata.private.json"), {
            "schema": SCHEMA,
            "pid": evaluation.kernel_pid,
            "creationTime100ns": str(evaluation.kernel_filetime),
            "shimSha256": _RECOVERY_SHIM_SHA256,
        })
        from service.private_files import secure_directory

        secure_directory(path.parent)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(script)
            if os.name != "nt":
                path.chmod(0o600)
        except FileExistsError:
            raise ServiceError("attach_helper_already_exists", "quarantined") from None
        except OSError:
            raise ServiceError("attach_helper_write_failed") from None
        return path

    def _build_audio_route_config(self, evaluation: Evaluation, host_processes: list[dict[str, Any]], host_generation: int, directory: Path) -> str | None:
        if not self.config["audio"]["enabled"]:
            return None
        host_roots = [item for item in host_processes if item.get("role") == "host"]
        if len(host_roots) != 1:
            raise ServiceError("host_process_identity_unavailable", "quarantined")
        processes = [
            {"role": "host" if item["role"] == "host" else "child", "pid": item["pid"], "creationTime100ns": item["creationTime100ns"]}
            for item in host_processes if item["role"] in ("host", "host_child")
        ]
        if not processes or len(processes) > 128:
            raise ServiceError("host_process_tree_invalid", "quarantined")
        value = {
            "schema": 1,
            "hostGeneration": host_generation,
            "hostPid": host_roots[0]["pid"],
            "pythonExecutable": self.config["paths"]["python_exe"],
            "processes": processes,
            "captureRenderName": evaluation.guard_source["captureRenderName"] if evaluation.guard_source else "",
            "playbackRenderName": evaluation.guard_source["playbackRenderName"] if evaluation.guard_source else "",
            "loopbackReaders": [{"pid": evaluation.adapter_pid, "creationTime100ns": str(evaluation.adapter_filetime)}],
            "discoverChildren": True,
        }
        path = directory / "audio-route-config.private.json"
        atomic_private_json(path, value)
        return str(path)

    def _build_audio_plan(self, active: ActiveGeneration) -> dict[str, Any] | None:
        if not self.config["audio"]["enabled"]:
            return None
        source = active.evaluation.guard_source
        if source is None:
            raise ServiceError("audio_guard_config_missing", "quarantined")
        guard = source["guard"]
        return {
            "schema": 1,
            "accountId": self.config["account_id"],
            "serviceInstance": active.service_instance,
            "hostGeneration": active.host_generation,
            "connectionGeneration": active.connection_generation,
            "kernelPid": active.evaluation.kernel_pid,
            "kernelCreationTime100ns": str(active.evaluation.kernel_filetime),
            "captureEndpointId": guard["captureEndpointId"],
            "renderEndpointId": guard["renderEndpointId"],
            "audio": {
                "enabled": True,
                "capture_device": source["captureRenderName"] + " [loopback]",
                "playback_device": source["playbackRenderName"],
                "capture_mode": "loopback",
                "device_scope": {
                    "capture_render_name": source["captureRenderName"],
                    "playback_render_name": source["playbackRenderName"],
                },
            },
        }

    def _clear_runtime_snapshots(self) -> None:
        for path in (
            self.runtime_dir / "adapter-runtime.private.json",
            self.audio_plan_path,
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _start_bridge_process(
        self,
        evaluation: Evaluation,
        host_generation: int,
        connection_generation: int,
        host_pid: int,
        sidecar_path: str,
        directory: Path,
        peer_file: Path,
        audio_route_path: str | None,
        identity_source_path: Path | None,
        identity_output_path: Path | None,
    ) -> BridgeProcess:
        project = self.project_root
        cli = project / "bridge" / "cli.cjs"
        values = [
            "--pid", str(evaluation.kernel_pid),
            "--kernel-creation-time-100ns", str(evaluation.kernel_filetime),
            "--app-path", evaluation.app_path,
            "--token-file", sidecar_path,
            "--host-port", str(self.config["ports"]["host"]),
            "--output-port", str(self.config["ports"]["callback"]),
            "--host-generation", str(host_generation),
            "--host-pid", str(host_pid),
            "--connection-generation", str(connection_generation),
            "--control-port", str(self.config["ports"]["control"]),
            "--control-token-file", str(self.runtime_dir / "control.token.private"),
            "--account-uin", self.config["account_id"],
            "--kernel-peer-identities-path", str(peer_file),
            "--report", str(directory / "agent-report"),
            "--config", str(directory / "broker-config.private.json"),
            "--service-mode", "--auto-bootstrap",
        ]
        if evaluation.full_identity_path:
            values.extend(("--login-file", evaluation.full_identity_path))
        else:
            if identity_source_path is None or identity_output_path is None:
                raise ServiceError("identity_source_unavailable", "waiting_identity")
            values.extend(("--identity-source-file", str(identity_source_path), "--identity-output-file", str(identity_output_path)))
        if audio_route_path:
            values.extend(("--audio-route-config", audio_route_path))
        args = [self.config["paths"]["node_exe"], str(cli), *values]
        if Path(args[0]).is_absolute() and not Path(args[0]).is_file():
            raise ServiceError("node_executable_unavailable")
        try:
            process = subprocess.Popen(
                args,
                cwd=str(project),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            raise ServiceError("bridge_start_failed") from None
        return BridgeProcess(process)

    def _await_bridge_ready(self, active: ActiveGeneration, timeout_seconds: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._startup_checkpoint("bridge_callback_wait")
            event = active.bridge.wait_event(min(0.25, max(0.0, deadline - time.monotonic())))
            if event is None:
                if active.bridge.process.poll() is not None:
                    # The stderr reader may still be publishing a sanitized
                    # startup code after process exit.
                    event = active.bridge.wait_event(0.05)
                    if event is not None:
                        self._consume_bridge_event(active, event)
                        if active.bridge_startup_code:
                            raise ServiceError(active.bridge_startup_code, "error")
                    return False
                continue
            if event.get("kind") == "bridge_startup_error":
                self._consume_bridge_event(active, event)
                raise ServiceError(active.bridge_startup_code or "bridge_startup_failed", "error")
            if event.get("kind") == "bridge_ready":
                if (event.get("targetPid") != active.evaluation.kernel_pid or
                        event.get("hostGeneration") != active.host_generation or
                        event.get("connectionGeneration") != active.connection_generation or
                        not _valid_service_instance(event.get("controlServiceInstance"))):
                    return False
                active.service_instance = event["controlServiceInstance"]
                return True
            self._consume_bridge_event(active, event)
        return False

    def _host_report_failure(self, active: ActiveGeneration) -> str:
        """Read only a generation-bound terminal failure, never a positive ready proof."""
        path = Path(active.launch.report_path + ".host.status.json")
        if not path.exists():
            return ""
        try:
            report = read_private_json(path, max_bytes=16 * 1024)
        except (PrivateFileError, OSError):
            raise ServiceError("host_status_report_invalid", "quarantined") from None
        if (type(report) is not dict or type(report.get("hostGeneration")) is not int or
                report["hostGeneration"] != active.host_generation or type(report.get("targetPid")) is not int or
                report["targetPid"] != active.host_pid or type(report.get("phase")) is not str):
            raise ServiceError("host_status_binding_mismatch", "quarantined")
        if report["phase"] in {"failed", "renderer_failure", "output_failure", "stopped"}:
            code = report.get("lastFailureCode")
            return code if type(code) is str and code in _HOST_RUNTIME_FAILURE_CODES else "host_failed"
        return ""

    def _await_host_runtime_ready(self, active: ActiveGeneration, timeout_seconds: float = 52.0) -> None:
        """Require authenticated Host health and exact runtime process ownership."""
        deadline = time.monotonic() + timeout_seconds
        listener_owners = getattr(self.system, "listener_owners", None)
        process_identity = getattr(self.system, "process_identity", None)
        if listener_owners is None or process_identity is None:
            raise ServiceError("host_runtime_identity_unavailable", "quarantined")
        while time.monotonic() < deadline:
            self._startup_checkpoint("host_runtime_wait")
            while True:
                event = active.bridge.poll_event()
                if event is None:
                    break
                self._consume_bridge_event(active, event)
            if active.bridge_startup_code:
                raise ServiceError(active.bridge_startup_code, "error")
            if active.host_runtime_failure_code:
                raise ServiceError(active.host_runtime_failure_code, "error", diagnostic_stage=active.host_runtime_failure_stage)
            report_failure = self._host_report_failure(active)
            if report_failure:
                raise ServiceError(report_failure, "error", diagnostic_stage="host_terminal_report")
            if active.host_runtime_ready:
                try:
                    owners = listener_owners(self.config["ports"]["host"])
                except (WindowsServiceError, OSError):
                    raise ServiceError("host_listener_snapshot_unavailable", "quarantined") from None
                if owners and owners != [active.host_pid]:
                    raise ServiceError("host_listener_owner_mismatch", "quarantined")
                if owners == [active.host_pid]:
                    identity = process_identity(active.host_pid)
                    expected_hosts = [item for item in active.owned_hosts if item.get("role") == "host"]
                    expected_path = expected_hosts[0].get("imagePath") if len(expected_hosts) == 1 else None
                    if (identity is None or identity[0] != int(active.host_filetime) or
                            not isinstance(expected_path, str) or
                            ntpath.normcase(ntpath.normpath(identity[1])) != ntpath.normcase(ntpath.normpath(expected_path)) or
                            expected_hosts[0].get("pid") != active.host_pid or
                            expected_hosts[0].get("creationTime100ns") != active.host_filetime):
                        raise ServiceError("host_runtime_identity_mismatch", "quarantined")
                    return
            self.sleeper(min(0.1, max(0.0, deadline - time.monotonic())))
        raise ServiceError("host_runtime_ready_timeout", "error")

    def _write_generation_identity(self, active: ActiveGeneration) -> None:
        if not active.service_instance:
            raise ServiceError("control_service_instance_missing", "quarantined")
        if active.peer_snapshot_written:
            return
        self._startup_checkpoint("generation_identity_validate")
        self._clear_runtime_snapshots()
        if not self._binding_still_current(
            active.evaluation.manifest, active.evaluation.binding, explain_failure=True,
        ):
            raise ServiceError("target_binding_changed", "waiting_target")
        self._revalidate_audio_route(active.evaluation)
        self._startup_checkpoint("generation_identity_publish")
        snapshot = {
            "schema": SCHEMA,
            "accountId": self.config["account_id"],
            "serviceInstance": active.service_instance,
            "hostGeneration": active.host_generation,
            "connectionGeneration": active.connection_generation,
            "kernelPid": active.evaluation.kernel_pid,
            "kernelCreationTime100ns": str(active.evaluation.kernel_filetime),
            "peerMap": dict(active.evaluation.peer_map),
        }
        if self._auto_adapter:
            snapshot["adapterBinding"] = {
                "adapter": active.evaluation.manifest["adapter"],
                "pid": active.evaluation.manifest["adapterPid"],
                "creationTime100ns": active.evaluation.manifest["adapterCreationTime100ns"],
            }
        try:
            audio_plan = self._build_audio_plan(active)
            if audio_plan is not None:
                atomic_private_json(self.audio_plan_path, audio_plan)
            peer_path = active.directory / "kernel-peer-identities.private.json"
            peer_by_uin = {uin: uid for uid, uin in active.evaluation.peer_map.items()}
            atomic_private_json(peer_path, {
                "accountUin": self.config["account_id"],
                "targetPid": active.evaluation.kernel_pid,
                "connectionGeneration": active.connection_generation,
                "peerUidsByUin": peer_by_uin,
            })
            runtime_snapshot_path = self.runtime_dir / "adapter-runtime.private.json"
            atomic_private_json(runtime_snapshot_path, snapshot)
            if not self._binding_still_current(active.evaluation.manifest, active.evaluation.binding):
                raise ServiceError("target_binding_changed", "waiting_target")
            self._revalidate_audio_route(active.evaluation)
        except Exception:
            self._clear_runtime_snapshots()
            raise
        else:
            active.peer_snapshot_written = True

    def _start_generation(self, evaluation: Evaluation) -> ActiveGeneration:
        self._startup_checkpoint("startup_dependencies")
        self._validate_static_dependencies()
        self._ensure_private_runtime()
        read_or_create_private_token(self.runtime_dir / "control.token.private")
        self._startup_checkpoint("host_generation_prepare")
        host_generation = self._allocate_generation()
        connection_generation = self._allocate_generation()
        directory = self.runtime_dir / "generations" / f"connection-{connection_generation}"
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise ServiceError("generation_directory_exists", "quarantined") from None
        except OSError:
            raise ServiceError("generation_directory_unavailable") from None
        from service.private_files import secure_directory

        secure_directory(directory)
        launch: HostLaunch | None = None
        host_processes: list[dict[str, Any]] = []
        host_pid: int | None = None
        bridge: BridgeProcess | None = None
        active: ActiveGeneration | None = None
        try:
            guard_path = self._make_guard_for_generation(evaluation.guard_source, host_generation, directory)
            self._revalidate_audio_route(evaluation)
            if not self._binding_still_current(evaluation.manifest, evaluation.binding):
                raise ServiceError("target_binding_changed", "waiting_target")
            self._startup_checkpoint("host_launch")
            launch = self.system.launch_host(
                self.config,
                host_generation,
                evaluation.binding,
                guard_path,
                should_stop=self._stop_request_pending,
                on_progress=lambda: self._status("starting", "host_launch"),
            )
            self._startup_checkpoint("host_bootstrap_wait")
            host_processes, host_pid, sidecar, host_filetime = self.system.wait_host_ready(
                launch,
                self.config,
                timeout_seconds=20.0,
                should_stop=self._stop_request_pending,
                on_progress=lambda: self._status("starting", "host_bootstrap_wait"),
            )
            self._startup_checkpoint("bridge_callback_prepare")
            audio_route = self._build_audio_route_config(evaluation, host_processes, host_generation, directory)
            peer_file = directory / "kernel-peer-identities.private.json"
            peer_by_uin = {uin: uid for uid, uin in evaluation.peer_map.items()}
            atomic_private_json(peer_file, {
                "accountUin": self.config["account_id"],
                "targetPid": evaluation.kernel_pid,
                "connectionGeneration": connection_generation,
                "peerUidsByUin": peer_by_uin,
            })
            source_path = None
            output_path = None
            if evaluation.full_identity_path is None:
                source_path = directory / "identity-source.private.json"
                output_path = directory / "login-identity.private.json"
                atomic_private_json(source_path, evaluation.identity_source)
                if output_path.exists():
                    raise ServiceError("identity_output_already_exists", "quarantined")
            broker_config = directory / "broker-config.private.json"
            bridge = self._start_bridge_process(
                evaluation, host_generation, connection_generation, host_pid, sidecar, directory,
                peer_file, audio_route, source_path, output_path,
            )
            active = ActiveGeneration(
                evaluation, host_generation, connection_generation, directory, launch,
                host_processes, host_pid, host_filetime, sidecar, bridge,
            )
            self._startup_checkpoint("bridge_callback_wait")
            if not self._await_bridge_ready(active):
                raise ServiceError("bridge_startup_unconfirmed")
            self._await_host_runtime_ready(active)
            self._write_generation_identity(active)
            identity_reader = getattr(self.system, "process_identity", None)
            controller_identity = identity_reader(bridge.process.pid) if identity_reader else None
            if controller_identity is None:
                raise ServiceError("controller_identity_unavailable", "quarantined")
            marker_path = directory / "attach-attempt.private.json"
            manager_record = {
                **(self.run_identity or {}),
                "controllerGeneration": connection_generation,
                "controllerPid": bridge.process.pid,
                "controllerCreationTime100ns": str(controller_identity[0]),
                "hostGeneration": host_generation,
                "kernelPid": evaluation.kernel_pid,
                "kernelCreationTime100ns": str(evaluation.kernel_filetime),
                "attachAttemptPath": str(marker_path),
                "ownedHosts": host_processes,
            }
            atomic_private_json(self.manager_path, manager_record)
            admin_helper = self._write_attach_helper(
                evaluation, directory, broker_config, host_generation, connection_generation,
                bridge.process.pid, str(controller_identity[0]),
            )
            manager_record["attachHelper"] = str(admin_helper)
            atomic_private_json(self.manager_path, manager_record)
            return active
        except (ServiceError, WindowsServiceError, PrivateFileError, OSError) as exc:
            self._persist_controller_termination(
                directory=directory,
                bridge=bridge,
                host_generation=host_generation,
                host_pid=host_pid,
                host_report_path=launch.report_path if launch is not None else None,
                trigger_code="generation_start_failed",
                phase=exc.phase if isinstance(exc, ServiceError) else "error",
                failure_code=getattr(exc, "code", None),
                failure_stage=getattr(exc, "_diagnostic_stage", None) if isinstance(exc, ServiceError) else None,
            )
            cleanup_confirmed = launch is None
            if launch is not None:
                host_processes = self._refresh_owned_hosts(launch, host_processes)
            if bridge is not None:
                try:
                    detached = bridge.stop(timeout=2.0)
                except (OSError, ValueError, subprocess.SubprocessError):
                    detached = False
                if not detached:
                    self._quarantine_kernel(evaluation, "detach_unconfirmed")
                if launch is not None:
                    host_processes = self._refresh_owned_hosts(launch, host_processes)
            if host_processes:
                cleanup_confirmed, host_processes = self._cleanup_owned_hosts(launch, host_processes)
            elif launch is not None:
                cleanup_confirmed = False
            if launch is not None and not cleanup_confirmed:
                self._quarantine_kernel(evaluation, "detach_unconfirmed")
                self._persist_unconfirmed_host_cleanup(directory, host_generation, host_processes)
            self._clear_runtime_snapshots()
            raise

    def _latch_first_rejected_output(self, active: ActiveGeneration, status: Any) -> None:
        if active.first_rejected_output is not None or not isinstance(status, dict):
            return
        if status.get("attached") is not False:
            return
        if (type(status.get("hostGeneration")) is not int or
                status["hostGeneration"] != active.host_generation or
                type(status.get("connectionGeneration")) is not int or
                status["connectionGeneration"] != active.connection_generation):
            return

        candidate = status.get("firstRejectedOutput")
        expected_fields = {
            "command", "id", "hostGeneration", "connectionGeneration", "sequence", "reasonCode",
        }
        if not isinstance(candidate, dict) or set(candidate) != expected_fields:
            return

        def in_range(value: Any, minimum: int, maximum: int) -> bool:
            return type(value) is int and minimum <= value <= maximum

        if (not in_range(candidate["command"], 0, MAX_DIAGNOSTIC_INTEGER) or
                not in_range(candidate["id"], 0, MAX_DIAGNOSTIC_INTEGER) or
                not in_range(candidate["hostGeneration"], 1, MAX_SAFE_INTEGER) or
                candidate["hostGeneration"] != active.host_generation or
                not in_range(candidate["connectionGeneration"], 1, MAX_SAFE_INTEGER) or
                candidate["connectionGeneration"] != active.connection_generation or
                not in_range(candidate["sequence"], 1, MAX_DIAGNOSTIC_INTEGER) or
                candidate["reasonCode"] != "kernel_listener_not_attached"):
            return

        safe_record = {
            "command": candidate["command"],
            "id": candidate["id"],
            "hostGeneration": candidate["hostGeneration"],
            "connectionGeneration": candidate["connectionGeneration"],
            "sequence": candidate["sequence"],
            "reasonCode": "kernel_listener_not_attached",
        }
        active.first_rejected_output = safe_record
        private_record = {
            "schema": 1,
            "hostGeneration": active.host_generation,
            "connectionGeneration": active.connection_generation,
            "firstRejectedOutput": safe_record,
        }
        try:
            atomic_private_json_if_absent(active.directory / "startup-failure.private.json", private_record)
        except (PrivateFileError, OSError):
            # This diagnostic must not change the supervisor's startup or cleanup path.
            return

    def _consume_bridge_event(self, active: ActiveGeneration, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "agent_event" and isinstance(event.get("event"), dict):
            summary = event["event"]
            if summary.get("kind") == "bootstrap_status":
                if (summary.get("processPid") == active.evaluation.kernel_pid and
                        summary.get("connectionGeneration") == active.connection_generation and
                        type(summary.get("sessionFound")) is bool and type(summary.get("serviceAvailable")) is bool):
                    active.shim_consumed = True
                    self._quarantine_kernel(active.evaluation, "shim_consumed")
                    active.last_event = "matching_bootstrap"
                elif summary.get("processPid") == active.evaluation.kernel_pid:
                    active.shim_consumed = True
                    self._quarantine_kernel(active.evaluation, "shim_consumed")
                    active.last_event = "bootstrap_generation_mismatch"
            return
        if kind == "auto_login_identity_verified":
            result = event.get("result")
            if isinstance(result, dict) and result.get("accountPathVerified") is True:
                active.shim_consumed = True
                self._quarantine_kernel(active.evaluation, "shim_consumed")
                active.account_path_verified = True
                active.last_event = "identity_path_verified"
            return
        if kind == "auto_login_ready":
            result = event.get("result")
            if (isinstance(result, dict) and type(result.get("posted")) is bool and
                    result.get("loginState") == "ready"):
                active.login_ready = True
                active.last_event = "login_ready"
            return
        if kind == "auto_bootstrap_failed":
            code = event.get("code")
            active.bootstrap_failure_code = (
                code if type(code) is str and code in _AUTO_BOOTSTRAP_FAILURE_CODES else "auto_bootstrap_failed"
            )
            active.shim_consumed = True
            self._quarantine_kernel(active.evaluation, "shim_consumed")
            active.last_event = "bootstrap_failed"
            return
        if kind == "bridge_status":
            status = event.get("status")
            if isinstance(status, dict) and any(status.get(key) is True for key in (
                "providerInvalidated", "controllerInvalidated", "hostInvalidated", "stopped",
            )):
                active.host_runtime_failure_code = active.host_runtime_failure_code or "native_binding_invalidated"
                active.host_runtime_ready = False
            group = status.get("groupCall") if isinstance(status, dict) else None
            if isinstance(group, dict) and event.get("event") in {"group_joined", "group_idle", "group_ending", "group_ending_remote", "group_quarantined"}:
                room_generation = group.get("roomGeneration")
                state = group.get("state")
                if (type(room_generation) is int and room_generation >= 0
                        and type(state) is str and state in {"joined", "idle", "ending", "ending_remote", "quarantined"}
                        and group.get("hostGeneration") == active.host_generation
                        and group.get("connectionGeneration") == active.connection_generation):
                    try:
                        atomic_private_json(active.directory / "group-state.safe.json", {
                            "hostGeneration": active.host_generation, "connectionGeneration": active.connection_generation,
                            "roomGeneration": room_generation, "state": state, "observedAtMs": round(time.time() * 1000),
                        })
                    except Exception:
                        pass
            observation = event.get("status", {}).get("groupInvitationObservation") if isinstance(event.get("status"), dict) else None
            fields = ("kernelInvitePresent", "kernelCallerPresent", "callerCorrelated", "groupCorrelated", "addressedToSelf", "ticketPublished")
            if event.get("event") in {"group_invite_classified", "group_invite_uncorrelated", "invite_callback_unmatched"} and isinstance(observation, dict) and all(type(observation.get(key)) is bool for key in fields):
                try:
                    atomic_private_json(active.directory / "group-invite-classification.safe.json", {
                        "hostGeneration": active.host_generation, "observedAtMs": round(time.time() * 1000),
                        **{key: observation[key] for key in fields},
                    })
                except Exception:
                    pass  # Diagnostic persistence never participates in admission.
            if event.get("event") == "output_rejected":
                self._latch_first_rejected_output(active, event.get("status"))
            if event.get("event") == "detach_unconfirmed":
                active.shim_consumed = True
                self._quarantine_kernel(active.evaluation, "detach_unconfirmed")
                return
            if event.get("event") == "host_availability":
                status = event.get("status")
                if not isinstance(status, dict):
                    active.host_runtime_failure_code = active.host_runtime_failure_code or "host_status_invalid"
                    active.host_runtime_ready = False
                    return
                if (status.get("hostGeneration") != active.host_generation or
                        status.get("hostPid") != active.host_pid):
                    active.host_runtime_failure_code = active.host_runtime_failure_code or "host_status_binding_mismatch"
                    active.host_runtime_ready = False
                    return
                if type(status.get("hostAvailable")) is not bool:
                    active.host_runtime_failure_code = active.host_runtime_failure_code or "host_status_invalid"
                    active.host_runtime_ready = False
                    return
                was_ready = active.host_runtime_ready
                active.host_runtime_ready = status["hostAvailable"] and not active.host_runtime_failure_code
                if was_ready and not active.host_runtime_ready and not active.host_runtime_failure_code:
                    code = status.get("lastFailureCode")
                    active.host_runtime_failure_code = (
                        code if type(code) is str and code in _HOST_RUNTIME_FAILURE_CODES else "host_runtime_unavailable"
                    )
                    active.host_runtime_failure_stage = "host_availability"
            return
        if kind == "bridge_startup_error":
            code = event.get("code")
            active.bridge_startup_code = (
                code if type(code) is str and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", code)
                else "bridge_startup_failed"
            )
            return
        if kind == "host_failure":
            code = event.get("code")
            operation = event.get("operation")
            if type(operation) is str and operation in _HOST_BUSINESS_OPERATIONS:
                # A bounded SDK request timeout is not evidence of Host death.
                # Authenticated health loss and bound terminal reports remain
                # authoritative even when this business diagnostic is ignored.
                return
            # The Bridge also emits this diagnostic for bounded business
            # rejections (for example group_start_refused). Only transport or
            # Host runtime failures can terminate a previously ready Host.
            if (type(code) is str and code in _HOST_RUNTIME_FAILURE_CODES and
                    (code == "output_callback_not_ready" or active.host_runtime_ready)):
                if not active.host_runtime_failure_code:
                    active.host_runtime_failure_code = code
                    active.host_runtime_failure_stage = "host_health" if operation == "health" else "host_transport"
                active.host_runtime_ready = False
            return
        if kind == "service_output_error":
            active.last_event = "bridge_output_error"

    def _monitor_active(self, active: ActiveGeneration) -> str:
        marker_path = active.directory / "attach-attempt.private.json"
        if marker_path.exists() and not active.shim_consumed:
            active.shim_consumed = True
            self._quarantine_kernel(active.evaluation, "shim_consumed")
            active.last_event = "attach_attempt_recorded"
        while True:
            event = active.bridge.poll_event()
            if event is None:
                break
            self._consume_bridge_event(active, event)
        report_failure = self._host_report_failure(active)
        if report_failure and not active.host_runtime_failure_code:
            active.host_runtime_failure_code = report_failure
            active.host_runtime_failure_stage = "host_terminal_report"
            active.host_runtime_ready = False
        if active.host_runtime_failure_code:
            raise ServiceError(active.host_runtime_failure_code, "error", diagnostic_stage=active.host_runtime_failure_stage)
        if active.bridge.process.poll() is not None:
            if active.shim_consumed:
                self._quarantine_kernel(active.evaluation, "controller_failed")
                return "waiting_kernel_restart"
            return "bridge_exited"
        if active.adapter_suspended:
            # The adapter's old manifest is deliberately no longer authoritative.
            # Keep supervising the exact original QQ incarnation and native stack.
            if self.system.process_is_current(
                active.evaluation.kernel_pid, active.evaluation.kernel_filetime,
            ) is not True:
                return "binding_changed"
            if self.system.process_is_current(active.host_pid, int(active.host_filetime)) is not True:
                return "bridge_exited"
        elif not self._binding_still_current(active.evaluation.manifest, active.evaluation.binding):
            return "binding_changed"
        if active.account_path_verified and active.login_ready:
            return "ready"
        if active.bootstrap_failure_code:
            return "bootstrap_failed"
        if active.shim_consumed:
            return "waiting_login" if active.account_path_verified else "waiting_identity"
        return "needs_attachment"

    def _set_adapter_available(self, active: ActiveGeneration, available: bool) -> None:
        command = "adapter-resume" if available else "adapter-pause"
        if not active.bridge.send(command):
            raise ServiceError("adapter_gate_unconfirmed", "quarantined")
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            event = active.bridge.wait_event(min(0.1, deadline - time.monotonic()))
            if event is None:
                if active.bridge.process.poll() is not None:
                    break
                continue
            if event.get("command") == command:
                result = event.get("result")
                if isinstance(result, dict) and result.get("adapterAvailable") is available:
                    return
                break
            self._consume_bridge_event(active, event)
        raise ServiceError("adapter_gate_unconfirmed", "quarantined")

    def _suspend_adapter(self, active: ActiveGeneration) -> None:
        # The audio observer pins the adapter's exact loopback-reader process.
        # A dead adapter incarnation cannot be resumed with a replacement PID:
        # its frozen route proof and any in-flight call belong to the old owner.
        # Let the supervisor's existing error path tear down Bridge and owned
        # Host processes, while retaining transient transport recovery when the
        # original adapter is still alive (or cannot be authoritatively queried).
        if self.system.process_is_current(
            active.evaluation.adapter_pid, active.evaluation.adapter_filetime,
        ) is False:
            raise ServiceError("adapter_process_exited", "waiting_kernel_restart")
        if active.adapter_suspended:
            return
        self._clear_runtime_snapshots()
        active.peer_snapshot_written = False
        # Set before IPC: any failure here must remain closed until full cleanup.
        active.adapter_suspended = True
        self._set_adapter_available(active, False)

    def _resume_adapter(self, active: ActiveGeneration, evaluation: Evaluation) -> None:
        if active.evaluation.native_fingerprint != evaluation.native_fingerprint:
            raise ServiceError("adapter_recovery_identity_mismatch", "quarantined")
        self._suspend_adapter(active)
        if self._monitor_active(active) not in {"ready", "needs_attachment", "waiting_identity", "waiting_login"}:
            raise ServiceError("adapter_recovery_native_unavailable", "waiting_kernel_restart")
        active.evaluation = evaluation
        self._write_generation_identity(active)
        # Publication alone cannot enable requests: only the private stdin ACK
        # after all identity/route checks permits a new adapter lease.
        self._set_adapter_available(active, True)
        active.adapter_suspended = False

    def _cleanup_owned_hosts(self, launch: HostLaunch, records: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
        # Each pass keeps WindowsSystem's child-before-parent safety barrier and
        # its five-second bound. A second pass can finish the launcher after
        # already terminated children consumed the first pass's wait budget.
        for _attempt in range(2):
            if _attempt:
                records = self._refresh_owned_hosts(launch, records)
            try:
                if self.system.terminate_owned(records):
                    return True, records
            except (WindowsServiceError, OSError, ValueError, subprocess.SubprocessError):
                pass
        return False, records

    def _finish_stop_request(self) -> tuple[dict[str, Any], int]:
        self._status("stopping", "stop_requested")
        detached = self._stop_active(trigger_code="explicit_stop", phase="stopping")
        phase = "stopped" if detached else "quarantined"
        status = self._status(phase, "service_stopped" if detached else "detach_unconfirmed")
        return status, 0

    def _refresh_owned_hosts(
        self,
        launch: HostLaunch,
        previously_verified: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        refresh = getattr(self.system, "owned_host_processes", None)
        if refresh is None:
            return previously_verified
        try:
            current = refresh(launch, self.config)
        except Exception:
            return previously_verified
        previous_roots = [item for item in previously_verified if item.get("role") == "host"]
        current_roots = [item for item in current if item.get("role") == "host"]
        if previous_roots:
            if len(previous_roots) != 1 or len(current_roots) != 1:
                return previously_verified
            old, new = previous_roots[0], current_roots[0]
            old_image = old.get("imagePath")
            new_image = new.get("imagePath")
            if (old.get("pid") != new.get("pid") or
                    old.get("creationTime100ns") != new.get("creationTime100ns") or
                    not isinstance(old_image, str) or not isinstance(new_image, str) or
                    ntpath.normcase(ntpath.normpath(old_image)) != ntpath.normcase(ntpath.normpath(new_image))):
                return previously_verified
        elif len(current_roots) > 1:
            return previously_verified

        # Keep orphaned children from the first verified snapshot even when
        # they no longer appear under the live root. New records come only
        # from the exact generation-bound process-tree verifier above.
        merged = list(previously_verified)

        def identity_key(item: dict[str, Any]) -> tuple[Any, ...] | None:
            pid = item.get("pid")
            creation = item.get("creationTime100ns")
            image = item.get("imagePath")
            role = item.get("role")
            if (type(pid) is not int or type(creation) is not str or not creation.isdecimal() or
                    not isinstance(image, str) or not isinstance(role, str)):
                return None
            return role, pid, creation, ntpath.normcase(ntpath.normpath(image))

        merged_positions = {
            key: index
            for index, item in enumerate(merged)
            if (key := identity_key(item)) is not None
        }
        for item in current:
            key = identity_key(item)
            if key is None or key not in merged_positions:
                if key is not None:
                    merged_positions[key] = len(merged)
                merged.append(item)
                continue
            index = merged_positions[key]
            merged[index] = {**merged[index], **item}
        return merged

    @staticmethod
    def _persist_unconfirmed_host_cleanup(
        directory: Path,
        host_generation: int,
        records: list[dict[str, Any]],
    ) -> bool:
        if not records or not directory.is_dir():
            return False
        try:
            atomic_private_json(directory / "host-cleanup-unconfirmed.private.json", {
                "schema": SCHEMA,
                "hostGeneration": host_generation,
                "ownedHosts": records,
            })
        except (PrivateFileError, OSError, ValueError):
            return False
        return True

    def _persist_controller_termination(
        self,
        *,
        directory: Path,
        bridge: BridgeProcess | None,
        host_generation: int,
        host_pid: int | None,
        host_report_path: str | None,
        trigger_code: str,
        phase: str,
        failure_code: str | None = None,
        failure_stage: Any = None,
    ) -> bool:
        """Best-effort first-writer termination snapshot, before owned-process cleanup."""
        try:
            now_ms = _safe_diagnostic_timestamp(self.clock_ms())
            if now_ms is None:
                return False
            safe_trigger = trigger_code if trigger_code in _CONTROLLER_TERMINATION_TRIGGERS else "supervisor_stop_active"
            safe_phase = phase if phase in _PHASES else "error"
            record: dict[str, Any] = {
                "schema": 1,
                "observedAtMs": now_ms,
                "hostGeneration": host_generation if type(host_generation) is int and host_generation > 0 else None,
                "triggerCode": safe_trigger,
                "phase": safe_phase,
            }
            safe_failure_code = _safe_diagnostic_code(failure_code)
            if safe_failure_code:
                record["supervisorFailureCode"] = safe_failure_code
            record["supervisorFailureStage"] = (
                _safe_controller_failure_stage(safe_failure_code, failure_stage)
            )

            controller_poll: Any = None
            if bridge is not None:
                poll_observed = False
                try:
                    controller_poll = bridge.process.poll()
                    poll_observed = True
                except Exception:
                    pass
                if poll_observed and (controller_poll is None or
                        (type(controller_poll) is int and -0x80000000 <= controller_poll <= 0xFFFFFFFF)):
                    record["controllerPollReturnCode"] = controller_poll
                try:
                    known_bridge_code = _safe_diagnostic_code(
                        getattr(bridge, "last_failure_code", None) or getattr(bridge, "startup_code", None)
                    )
                except Exception:
                    known_bridge_code = None
                if known_bridge_code:
                    record["controllerFailureCode"] = known_bridge_code

            node_report_path = directory / "agent-report.controller-exit.private.json"
            try:
                node_report = read_private_json(node_report_path, max_bytes=4096)
            except (PrivateFileError, OSError, ValueError, TypeError):
                node_report = None
            node_origin = node_report.get("origin") if isinstance(node_report, dict) else None
            if (isinstance(node_report, dict) and
                    set(node_report) in (_CONTROLLER_EXIT_REPORT_FIELDS, _CONTROLLER_EXIT_REPORT_FIELDS | {"exceptionDetails"}) and
                    type(node_report.get("schema")) is int and node_report["schema"] == 1 and
                    node_report.get("errorClass") == "uncaught_exception" and
                    node_report.get("failureCode") == "controller_uncaught_exception" and
                    type(node_origin) is str and node_origin in _CONTROLLER_EXIT_ORIGINS and
                    _safe_diagnostic_timestamp(node_report.get("observedAtMs")) is not None and
                    _safe_diagnostic_timestamp(node_report.get("exitedAtMs")) is not None and
                    type(node_report.get("exitCode")) is int and 0 <= node_report["exitCode"] <= 255):
                record["controllerFailureCode"] = "controller_uncaught_exception"
                record["controllerExceptionOrigin"] = node_origin
                record["controllerExceptionObservedAtMs"] = node_report["observedAtMs"]
                record["controllerExitAtMs"] = node_report["exitedAtMs"]
                record["controllerObserverExitCode"] = node_report["exitCode"]
                details = validated_exception_details(node_report.get("exceptionDetails"))
                if details is not None:
                    record["controllerExceptionDetails"] = details

            if host_report_path and host_pid is not None:
                try:
                    host_report = read_private_json(Path(host_report_path + ".host.status.json"), max_bytes=16 * 1024)
                except (PrivateFileError, OSError, ValueError, TypeError):
                    host_report = None
                host_phase_value = host_report.get("phase") if isinstance(host_report, dict) else None
                if (isinstance(host_report, dict) and
                        type(host_report.get("hostGeneration")) is int and
                        host_report.get("hostGeneration") == host_generation and
                        type(host_report.get("targetPid")) is int and
                        host_report.get("targetPid") == host_pid):
                    host_phase = host_phase_value if type(host_phase_value) is str and host_phase_value in {
                        "starting", "ready", "failed", "renderer_failure", "output_failure", "stopped",
                    } else None
                    host_failure = _safe_diagnostic_code(host_report.get("lastFailureCode"))
                    if host_phase:
                        record["hostPhase"] = host_phase
                    if host_failure:
                        record["hostFailureCode"] = host_failure

            atomic_private_json_if_absent(directory / "supervisor-termination.private.json", record)
            return True
        except Exception:
            # Diagnostics must never affect cleanup or the supervisor's control outcome.
            return False

    def _wait_before_retry(self, seconds: float, status: dict[str, Any], poll_interval: float) -> bool:
        """Wait with fresh status timestamps and a prompt scoped-stop check."""
        deadline = time.monotonic() + max(0.0, seconds)
        phase = status["phase"]
        code = status.get("code", "")
        while True:
            if self._consume_stop_request():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            self.sleeper(min(poll_interval, remaining))
            self._status(phase, code)

    def _stop_active(
        self,
        *,
        trigger_code: str = "supervisor_stop_active",
        phase: str | None = None,
        failure_code: str | None = None,
        failure_stage: Any = None,
    ) -> bool:
        active = self.active
        if active is None:
            self._clear_runtime_snapshots()
            return True
        self._persist_controller_termination(
            directory=active.directory,
            bridge=active.bridge,
            host_generation=active.host_generation,
            host_pid=active.host_pid,
            host_report_path=active.launch.report_path,
            trigger_code=trigger_code,
            phase=phase or self.last_status.get("phase", "error"),
            failure_code=failure_code or active.bridge_startup_code or active.bootstrap_failure_code,
            failure_stage=failure_stage,
        )
        owned_hosts = self._refresh_owned_hosts(active.launch, active.owned_hosts)
        try:
            detached = active.bridge.stop()
        except (OSError, ValueError, subprocess.SubprocessError):
            detached = False
        # A failed detach is ambiguous even when no matching event was seen:
        # an attach could have reached the one-shot native shim before the
        # controller lost its acknowledgement. Keep this exact Kernel
        # incarnation quarantined until its FILETIME changes or it exits.
        if not detached:
            self._quarantine_kernel(active.evaluation, "detach_unconfirmed")
        owned_hosts = self._refresh_owned_hosts(active.launch, owned_hosts)
        stopped, owned_hosts = self._cleanup_owned_hosts(active.launch, owned_hosts)
        if not stopped:
            # A live, unqueryable child may outlast the Bridge and trigger the
            # SDK's natural exit path later. Keep this exact Kernel pinned out
            # of a new generation until its FILETIME changes or it exits.
            self._quarantine_kernel(active.evaluation, "detach_unconfirmed")
            self._persist_unconfirmed_host_cleanup(active.directory, active.host_generation, owned_hosts)
        self.active = None
        self._clear_runtime_snapshots()
        return detached and stopped

    def _validate_static_dependencies(self) -> None:
        if not self.config["paths"]["qq_exe"]:
            try:
                qq_exe = self.system.discover_qq_executable()
            except WindowsServiceError as exc:
                raise ServiceError(exc.code, "waiting_target") from None
            self.config["paths"]["qq_exe"] = qq_exe
            self._discovery_config["paths"]["qq_exe"] = qq_exe
        project = self.project_root
        loader = Path(self.config["paths"]["loader_root"])
        qq_exe = Path(self.config["paths"]["qq_exe"])
        python_exe = Path(self.config["paths"]["python_exe"])
        required = (
            loader / "NapCatWinBootMain.exe", loader / "NapCatWinBootHook.dll", loader / "qqnt.json",
            project / "bridge" / "cli.cjs", project / "scripts" / "launch-host.ps1",
            project / "scripts" / "qqcall_injector.py", qq_exe, python_exe,
        )
        if any(not path.is_file() for path in required):
            raise ServiceError("service_dependency_missing")
        node = self.config["paths"]["node_exe"]
        if Path(node).is_absolute():
            if not Path(node).is_file():
                raise ServiceError("node_executable_unavailable")
        elif shutil.which(node) is None:
            raise ServiceError("node_executable_unavailable")
        shim = project / "native" / "build" / "x64" / "QQCallRecoveryShim2.dll"
        if _sha256_file(shim) != _RECOVERY_SHIM_SHA256.lower():
            raise ServiceError("recovery_shim_hash_mismatch", "quarantined")

    def check(self) -> tuple[dict[str, Any], int]:
        if not self.config["enabled"]:
            return self._result_status("disabled", "service_disabled"), 0
        try:
            self._validate_private_runtime_location()
            self._validate_static_dependencies()
            return self._result_status("waiting_adapter", "configuration_valid"), 0
        except ServiceError as exc:
            return self._result_status(exc.phase, exc.code), 2

    def _stop_request_path(self) -> Path:
        return self.runtime_dir / "stop.request.private.json"

    def request_stop(self) -> tuple[dict[str, Any], int]:
        if not self.config["enabled"]:
            return {"ok": True, "phase": "not_running"}, 0
        try:
            self._validate_private_runtime_location()
            if not self.runtime_dir.exists():
                return {"ok": True, "phase": "not_running"}, 0
            if is_reparse_point(self.runtime_dir):
                return {"ok": False, "phase": "error", "code": "runtime_path_invalid"}, 2
            if is_reparse_point(self.manager_path) or is_reparse_point(self._stop_request_path()):
                return {"ok": False, "phase": "error", "code": "stop_request_binding_mismatch"}, 2
            if not self.manager_path.exists():
                return {"ok": True, "phase": "not_running"}, 0
            manager = read_private_json(self.manager_path, max_bytes=16 * 1024)
            if not self._valid_manager_record(manager):
                return {"ok": False, "phase": "error", "code": "manager_record_invalid"}, 2
            if not self._process_current(manager["pid"], int(manager["creationTime100ns"])):
                lock = ProcessLock(self.runtime_dir / "service.lock")
                if not lock.acquire():
                    return {"ok": False, "phase": "error", "code": "service_already_running"}, 2
                try:
                    self._recover_prior_attach_attempt()
                    recovered = self._recover_prior_owned_hosts()
                finally:
                    lock.release()
                if recovered:
                    return {"ok": True, "phase": "stopped", "code": "prior_hosts_released"}, 0
                return {"ok": True, "phase": "not_running"}, 0
            request = {
                "schema": SCHEMA,
                "requestId": str(uuid.uuid4()),
                "pid": manager["pid"],
                "creationTime100ns": manager["creationTime100ns"],
                "runGeneration": manager["runGeneration"],
                "requestedAtMs": self.clock_ms(),
            }
            path = self._stop_request_path()
            if path.exists():
                existing = read_private_json(path, max_bytes=4096)
                if self._request_matches(existing, manager):
                    return {"ok": True, "phase": "stop_requested"}, 0
                return {"ok": False, "phase": "error", "code": "stop_request_binding_mismatch"}, 2
            # A same-directory hard link publishes only a complete file and
            # atomically refuses to replace a prior request.
            if not atomic_private_json_if_absent(path, request):
                existing = read_private_json(path, max_bytes=4096)
                if self._request_matches(existing, manager):
                    return {"ok": True, "phase": "stop_requested"}, 0
                return {"ok": False, "phase": "error", "code": "stop_request_binding_mismatch"}, 2
            return {"ok": True, "phase": "stop_requested"}, 0
        except ServiceError as exc:
            return {"ok": False, "phase": exc.phase, "code": exc.code}, 2
        except (PrivateFileError, WindowsServiceError, OSError):
            return {"ok": False, "phase": "error", "code": "stop_request_unavailable"}, 2

    def _valid_manager_record(self, value: Any) -> bool:
        expected = {"schema", "pid", "creationTime100ns", "runGeneration", "startedAtMs"}
        return (
            isinstance(value, dict) and expected <= set(value) and type(value.get("schema")) is int and value["schema"] == SCHEMA and
            type(value.get("pid")) is int and 0 < value["pid"] <= 0x7FFFFFFF and
            type(value.get("creationTime100ns")) is str and re.fullmatch(r"[1-9][0-9]{0,19}", value["creationTime100ns"]) is not None and int(value["creationTime100ns"]) <= MAX_FILETIME and
            type(value.get("runGeneration")) is int and 0 < value["runGeneration"] <= MAX_SAFE_INTEGER and
            type(value.get("startedAtMs")) is int and 0 <= value["startedAtMs"] <= MAX_SAFE_INTEGER
        )

    def _process_current(self, pid: int, filetime: int) -> bool:
        method = getattr(self.system, "process_is_current", None)
        if method:
            return method(pid, filetime) is True
        if os.name == "nt":
            return WindowsSystem().process_is_current(pid, filetime) is True
        return pid == os.getpid()

    def _request_matches(self, value: Any, manager: dict[str, Any]) -> bool:
        return (
            isinstance(value, dict) and set(value) == {"schema", "requestId", "pid", "creationTime100ns", "runGeneration", "requestedAtMs"} and
            type(value.get("schema")) is int and value["schema"] == SCHEMA and
            type(value.get("pid")) is int and value["pid"] == manager["pid"] and
            type(value.get("creationTime100ns")) is str and value["creationTime100ns"] == manager["creationTime100ns"] and
            type(value.get("runGeneration")) is int and value["runGeneration"] == manager["runGeneration"] and
            type(value.get("requestId")) is str and _valid_service_instance(value["requestId"]) and
            type(value.get("requestedAtMs")) is int and 0 <= value["requestedAtMs"] <= MAX_SAFE_INTEGER
        )

    def _stop_request_pending(self) -> bool:
        path = self._stop_request_path()
        manager = self.run_identity
        if manager is None or is_reparse_point(path) or not path.exists():
            return False
        try:
            value = read_private_json(path, max_bytes=4096)
        except PrivateFileError:
            return False
        return self._request_matches(value, manager)

    def _startup_checkpoint(self, code: str) -> None:
        if self._stop_request_pending():
            raise ServiceError("service_stop_requested", "stopping")
        self._status("starting", code)

    def _consume_stop_request(self) -> bool:
        path = self._stop_request_path()
        if is_reparse_point(path) or not path.exists() or self.run_identity is None:
            return False
        try:
            value = read_private_json(path, max_bytes=4096)
        except PrivateFileError:
            return False
        manager = self.run_identity
        if self._request_matches(value, manager):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return True
        return False

    def _write_run_identity(self) -> None:
        run_generation = self._allocate_generation()
        self.run_identity = self._manager_identity(run_generation)
        self.supervisor_pid = self.run_identity["pid"]
        self.supervisor_creation_time_100ns = self.run_identity["creationTime100ns"]
        atomic_private_json(self.manager_path, self.run_identity)

    def run(self, *, once: bool = False) -> tuple[dict[str, Any], int]:
        self._console_logging = True
        if not self.config["enabled"]:
            self._status("disabled", "service_disabled", persist=False)
            return self.last_status, 0
        try:
            self._validate_static_dependencies()
            self._ensure_private_runtime()
            lock = ProcessLock(self.runtime_dir / "service.lock")
            if not lock.acquire():
                # A competing BAT/WebUI start must not overwrite the live
                # owner's status or make its attachment generation unusable.
                return self._status("error", "service_already_running", persist=False), 2
        except (ServiceError, PrivateFileError) as exc:
            code = getattr(exc, "code", "service_start_failed")
            phase = getattr(exc, "phase", "error")
            return self._status(phase, code), 2
        try:
            pid, creation = self._current_supervisor_identity()
            self.supervisor_pid, self.supervisor_creation_time_100ns = pid, creation
            self._recover_prior_attach_attempt()
            self._recover_prior_owned_hosts()
            self._write_run_identity()
            self._status("starting", "service_starting")
            try:
                self._ensure_universal_service()
            except ServiceError as exc:
                if exc.code == "service_stop_requested" and self._consume_stop_request():
                    return self._finish_stop_request()
                self.universal_service_status = {
                    "state": "unavailable",
                    "code": "universal_api_service_unavailable",
                    "reused": False,
                }
            except Exception:
                # Audio service startup is optional to the QQ control plane;
                # expose only a fixed component failure and keep supervision.
                self.universal_service_status = {
                    "state": "unavailable",
                    "code": "universal_api_service_unavailable",
                    "reused": False,
                }
            if self._consume_stop_request():
                return self._finish_stop_request()
            self._status("starting", "service_starting")
            supervision = self.config["supervision"]
            initial_delay = float(supervision["retry_initial_sec"])
            maximum_delay = float(supervision["retry_max_sec"])
            poll_delay = float(supervision["poll_interval_sec"])
            maximum_failures = int(supervision["max_failures_per_generation"])
            delay = initial_delay
            last_failure_key: tuple[Any, ...] | None = None
            while True:
                if self._consume_stop_request():
                    return self._finish_stop_request()
                try:
                    evaluation = self.evaluate()
                    if self.active is None:
                        if self._kernel_quarantined(evaluation):
                            status = self._status("waiting_kernel_restart", "fixed_shim_kernel_consumed")
                        else:
                            failure_key = evaluation.fingerprint
                            if failure_key != last_failure_key:
                                last_failure_key = failure_key
                                delay = initial_delay
                            failures = self.failures.get(failure_key, 0)
                            if failures >= maximum_failures:
                                status = self._status("error", "generation_failure_limit")
                            else:
                                try:
                                    self._validate_ports()
                                    self.active = self._start_generation(evaluation)
                                except Exception as exc:
                                    if getattr(exc, "code", None) == "service_stop_requested":
                                        self._consume_stop_request()
                                        return self._finish_stop_request()
                                    failures += 1
                                    self.failures[failure_key] = failures
                                    if len(self.failures) > 32:
                                        oldest = next(iter(self.failures))
                                        if oldest != failure_key:
                                            self.failures.pop(oldest, None)
                                    if failures >= maximum_failures:
                                        # Persist the triggering cause before the summary
                                        # replaces status.json, including a one-attempt limit.
                                        if isinstance(exc, ServiceError):
                                            self._status(exc.phase, exc.code)
                                        elif isinstance(exc, WindowsServiceError):
                                            self._status("error", exc.code)
                                        else:
                                            self._status("error", "service_runtime_unavailable")
                                        status = self._status("error", "generation_failure_limit")
                                    elif isinstance(exc, ServiceError):
                                        status = self._status(exc.phase, exc.code)
                                    elif isinstance(exc, WindowsServiceError):
                                        status = self._status("error", exc.code)
                                    else:
                                        status = self._status("error", "service_runtime_unavailable")
                                    if failures < maximum_failures and not once:
                                        should_continue = self._wait_before_retry(delay, status, poll_delay)
                                        if not should_continue:
                                            return self._finish_stop_request()
                                    delay = min(maximum_delay, delay * 2)
                                    if once:
                                        return status, 0 if status["phase"] in {
                                            "waiting_adapter", "waiting_login", "waiting_identity", "waiting_target",
                                            "waiting_kernel_restart", "needs_attachment", "ready", "quarantined",
                                            "stopping", "stopped",
                                        } else 2
                                    continue
                    elif (self.active.adapter_suspended or
                          self.active.evaluation.fingerprint != evaluation.fingerprint) and (
                              self.active.evaluation.native_fingerprint == evaluation.native_fingerprint
                          ):
                        self._resume_adapter(self.active, evaluation)
                    elif (self.active.evaluation.fingerprint != evaluation.fingerprint or
                          self.active.evaluation.native_fingerprint != evaluation.native_fingerprint):
                        old_active = self.active
                        old = old_active.evaluation
                        was_consumed = old_active.shim_consumed
                        stopped_cleanly = self._stop_active(
                            trigger_code="evaluation_changed", phase="waiting_target",
                        )
                        if self.active is not None:
                            raise ServiceError("controller_stop_unconfirmed", "waiting_kernel_restart")
                        same_kernel = old.kernel_pid == evaluation.kernel_pid and old.kernel_filetime == evaluation.kernel_filetime
                        if same_kernel and (was_consumed or not stopped_cleanly):
                            self._quarantine_kernel(old, "shim_consumed" if was_consumed else "detach_unconfirmed")
                            status = self._status("waiting_kernel_restart", "fixed_shim_kernel_consumed" if was_consumed else "detach_unconfirmed")
                            if once:
                                return status, 0
                        else:
                            with self._resolved_peers_lock:
                                self._resolved_peers.clear()
                            status = self._status("waiting_target", "adapter_generation_changed")
                    if self.active is not None and self.active.evaluation.fingerprint == evaluation.fingerprint:
                        lifecycle = self._monitor_active(self.active)
                        if lifecycle == "ready":
                            status = self._status("ready", "")
                            self.failures[evaluation.fingerprint] = 0
                            delay = initial_delay
                        elif lifecycle == "needs_attachment":
                            status = self._status("needs_attachment", "admin_attach_required")
                        elif lifecycle == "waiting_identity":
                            status = self._status("waiting_identity", "identity_verification_pending")
                        elif lifecycle == "waiting_login":
                            status = self._status("waiting_login", "native_login_pending")
                        elif lifecycle == "bootstrap_failed":
                            failure_code = self.active.bootstrap_failure_code
                            old = self.active.evaluation
                            self._stop_active(
                                trigger_code="bootstrap_failed", phase="error", failure_code=failure_code,
                            )
                            status = self._status("waiting_kernel_restart", failure_code or "auto_bootstrap_failed")
                            self._quarantine_kernel(old, "shim_consumed")
                        elif lifecycle == "waiting_kernel_restart":
                            old = self.active.evaluation
                            self._stop_active(
                                trigger_code="controller_exit", phase="waiting_kernel_restart",
                            )
                            self._quarantine_kernel(old, "shim_consumed")
                            status = self._status("waiting_kernel_restart", "fixed_shim_kernel_consumed")
                        elif lifecycle == "binding_changed" and self.system.process_is_current(
                            self.active.evaluation.kernel_pid, self.active.evaluation.kernel_filetime,
                        ) is True:
                            # Adapter restart can race the final evaluation and
                            # this monitor read. Close admission now, then let a
                            # fresh full evaluation decide the next incarnation.
                            self._suspend_adapter(self.active)
                            if self._monitor_active(self.active) not in {
                                "ready", "needs_attachment", "waiting_identity", "waiting_login",
                            }:
                                raise ServiceError("adapter_recovery_native_unavailable", "waiting_kernel_restart")
                            status = self._status("waiting_adapter", "target_binding_changed")
                        elif lifecycle in ("binding_changed", "bridge_exited"):
                            old_active = self.active
                            old = old_active.evaluation
                            was_consumed = old_active.shim_consumed
                            stopped_cleanly = self._stop_active(
                                trigger_code="binding_changed" if lifecycle == "binding_changed" else "controller_exit",
                                phase="waiting_target" if lifecycle == "binding_changed" else (
                                    "waiting_kernel_restart" if was_consumed else "waiting_target"
                                ),
                            )
                            same_kernel = old.kernel_pid == evaluation.kernel_pid and old.kernel_filetime == evaluation.kernel_filetime
                            if same_kernel and (was_consumed or not stopped_cleanly):
                                self._quarantine_kernel(old, "shim_consumed" if was_consumed else "detach_unconfirmed")
                                status = self._status("waiting_kernel_restart", "fixed_shim_kernel_consumed" if was_consumed else "detach_unconfirmed")
                            elif lifecycle == "bridge_exited":
                                key = evaluation.fingerprint
                                failures = self.failures.get(key, 0) + 1
                                self.failures[key] = failures
                                if failures >= maximum_failures:
                                    self._status("waiting_target", "controller_exited")
                                    status = self._status("error", "generation_failure_limit")
                                else:
                                    status = self._status("waiting_target", "controller_exited")
                                    if not once:
                                        should_continue = self._wait_before_retry(delay, status, poll_delay)
                                        if not should_continue:
                                            return self._finish_stop_request()
                                    delay = min(maximum_delay, delay * 2)
                            else:
                                status = self._status("waiting_target", "adapter_binding_changed")
                        else:
                            status = self._status("error", "service_state_invalid")
                except ServiceError as exc:
                    if self.active and exc.code in _ADAPTER_OUTAGE_CODES:
                        try:
                            self._suspend_adapter(self.active)
                            lifecycle = self._monitor_active(self.active)
                            if lifecycle not in {"ready", "needs_attachment", "waiting_identity", "waiting_login"}:
                                raise ServiceError("adapter_recovery_native_unavailable", "waiting_kernel_restart")
                            status = self._status("waiting_adapter", exc.code)
                        except ServiceError as recovery_error:
                            self._stop_active(
                                trigger_code="service_error", phase=recovery_error.phase,
                                failure_code=recovery_error.code,
                            )
                            status = self._status(recovery_error.phase, recovery_error.code)
                        except (WindowsServiceError, PrivateFileError, OSError):
                            self._stop_active(trigger_code="service_runtime_unavailable", phase="error")
                            status = self._status("error", "service_runtime_unavailable")
                    elif self.active:
                        self._stop_active(
                            trigger_code="service_error", phase=exc.phase, failure_code=exc.code,
                            failure_stage=exc._diagnostic_stage,
                        )
                        status = self._status(exc.phase, exc.code)
                    else:
                        status = self._status(exc.phase, exc.code)
                except (WindowsServiceError, PrivateFileError, OSError):
                    if self.active:
                        self._stop_active(
                            trigger_code="service_runtime_unavailable", phase="error",
                        )
                    status = self._status("error", "service_runtime_unavailable")
                if once:
                    return status, 0 if status["phase"] in ("disabled", "starting", "waiting_adapter", "waiting_login", "waiting_identity", "waiting_target", "waiting_kernel_restart", "needs_attachment", "ready", "quarantined", "stopping", "stopped") else 2
                self.sleeper(poll_delay)
        finally:
            if self.active:
                self._stop_active(
                    trigger_code="supervisor_finally", phase=self.last_status.get("phase", "error"),
                )
            lock.release()


def _valid_service_instance(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except (ValueError, AttributeError):
        return False


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supervise one verified QQCall Bridge for the selected OneBot account.")
    parser.add_argument("command", choices=("run", "check", "plan", "status", "stop"))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        try:
            config_path = Path(args.config).expanduser().resolve(strict=False)
            canonical_path = DEFAULT_CONFIG_PATH.expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise ConfigError("config_unavailable") from None
        if os.path.normcase(str(config_path)) != os.path.normcase(str(canonical_path)):
            raise ConfigError("config_path_noncanonical")
        config = load_config(config_path)
        supervisor = Supervisor(config)
        if args.command == "check":
            output, code = supervisor.check()
        elif args.command == "plan":
            output, code = supervisor.plan(), 0
        elif args.command == "status":
            if supervisor.status_path.is_symlink():
                output = supervisor._result_status("error", "status_unavailable")
            elif not supervisor.status_path.exists():
                output = supervisor._result_status("disabled" if not config["enabled"] else "waiting_adapter", "status_unavailable")
            else:
                try:
                    value = read_private_json(supervisor.status_path, max_bytes=4096)
                    required = {"schema", "phase", "ready", "updatedAtMs", "supervisorPid", "supervisorCreationTime100ns"}
                    expected = required
                    if isinstance(value, dict) and "code" in value:
                        expected.add("code")
                    if isinstance(value, dict) and "universalService" in value:
                        expected.add("universalService")
                    universal = value.get("universalService") if isinstance(value, dict) else None
                    safe_universal_codes = {
                        "universal_api_service_ready", "universal_api_service_unavailable",
                        "universal_api_auth_failed", "universal_api_health_invalid",
                        "universal_api_route_mismatch", "universal_api_url_unsupported",
                        "universal_api_https_autostart_unsupported", "universal_api_token_invalid",
                        "universal_api_token_mismatch", "universal_api_service_config_invalid",
                        "universal_api_service_cli_unavailable", "universal_api_start_lock_unavailable",
                        "universal_api_start_in_progress_timeout", "universal_api_start_process_exited",
                        "universal_api_start_timeout", "universal_api_start_cancelled",
                        "universal_api_process_cleanup_unconfirmed",
                    }
                    if isinstance(value, dict) and "universalService" in value and (
                        not isinstance(universal, dict)
                        or set(universal) != {"state", "code", "reused"}
                        or universal.get("state") not in ("ready", "unavailable", "not_checked")
                        or type(universal.get("code")) is not str
                        or (universal["code"] and universal["code"] not in safe_universal_codes)
                        or type(universal.get("reused")) is not bool
                        or (universal["state"] == "ready" and universal["code"] != "universal_api_service_ready")
                        or (universal["state"] == "not_checked" and universal["code"] != "")
                        or (universal["state"] == "unavailable" and not universal["code"])
                    ):
                        raise PrivateFileError("status_invalid")
                    if (not isinstance(value, dict) or set(value) != expected or type(value.get("schema")) is not int or value["schema"] != SCHEMA or
                            value.get("phase") not in _PHASES or type(value.get("ready")) is not bool or type(value.get("updatedAtMs")) is not int or
                            not 0 <= value["updatedAtMs"] <= MAX_SAFE_INTEGER or type(value.get("supervisorPid")) is not int or
                            not 0 <= value["supervisorPid"] <= 0x7FFFFFFF or type(value.get("supervisorCreationTime100ns")) is not str or
                            re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value["supervisorCreationTime100ns"]) is None or
                            ("code" in value and (type(value["code"]) is not str or re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value["code"]) is None)) or
                            value["ready"] != (value["phase"] == "ready" and "code" not in value)):
                        raise PrivateFileError("status_invalid")
                    if value["ready"] and not _status_supervisor_is_current(supervisor.system, value):
                        output = supervisor._result_status("error", "status_unavailable")
                    else:
                        output = value
                except PrivateFileError:
                    output = supervisor._result_status("error", "status_unavailable")
            code = 0
        elif args.command == "stop":
            output, code = supervisor.request_stop()
        else:
            if config["enabled"]:
                ensure_process_job()
            output, code = supervisor.run(once=args.once)
    except ConfigError as exc:
        output = {"ok": False, "phase": "error", "code": exc.code}
        code = 2
    except (ServiceError, PrivateFileError, ProcessJobError) as exc:
        output = {"ok": False, "phase": getattr(exc, "phase", "error"), "code": getattr(exc, "code", "service_error")}
        code = 2
    except Exception:
        output = {"ok": False, "phase": "error", "code": "service_error"}
        code = 2
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
