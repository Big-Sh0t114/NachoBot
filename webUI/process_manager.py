"""
NachoBot WebUI — Process Manager
Manages service subprocess lifecycles, log capture, and WebSocket broadcasting.
"""

import asyncio
import ctypes
import errno
import json
import locale
import logging
import ntpath
import os
import re
import secrets
import signal
import subprocess
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from ctypes import wintypes

import psutil

try:
    from .multimodal_runtime import MultimodalRuntimeManager
    from .qq_adapter_selector import (
        DEFAULT_QQ_ADAPTER,
        QQAdapterSelectorError,
        read_qq_adapter,
    )
except ImportError:
    from multimodal_runtime import MultimodalRuntimeManager
    from qq_adapter_selector import (
        DEFAULT_QQ_ADAPTER,
        QQAdapterSelectorError,
        read_qq_adapter,
    )

VRCHAT_CAPABILITY_ENV = "NACHOBOT_VRCHAT_CONTROL_TOKEN"
logger = logging.getLogger("webui.process_manager")

# Regex to strip ANSI escape sequences (colors, cursor moves, etc.)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

# Process-group shutdown is deliberately bounded. These values are shortened
# by isolated lifecycle tests without creating real subprocesses.
_PROCESS_GROUP_TERM_TIMEOUT = 10.0
_PROCESS_GROUP_KILL_TIMEOUT = 2.0
_PROCESS_REAP_TIMEOUT = 2.0
_PROCESS_GROUP_POLL_INTERVAL = 0.05
_WINDOWS_JOB_POLL_INTERVAL = 0.05
_CORE_DISPLAY_GRACE_SECONDS = 6.0
_CORE_LAST_VERIFIED_MAX_AGE_SECONDS = 15.0


class _WindowsJobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _WindowsJobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _WindowsJobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsJobBasicLimitInformation),
        ("IoInfo", _WindowsJobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_WINDOWS_TH32CS_SNAPTHREAD = 0x00000004
_WINDOWS_THREAD_SUSPEND_RESUME = 0x0002
_WINDOWS_THREAD_QUERY_INFORMATION = 0x0040
_WINDOWS_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


ROOT_DIR = Path(__file__).resolve().parent.parent

@dataclass
class _WindowsJobCapability:
    """Opaque manager-owned Windows Job Object capability."""

    handle: int
    closed: bool = False


class _WindowsJobFacade:
    """Small stdlib-only Win32 Job Object facade.

    The facade is never loaded on POSIX.  Keeping all ctypes declarations and
    calls here gives tests a narrow injectable seam and keeps module import
    safe on non-Windows hosts.
    """

    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_SUSPEND_RESUME = 0x0800

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]


    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable on this platform")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._bind("CreateJobObjectW", wintypes.HANDLE, [wintypes.LPVOID, wintypes.LPCWSTR], library=self.kernel32)
        self._bind(
            "SetInformationJobObject",
            wintypes.BOOL,
            [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD], library=self.kernel32,
        )
        self._bind("OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], library=self.kernel32)
        self._bind("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE], library=self.kernel32)
        self._bind("CloseHandle", wintypes.BOOL, [wintypes.HANDLE], library=self.kernel32)
        self._bind("TerminateProcess", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT], library=self.kernel32)
        self._bind("TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT], library=self.kernel32)
        self._bind(
            "QueryInformationJobObject",
            wintypes.BOOL,
            [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], library=self.kernel32,
        )
        self._bind("NtResumeProcess", ctypes.c_long, [wintypes.HANDLE], library=self.ntdll)

    @staticmethod
    def _bind(name: str, restype: Any, argtypes: list[Any], *, library: Any) -> None:
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = argtypes

    @staticmethod
    def _raise_last_error(message: str) -> None:
        error = ctypes.get_last_error()
        raise OSError(error, f"{message} (WinError {error})")

    @staticmethod
    def _handle_value(handle: Any) -> int:
        """Normalize ctypes HANDLE values without truncating Win64 pointers."""
        value = getattr(handle, "value", handle)
        if value is None:
            return 0
        return int(value)

    def _open_process(self, pid: int, access: int) -> int:
        handle = self.kernel32.OpenProcess(access, False, int(pid))
        if not handle:
            self._raise_last_error(f"OpenProcess({pid}) failed")
        return self._handle_value(handle)

    def _close_raw(self, handle: int) -> None:
        if handle:
            if not self.kernel32.CloseHandle(ctypes.c_void_p(handle)):
                self._raise_last_error("CloseHandle failed")

    def create_assign_resume(self, pid: int) -> _WindowsJobCapability:
        job_handle = self.kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            self._raise_last_error("CreateJobObjectW failed")
        capability = _WindowsJobCapability(self._handle_value(job_handle))
        process_handle = 0
        resume_handle = 0
        assigned = False
        uncertain_handles: list[tuple[str, int]] = []
        try:
            limits = _WindowsJobExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel32.SetInformationJobObject(
                ctypes.c_void_p(capability.handle),
                _WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                self._raise_last_error("SetInformationJobObject failed")
            process_handle = self._open_process(
                pid,
                self._PROCESS_TERMINATE | self._PROCESS_SET_QUOTA | self._PROCESS_QUERY_LIMITED_INFORMATION,
            )
            if not self.kernel32.AssignProcessToJobObject(
                ctypes.c_void_p(capability.handle), ctypes.c_void_p(process_handle)
            ):
                self._raise_last_error("AssignProcessToJobObject failed")
            assigned = True
            assigned_process_handle = process_handle
            process_handle = 0
            try:
                self._close_raw(assigned_process_handle)
            except Exception:
                uncertain_handles.append(("assigned process", assigned_process_handle))
                raise
            resume_handle = self._open_process(pid, self._PROCESS_SUSPEND_RESUME)
            try:
                status = self.ntdll.NtResumeProcess(ctypes.c_void_p(resume_handle))
                if status != 0:
                    raise OSError(status, "NtResumeProcess failed")
            finally:
                owned_resume_handle = resume_handle
                resume_handle = 0
                try:
                    self._close_raw(owned_resume_handle)
                except Exception:
                    uncertain_handles.append(("resume", owned_resume_handle))
                    raise
            return capability
        except Exception as setup_error:
            cleanup_errors: list[BaseException] = []
            if process_handle:
                owned_process_handle = process_handle
                process_handle = 0
                if not assigned:
                    try:
                        if not self.kernel32.TerminateProcess(ctypes.c_void_p(owned_process_handle), 1):
                            self._raise_last_error("TerminateProcess failed")
                    except Exception as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                try:
                    self._close_raw(owned_process_handle)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if resume_handle:
                owned_resume_handle = resume_handle
                resume_handle = 0
                try:
                    self._close_raw(owned_resume_handle)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            try:
                self.terminate(capability)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
            try:
                self.close(capability)
            except Exception as close_error:
                cleanup_errors.append(close_error)
            if uncertain_handles:
                setattr(setup_error, "uncertain_windows_handles", tuple(uncertain_handles))
            if cleanup_errors:
                setattr(setup_error, "windows_cleanup_errors", tuple(cleanup_errors))
            if not capability.closed:
                # A failed Job termination/close remains manager-owned.  The
                # caller must retain this capability and retry stop rather
                # than clearing process state and orphaning descendants.
                setattr(setup_error, "windows_job", capability)
            raise

    def terminate(self, capability: _WindowsJobCapability) -> None:
        if capability.closed:
            raise RuntimeError("Windows Job Object capability is already closed")
        if not self.kernel32.TerminateJobObject(ctypes.c_void_p(capability.handle), 1):
            self._raise_last_error("TerminateJobObject failed")

    def active_processes(self, capability: _WindowsJobCapability) -> int:
        if capability.closed:
            raise RuntimeError("Windows Job Object capability is already closed")
        info = self._BasicAccounting()
        returned = wintypes.DWORD()
        if not self.kernel32.QueryInformationJobObject(
            ctypes.c_void_p(capability.handle),
            _WINDOWS_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        ):
            self._raise_last_error("QueryInformationJobObject failed")
        return int(info.ActiveProcesses)

    def close(self, capability: _WindowsJobCapability) -> None:
        if capability.closed:
            return
        self._close_raw(capability.handle)
        capability.closed = True


def _read_tts_service_endpoint(root_dir: Path) -> str:
    """Resolve the public unified TTS Runtime endpoint on port 9880."""

    host = "127.0.0.1"
    config_path = root_dir / "NachoBot-Multimodal-Adapter" / "configs" / "base.toml"
    try:
        import tomlkit

        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        server = document.get("server", {})
        host = str(server.get("host", host)).strip() or host
    except Exception:
        pass
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:9880"


def _read_perception_service_endpoint(root_dir: Path) -> str:
    """Resolve the configured Core-facing 9874 perception endpoint."""

    host = "127.0.0.1"
    port = 9874
    config_path = root_dir / "NachoBot-Multimodal-Adapter" / "configs" / "perception.toml"
    try:
        import tomlkit

        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        perception = document.get("perception", {})
        host = str(perception.get("host", host)).strip() or host
        port = int(perception.get("port", port))
    except Exception:
        pass
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if not 1 <= port <= 65535:
        port = 9874
    return f"http://{host}:{port}"


class ServiceStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass(frozen=True)
class CoreObservation:
    """Ephemeral, non-owning evidence that an external Core is ready.

    This deliberately contains no PID, process handle, process-group ID, or
    Windows Job capability.  It is therefore safe to use for readiness while
    remaining useless for termination/adoption.
    """

    status: str
    observed_profile: str
    observed_at: float
    # ``None`` keeps the historical three-positional-argument construction
    # useful without treating an old observation as proof that any dependent
    # service is externally ready.
    observed_local_ready: bool | None = None
    perception_required: bool | None = None
    perception_ready: bool | None = None
    tts_required: bool | None = None
    tts_ready: bool | None = None

    @property
    def profile(self) -> str:
        """Compatibility alias for callers that refer to the profile plainly."""
        return self.observed_profile

    @property
    def local_ready(self) -> bool | None:
        """Compatibility alias for the retained Core ``observed_local`` state."""
        return self.observed_local_ready

    @property
    def readiness_known(self) -> bool:
        """Whether this observation contains the strict component contract."""
        return all(
            value is not None
            for value in (
                self.observed_local_ready,
                self.perception_required,
                self.perception_ready,
                self.tts_required,
                self.tts_ready,
            )
        )


@dataclass(frozen=True)
class _CoreProbeResult:
    observation: CoreObservation | None
    failure_kind: str | None = None


# Adapter process discovery is deliberately a separate, non-owning plane from
# ``ServiceState``.  The latter contains process handles and shutdown
# capabilities; these values are only a short-lived classification of what a
# BAT/manual launcher appears to have started.
EXTERNAL_ADAPTER_OUTCOMES = frozenset({
    "absent",
    "external_ready",
    "external_present_unready",
    "indeterminate",
})


@dataclass(frozen=True)
class AdapterObservation:
    service_id: str
    outcome: str
    observed_at: float
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in EXTERNAL_ADAPTER_OUTCOMES:
            raise ValueError(f"invalid external adapter outcome: {self.outcome}")

    @property
    def external_state(self) -> str | None:
        return {
            "external_ready": "ready",
            "external_present_unready": "present_unready",
            "indeterminate": "indeterminate",
        }.get(self.outcome)

    @property
    def present(self) -> bool:
        return self.outcome != "absent"


@dataclass(frozen=True)
class _ProcessRecord:
    pid: int
    ppid: int | None
    cwd: str | None
    argv: tuple[str, ...]
    executable: str | None
    name: str | None


@dataclass(frozen=True)
class _ProcessSnapshot:
    processes: tuple[_ProcessRecord, ...]
    listeners: Mapping[int, frozenset[int]]
    failed: bool = False


@dataclass
class ServiceDef:
    """Static definition of a launchable service."""
    id: str
    name: str
    group_id: str
    cwd: str                    # Relative to ROOT_DIR
    cmd: list[str]
    port: int | None = None
    env_extra: dict[str, str] = field(default_factory=dict)
    wait_port: bool = False     # Whether to wait for port before marking "running"
    order: int = 0              # Launch order within group
    detail: str = ""            # User-facing role/connection hint
    health_mode: str | None = None  # Optional /api/health mode required for readiness


@dataclass
class GroupDef:
    """Static definition of a launch group."""
    id: str
    name: str
    icon: str
    services: list[str]         # Service IDs in launch order
    detail: str = ""            # User-facing group description


# ---------------------------------------------------------------------------
# Service & Group definitions
# ---------------------------------------------------------------------------

SERVICE_DEFS: dict[str, ServiceDef] = {}
GROUP_DEFS: dict[str, GroupDef] = {}

# User-facing NachoBot launch profiles.  These remain backed by the existing
# groups so older group/service API callers continue to work unchanged.
LAUNCH_PROFILE_GROUPS: dict[str, str] = {
    "full": "tts_full",
    "lite": "tts_lite",
    "potato": "potato",
}

QQ_SERVICE_IDS = (
    "napcat_adapter",
    "snowluma_runtime",
    "snowluma_adapter",
    "napcat_shell",
)
EXTERNAL_ADAPTER_SERVICE_IDS = (
    "napcat_adapter",
    "napcat_shell",
    "snowluma_adapter",
    "snowluma_runtime",
    "bilibili",
    "live2d",
    "koishi",
    "koishi_adapter",
    "discordvc",
    "universalvc",
)
_EXTERNAL_PRESENCE_OUTCOMES = frozenset({
    "external_ready",
    "external_present_unready",
    "indeterminate",
})
_EXTERNAL_BLOCKING_OUTCOMES = frozenset({
    "external_present_unready",
    "indeterminate",
})
_EXTERNAL_READY_DETAIL = "由外部启动器运行"
_EXTERNAL_PRESENT_UNREADY_DETAIL = "检测到外部进程，但尚未通过就绪检查"
_EXTERNAL_INDETERMINATE_DETAIL = "无法确认外部进程归属或状态，请在原启动器中检查"
QQ_BUSY_STATUSES = frozenset(
    {ServiceStatus.STARTING, ServiceStatus.RUNNING, ServiceStatus.STOPPING}
)
_QQ_BACKEND_SERVICES = {
    "napcat": frozenset({"napcat_adapter", "napcat_shell"}),
    "snowluma": frozenset({"snowluma_runtime", "snowluma_adapter"}),
}
_QQ_SERVICE_BACKEND = {
    service_id: backend
    for backend, service_ids in _QQ_BACKEND_SERVICES.items()
    for service_id in service_ids
}
# Keep the start-boundary error independent from parser exception text.  The
# selector parser currently emits sanitized diagnostics, but a fixed message
# also keeps a future parser change from echoing .env content through the API.
QQ_ADAPTER_SELECTOR_START_ERROR = (
    "Cannot start QQ services: invalid qq_adapter configuration; "
    "correct the selector before starting a QQ adapter."
)
_QQ_ADAPTER_SELECTOR_ERROR: str | None = None
_LATEST_PROCESS_MANAGER: "ProcessManager | None" = None


def _qq_process_manager_instance() -> "ProcessManager | None":
    """Resolve the live WebUI manager lazily to avoid import cycles."""
    if _LATEST_PROCESS_MANAGER is not None:
        return _LATEST_PROCESS_MANAGER
    try:
        from . import server

        return getattr(server, "process_mgr", None)
    except Exception:
        try:
            import server

            return getattr(server, "process_mgr", None)
        except Exception:
            return None


def qq_backend_process_states(manager: "ProcessManager | None" = None) -> dict[str, ServiceStatus]:
    """Return managed QQ service states without exposing process credentials."""
    manager = manager or _qq_process_manager_instance()
    if manager is None:
        return {}
    return {
        service_id: manager.states.get(service_id, ServiceState()).status
        for service_id in QQ_SERVICE_IDS
    }


def assert_qq_adapter_switch_allowed(manager: "ProcessManager | None" = None) -> None:
    """Reject backend switches while managed or externally-present QQ is active."""
    manager = manager or _qq_process_manager_instance()
    states = qq_backend_process_states(manager)
    busy = [service_id for service_id, status in states.items() if status in QQ_BUSY_STATUSES]
    retained = [
        service_id
        for service_id in QQ_SERVICE_IDS
        if manager is not None
        and service_state_retains_runtime(manager.states.get(service_id))
    ]
    if busy or retained:
        raise ValueError("QQ 适配器正在运行或切换中，请先停止当前 QQ 服务")
    if manager is not None:
        external = getattr(manager, "adapter_observation_cache", {})
        if any(
            service_id in external
            and external[service_id].outcome in _EXTERNAL_PRESENCE_OUTCOMES
            for service_id in QQ_SERVICE_IDS
        ):
            raise ValueError("QQ 适配器检测到外部进程，请先在原启动窗口停止当前 QQ 服务")
    if manager is not None:
        operation_tasks = getattr(manager, "_operation_tasks", {})
        pending_keys = {f"service:{service_id}" for service_id in QQ_SERVICE_IDS}
        pending_keys.add("group:qq_adapter")
        if any(
            (operation_tasks.get(key) is not None)
            and not operation_tasks[key].done()
            for key in pending_keys
        ):
            raise ValueError("QQ 适配器正在运行或切换中，请先停止当前 QQ 服务")


def _register_services(root_dir: Path | str | None = None):
    """Build the service and group lookup tables by dynamically reading adapter configs."""
    global SERVICE_DEFS, GROUP_DEFS, _QQ_ADAPTER_SELECTOR_ERROR
    try:
        from .webui_config import webui_config
    except ImportError:  # pragma: no cover - direct script/module context
        from webui_config import webui_config
    import tomlkit
    import re

    base_root = Path(root_dir or ROOT_DIR).resolve()
    try:
        from .snowluma_locator import SnowLumaLocatorError, resolve_snowluma_runtime
    except ImportError:  # pragma: no cover - direct module/script context
        from snowluma_locator import SnowLumaLocatorError, resolve_snowluma_runtime

    runtime_info = None
    try:
        runtime_info = resolve_snowluma_runtime(base_root)
    except SnowLumaLocatorError:
        # Keep the registry inspectable when setup is incomplete.  Start-time
        # component validation remains fail-closed with the sanitized locator
        # error, so this fallback never launches an arbitrary directory.
        runtime_info = None
    runtime_path = runtime_info.path if runtime_info is not None else None
    runtime_name = runtime_info.name if runtime_info is not None else ""

    # 1. Parse NachoBot .env and resolve the selected QQ backend.  A malformed
    # selector is kept fail-closed for start operations; the registry still
    # remains inspectable so the WebUI can report both retained service IDs.
    _QQ_ADAPTER_SELECTOR_ERROR = None
    qq_adapter = DEFAULT_QQ_ADAPTER
    nachobot_host = "127.0.0.1"
    nachobot_port = 8000
    env_path = base_root / "NachoBot" / ".env"
    if env_path.exists():
        try:
            qq_adapter = read_qq_adapter(env_path)
        except QQAdapterSelectorError as exc:
            logger.error("Invalid NachoBot QQ adapter selector: %s", exc)
            _QQ_ADAPTER_SELECTOR_ERROR = QQ_ADAPTER_SELECTOR_START_ERROR
            qq_adapter = DEFAULT_QQ_ADAPTER
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip()
                    if k == "HOST":
                        nachobot_host = v
                    elif k == "PORT":
                        try:
                            nachobot_port = int(v)
                        except ValueError:
                            pass
        except Exception:
            pass

    # 2. Parse Napcat Adapter config.toml
    napcat_host = "127.0.0.1"
    napcat_port = 8095
    napcat_config_path = base_root / "NachoBot-Napcat-Adapter" / "config.toml"
    if napcat_config_path.exists():
        try:
            doc = tomlkit.parse(napcat_config_path.read_text(encoding="utf-8"))
            napcat_server = doc.get("napcat_server", {})
            napcat_host = napcat_server.get("host", napcat_host)
            napcat_port = int(napcat_server.get("port", napcat_port))
        except Exception:
            pass

    # SnowLuma's bundled runtime is managed by WebUI.  Its local WebSocket
    # endpoint is shown as detail text without ever reading or exposing the
    # access token.
    snowluma_host = "127.0.0.1"
    snowluma_port = 3001
    snowluma_path = "/"
    snowluma_config_path = base_root / "NachoBot-SnowLuma-Adapter" / "config.toml"
    if snowluma_config_path.exists():
        try:
            snow_doc = tomlkit.parse(snowluma_config_path.read_text(encoding="utf-8"))
            snow_section = snow_doc.get("snowluma", {})
            snowluma_host = str(snow_section.get("host", snowluma_host))
            snowluma_port = int(snow_section.get("port", snowluma_port))
            snowluma_path = str(snow_section.get("path", snowluma_path))
        except Exception:
            pass

    # SnowLuma's managed runtime owns the WebUI listener.  Keep this separate
    # from the adapter's OneBot WS port: the bridge does not listen on 3001.
    snowluma_webui_port = 5099
    snowluma_runtime_config = runtime_path / "config" / "runtime.json" if runtime_path else None
    if snowluma_runtime_config is not None and snowluma_runtime_config.exists():
        try:
            runtime_doc = json.loads(snowluma_runtime_config.read_text(encoding="utf-8"))
            if isinstance(runtime_doc, dict):
                candidate = int(runtime_doc.get("webuiPort", snowluma_webui_port))
                if 1 <= candidate <= 65535:
                    snowluma_webui_port = candidate
        except Exception:
            pass

    snowluma_core_port = 8000
    if snowluma_config_path.exists():
        try:
            snow_doc = tomlkit.parse(snowluma_config_path.read_text(encoding="utf-8"))
            snowluma_core_port = int(
                snow_doc.get("nachobot_server", {}).get("port", snowluma_core_port)
            )
        except Exception:
            pass
    # 3. Parse Perception configs/perception.toml
    perception_host = "127.0.0.1"
    perception_port = 9874
    perception_config_path = base_root / "NachoBot-Multimodal-Adapter" / "configs" / "perception.toml"
    if perception_config_path.exists():
        try:
            doc = tomlkit.parse(perception_config_path.read_text(encoding="utf-8"))
            percep_sec = doc.get("perception", {})
            perception_host = percep_sec.get("host", perception_host)
            perception_port = int(percep_sec.get("port", perception_port))
        except Exception:
            pass

    # 4. Parse standalone Live2D adapter config
    live2d_host = "127.0.0.1"
    live2d_port = 8766
    live2d_config_path = base_root / "NachoBot-Live2D-Adapter" / "config.toml"
    if live2d_config_path.exists():
        try:
            doc = tomlkit.parse(live2d_config_path.read_text(encoding="utf-8"))
            live2d_server = doc.get("server", {})
            live2d_host = live2d_server.get("host", live2d_host)
            live2d_port = int(live2d_server.get("port", live2d_port))
        except Exception:
            pass

    # 5. Parse Koishi configs/koishi.yml
    koishi_port = 5140
    koishi_yml_path = base_root / "koishi-app" / "koishi.yml"
    if koishi_yml_path.exists():
        try:
            content = koishi_yml_path.read_text(encoding="utf-8")
            server_idx = content.find("group:server:")
            if server_idx != -1:
                port_match = re.search(r'port:\s*(\d+)', content[server_idx:server_idx+200])
                if port_match:
                    koishi_port = int(port_match.group(1))
        except Exception:
            pass

    # Resolve "0.0.0.0" to "127.0.0.1" for env_extra wait_port matching
    nachobot_env_host = nachobot_host
    napcat_env_host = napcat_host

    koishi_command = (
        ["cmd", "/c", "corepack", "yarn", "start"]
        if os.name == "nt"
        else ["corepack", "yarn", "start"]
    )

    defs = [
        # ── Core ──
        ServiceDef("nachobot", "NachoBot Core", "core", "NachoBot",
                   ["uv", "run", "python", "bot.py"], port=nachobot_port,
                   wait_port=True, order=1,
                   env_extra={"HOST": nachobot_env_host, "PORT": str(nachobot_port)},
                   detail=f"核心消息总线 · :{nachobot_port}"),
                   
        # ── QQ ──
        ServiceDef("napcat_adapter", "NapCat 适配器", "qq_adapter", "NachoBot-Napcat-Adapter",
                   ["uv", "run", "python", "main.py"], port=napcat_port,
                   wait_port=True, order=1,
                   env_extra={"HOST": napcat_env_host, "PORT": str(napcat_port)},
                   detail=f"QQ 消息 WebSocket · :{napcat_port}"),
        ServiceDef("napcat_shell", "NapCat Shell", "qq_adapter", "NapCat.Shell",
                   ["cmd", "/c", "launcher-user.bat"], order=2,
                   detail="QQ 客户端与登录窗口"),
        ServiceDef(
            "snowluma_runtime",
            "SnowLuma Runtime",
            "qq_adapter",
            runtime_name,
            ["cmd", "/d", "/s", "/c", "launcher.bat"],
            port=snowluma_webui_port,
            wait_port=True,
            order=2,
            detail=(
                f"SnowLuma Runtime {runtime_name or '(自动发现)'} WebUI · :{snowluma_webui_port}"
            ),
        ),
        ServiceDef("snowluma_adapter", "SnowLuma 适配器", "qq_adapter",
                   "NachoBot-SnowLuma-Adapter", ["uv", "run", "python", "main.py"],
                     order=1, detail=(
                        f"SnowLuma Runtime WebSocket · ws://{snowluma_host}:{snowluma_port}"
                        f"{snowluma_path} · Core :{snowluma_core_port}"
                    )),

        # ── Multimodal FULL ──
        ServiceDef("tts_runtime_full", "统一 TTS Runtime", "tts_full",
                   "NachoBot-Multimodal-Adapter", [],
                   port=9880, wait_port=True, order=1,
                   detail="GPT-SoVITS / VoxCPM supervised runtime · :9880"),
        ServiceDef("perception", "多模态感知运行时（FULL）", "tts_full",
                   "NachoBot-Multimodal-Adapter",
                   ["uv", "run", "python", "-m", "nachobot_multimodal.api_server"],
                   port=perception_port, wait_port=True, order=2,
                   env_extra={"HOST": perception_host, "PORT": str(perception_port)},
                   detail=f"Core typed multimodal API · :{perception_port}"),

        # ── Multimodal LITE ──
        ServiceDef("tts_runtime_lite", "统一 TTS Runtime", "tts_lite",
                   "NachoBot-Multimodal-Adapter", [],
                   port=9880, wait_port=True, order=1,
                   detail="GPT-SoVITS / VoxCPM supervised runtime · :9880"),

        # POTATO intentionally has no Multimodal service definition.

        # ── Live2D ──
        ServiceDef("live2d", "Live2D 渲染适配器", "live2d", "NachoBot-Live2D-Adapter",
                   ["uv", "run", "python", "-m", "live2d_adapter", "--config", "config.toml"],
                   port=live2d_port, wait_port=True, order=1,
                   detail=f"独立渲染 WebSocket · :{live2d_port}"),

        # ── UniversalVC ──
        ServiceDef("universalvc", "UniversalVC 语音适配器", "universalvc",
                   "NachoBot-UniversalVC-Adapter", ["uv", "run", "python", "main.py"], order=1,
                   detail="进程音频捕获 / 实时 ASR → Core"),

        # ── VRChat ──
        # WebUI intentionally omits the explicit autonomy acknowledgement
        # flag. The guardian idles fail-closed; launch_vrchat.bat is the only
        # bundled path that can authorize motion.
        # Temporarily hidden from WebUI; restore these definitions to expose VRChat.
        # ServiceDef("vrchat_guardian", "VRChat Lease Guardian", "vrchat",
        #            "NachoBot-VRChat-Adapter", ["uv", "run", "python", "main.py", "--guardian"], order=1,
        #            detail="loopback lease guardian · OSC/AnyaDance zero-on-failure"),
        # ServiceDef("vrchat_adapter", "VRChat 语音适配器", "vrchat",
        #            "NachoBot-VRChat-Adapter", ["uv", "run", "python", "main.py"], order=2,
        #            detail="UniversalVC 音频/ASR/TTS → Core (voice/chat by default)"),

        # ── Bilibili ──
        ServiceDef("bilibili", "Bilibili 直播适配器", "bilibili", "NachoBot-Bilibili-Adapter",
                   ["uv", "run", "python", "main.py"], order=1,
                   detail="直播弹幕、评论与私信 → Core"),

        # ── Discord ──
        ServiceDef("koishi", "Koishi 框架", "discord", "koishi-app",
                   koishi_command, port=koishi_port, wait_port=True, order=1,
                   env_extra={"HTTPS_PROXY": webui_config.https_proxy,
                              "HTTP_PROXY": webui_config.http_proxy},
                   detail=f"Discord / OneBot 平台网关 · :{koishi_port}"),
        ServiceDef("koishi_adapter", "Koishi 适配器", "discord", "NachoBot-Koishi-Adapter",
                   ["uv", "run", "python", "main.py"], order=2,
                   detail="Koishi 消息桥接 → NachoBot Core"),
        ServiceDef("discordvc", "DiscordVC 语音适配器", "discord",
                   "NachoBot-DiscordVC-Adapter", ["uv", "run", "python", "main.py"], order=3,
                   detail="Discord 语音频道 → Core"),
    ]

    SERVICE_DEFS = {d.id: d for d in defs}

    groups = [
        GroupDef("core", "核心服务", "🧠", ["nachobot"],
                  "NachoBot Core 消息总线"),
        GroupDef(
            "qq_adapter",
            "QQ / SnowLuma" if qq_adapter == "snowluma" else "QQ / NapCat",
            "🐧",
             ["snowluma_adapter", "snowluma_runtime"]
             if qq_adapter == "snowluma"
             else ["napcat_adapter", "napcat_shell"],
            (
                "WebUI 先启动本地 SnowLuma 适配器，再启动托管 Runtime"
                if qq_adapter == "snowluma"
                else "QQ 消息适配器与 NapCat 客户端"
            ),
        ),
        GroupDef("tts_full", "多模态服务（FULL）", "🎙️",
                   ["tts_runtime_full", "perception"],
                   f"统一 TTS Runtime :9880 + :{perception_port} 本地感知"),
        GroupDef("tts_lite", "多模态服务（LITE）", "🎙️",
                   ["tts_runtime_lite"],
                   "统一 TTS Runtime :9880（感知走远程 API）"),
        GroupDef("potato", "核心模式（POTATO）", "🥔", [],
                   "仅启动 NachoBot Core；不启动本地 Multimodal 服务"),
        GroupDef("bilibili", "Bilibili 直播", "📺", ["bilibili"],
                  "直播弹幕、评论、私信与可选 Live2D 联动"),
        GroupDef("live2d", "Live2D 渲染", "🖼️", ["live2d"],
                  "独立 Live2D WebSocket 渲染服务"),
        GroupDef("discord", "Discord / Koishi", "💬",
                  ["koishi", "koishi_adapter", "discordvc"],
                  "Koishi 平台网关、文字适配器与 Discord 语音适配器"),
        GroupDef("universalvc", "UniversalVC 语音", "🎤", ["universalvc"],
                  "进程音频捕获、实时 ASR 与虚拟声卡输出"),
        # Temporarily hidden from WebUI; restore to expose the VRChat group.
        # GroupDef("vrchat", "VRChat 语音与安全控制", "orbit", ["vrchat_guardian", "vrchat_adapter"],
        #          "语音/聊天默认；独立 guardian 的 bounded follow/wander/stop"),
    ]
    GROUP_DEFS = {g.id: g for g in groups}


_register_services()


# ---------------------------------------------------------------------------
# Runtime state for a running service
# ---------------------------------------------------------------------------

@dataclass
class ServiceState:
    status: ServiceStatus = ServiceStatus.STOPPED
    process: asyncio.subprocess.Process | None = None
    pid: int | None = None
    # POSIX only: this is populated solely for a process spawned with
    # ``start_new_session=True`` and is never inferred from an arbitrary PID.
    process_group_id: int | None = None
    # Windows only: psutil handles captured before shutdown signals.  Keeping
    # these across a failed stop lets a retry reap descendants even after the
    # leader has disappeared from the process table.
    windows_owned_processes: list[Any] = field(default_factory=list, repr=False)
    windows_job: _WindowsJobCapability | None = field(default=None, repr=False)
    started_at: float | None = None
    started_port: int | None = None
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=10000))
    _read_task: asyncio.Task | None = None
    # Synchronous request boundary reservation.  This marker carries no
    # process or termination capability and is cleared before a real start.
    start_reservation: bool = False


def service_state_has_runtime_capability(state: ServiceState | None) -> bool:
    """Whether a state carries process identity or manager cleanup authority."""
    if state is None:
        return False
    return bool(
        state.process is not None
        or state.pid is not None
        or state.process_group_id is not None
        or state.windows_owned_processes
        or state.windows_job is not None
    )


def service_state_is_pure_start_reservation(state: ServiceState | None) -> bool:
    """Whether a queued request is still only a non-owning reservation."""
    return bool(
        state is not None
        and state.start_reservation
        and not service_state_has_runtime_capability(state)
    )


def service_state_retains_runtime(state: ServiceState | None) -> bool:
    """Return whether manager-owned runtime capability still needs cleanup.

    A terminal ``ERROR`` status is not by itself proof that a child is still
    alive.  The manager deliberately retains the process handle/group/job
    capability when shutdown or output cleanup is incomplete, however, and
    selector switches must remain fenced until that capability is stopped.
    Keep this predicate side-effect free so all QQ guards use the same
    ownership semantics as the start/stop lifecycle.
    """
    if state is None:
        return False
    process = state.process
    if process is not None and getattr(process, "returncode", None) is None:
        return True
    return (
        state.process_group_id is not None
        or bool(state.windows_owned_processes)
        or state.windows_job is not None
    )


class ProcessManager:
    """Manages subprocess lifecycle and log broadcasting."""

    def __init__(self, root_dir: Path | None = None):
        global _LATEST_PROCESS_MANAGER
        self.root = root_dir or ROOT_DIR
        self.states: dict[str, ServiceState] = {}
        self._ws_subscribers: dict[str, list[Callable]] = {}  # service_id -> [callback]
        self._all_subscribers: list[Callable] = []  # "all" channel
        # Ephemeral per-group capability values are held only in memory and
        # injected into the paired VRChat processes. They are never logged or
        # written to TOML/config files.
        self._active_group_env: dict[str, dict[str, str]] = {}
        self._operation_tasks: dict[str, asyncio.Task[None]] = {}
        self._operation_kinds: dict[str, str] = {}
        self._service_locks: dict[str, asyncio.Lock] = {}
        # Runtime selected for the current/next NachoBot launch transaction.
        # FULL/LITE use gpu|cpu; POTATO does not select a Multimodal runtime.
        self._launch_runtime: str = "gpu"
        # Product profile carried by the manager-owned Core process. ``None``
        # means the process was started outside a profile transaction (or its
        # profile is unknown); launch requests fail closed by restarting it.
        self._core_runtime_profile: str | None = None
        # Verified health evidence for a Core started outside this manager.
        # This is intentionally separate from ``states`` and ``ServiceState``
        # so it can never acquire manager-owned termination authority.
        self.core_observation: CoreObservation | None = None
        self._core_probe_task: asyncio.Task[_CoreProbeResult] | None = None
        self._core_probe_generation = 0
        self._core_probe_task_generation = 0
        # UI evidence is deliberately kept out of ``core_observation``. A
        # short transport outage may preserve the last displayed state, but
        # start/stop preflights continue to use only the strict observation.
        self._last_verified_external_core: CoreObservation | None = None
        self._last_verified_external_core_at: float | None = None
        self._core_display_grace_until: float | None = None
        self._core_last_probe_failure_kind: str | None = None
        # External adapter observations never carry process handles or PIDs.
        # They are refreshed as one psutil snapshot and discarded/replaced as
        # a unit so a stale partial scan cannot authorize a later start.
        self.adapter_observation_cache: dict[str, AdapterObservation] = {}
        # Compatibility aliases make the cache discoverable to existing
        # diagnostics without exposing a second mutable source of truth.
        self.external_adapter_observations = self.adapter_observation_cache
        self._adapter_probe_task: asyncio.Task[dict[str, AdapterObservation]] | None = None
        self._adapter_observation_generation = 0
        self._adapter_probe_task_generation = 0
        self._adapter_start_preflight: dict[str, AdapterObservation] = {}
        # ``start_launch`` performs its own race-closing external probe.  The
        # marker lets the nested group start reuse that verified result while
        # direct group/service entrypoints still perform their own fresh probe.
        self._external_profile_start_verified: str | None = None
        # Lazy so importing/testing on non-Windows never loads Win32 DLLs.
        self._windows_job_facade: _WindowsJobFacade | Any | None = None
        _LATEST_PROCESS_MANAGER = self

    def _get_windows_job_facade(self) -> _WindowsJobFacade | Any:
        if self._windows_job_facade is None:
            self._windows_job_facade = _WindowsJobFacade()
        return self._windows_job_facade

    # ---- external Core observation ---------------------------------

    def _manager_core_takes_precedence(self) -> bool:
        """Whether a manager-owned Core state masks external evidence."""
        state = self.states.get("nachobot")
        if state is None:
            return False
        if service_state_is_pure_start_reservation(state):
            return False
        return state.status in {
            ServiceStatus.STARTING,
            ServiceStatus.RUNNING,
            ServiceStatus.STOPPING,
            ServiceStatus.ERROR,
        } or service_state_retains_runtime(state)

    def _external_core_observation(self) -> CoreObservation | None:
        """Return external evidence only when no manager state owns Core."""
        if self._manager_core_takes_precedence():
            return None
        return self.core_observation

    def _external_core_display_observation(self) -> CoreObservation | None:
        """Return strict external health or a still-bounded UI-only grace value."""
        if self._manager_core_takes_precedence():
            return None
        if self.core_observation is not None:
            return self.core_observation
        observation = self._last_verified_external_core
        deadline = self._core_display_grace_until
        if (
            observation is not None
            and self._core_last_probe_failure_kind == "transport"
            and deadline is not None
            and time.monotonic() < deadline
        ):
            return observation
        return None

    def _external_core_transport_uncertain(self) -> bool:
        """Whether a recent external Core's transport failure needs a process check."""
        observed_at = self._last_verified_external_core_at
        return bool(
            not self._manager_core_takes_precedence()
            and self.core_observation is None
            and self._last_verified_external_core is not None
            and self._core_last_probe_failure_kind == "transport"
            and observed_at is not None
        )

    def _external_core_process_presence(self, snapshot: _ProcessSnapshot) -> str:
        """Classify a previously verified Core during a transport outage.

        This process check runs only at mutation preflight, not for UI polling.
        Exact project cwd and entry-point checks avoid treating another Python
        process as Core; any listener on the configured Core port is still a
        conflict even when its owner cannot be identified.
        """
        if snapshot.failed:
            return "indeterminate"
        service = SERVICE_DEFS.get("nachobot")
        expected_cwd = self._external_service_cwd("nachobot")
        if service is None or expected_cwd is None:
            return "indeterminate"

        targets = {
            self._normalize_external_path(entrypoint, expected_cwd)
            for entrypoint in ("bot.py", "main.py")
        }
        for record in snapshot.processes:
            if self._normalize_external_path(record.cwd) != expected_cwd:
                continue
            if not self._record_has_python(record):
                continue
            if any(self._argv_path_matches(token, target, record.cwd)
                   for target in targets for token in record.argv):
                return "present"

        if service.port and int(service.port) in snapshot.listeners:
            return "present"
        return "absent"

    async def _resolve_uncertain_external_core_before_start(self) -> None:
        """Block replacement while a prior external Core may still be alive."""
        if not self._external_core_transport_uncertain():
            return
        snapshot = await asyncio.to_thread(self._build_external_process_snapshot)
        presence = self._external_core_process_presence(snapshot)
        if presence != "absent":
            raise RuntimeError(
                "外部 NachoBot Core 的健康连接暂不可用，且进程或端口仍存在/无法确认，拒绝启动替代服务"
            )
        # The full process/port snapshot confirmed disappearance, so the old
        # evidence must not block a later, legitimate WebUI start.
        self._last_verified_external_core = None
        self._last_verified_external_core_at = None
        self._core_display_grace_until = None
        self._core_last_probe_failure_kind = None

    @staticmethod
    def _profile_component_service_ids(profile_id: str) -> dict[str, str]:
        if profile_id == "full":
            return {
                "tts": "tts_runtime_full",
                "perception": "perception",
            }
        if profile_id == "lite":
            return {"tts": "tts_runtime_lite"}
        return {}

    def _external_ready_service_ids(
        self,
        profile_id: str,
        observation: CoreObservation | None = None,
    ) -> frozenset[str]:
        """Return only dependency IDs proven ready by external Core health."""
        observation = observation or self._external_core_observation()
        if (
            observation is None
            or observation.observed_profile != profile_id
            or not observation.readiness_known
        ):
            return frozenset()
        readiness = {
            "tts": observation.tts_required is True and observation.tts_ready is True,
            "perception": (
                observation.perception_required is True
                and observation.perception_ready is True
            ),
        }
        ready_ids = {
            service_id
            for component, service_id in self._profile_component_service_ids(profile_id).items()
            if readiness.get(component, False)
        }
        # A manager-owned dependent remains authoritative even if an external
        # Core reports a matching local component at the same time.
        return frozenset(
            service_id
            for service_id in ready_ids
            if not (
                (state := self.states.get(service_id)) is not None
                and not service_state_is_pure_start_reservation(state)
                and (
                    state.status != ServiceStatus.STOPPED
                    or service_state_retains_runtime(state)
                )
            )
        )

    def _external_profile_is_ready(
        self,
        profile_id: str,
        observation: CoreObservation | None = None,
    ) -> bool:
        """Whether the strict external contract proves every profile dependency."""
        observation = observation or self._external_core_observation()
        if observation is None or observation.observed_profile != profile_id:
            return False
        expected = set(self._profile_component_service_ids(profile_id).values())
        if not observation.readiness_known:
            return False
        return (
            observation.status == "ok"
            and observation.observed_local_ready is True
            and self._external_ready_service_ids(profile_id, observation) == expected
        )

    @staticmethod
    def _core_probe_failure_kind(error: BaseException) -> str:
        """Return transport only for socket/timeouts, never HTTP/auth/schema errors."""
        from urllib.error import HTTPError, URLError

        if isinstance(error, HTTPError):
            return "invalid"
        if isinstance(error, URLError):
            return "transport" if isinstance(error.reason, (OSError, TimeoutError)) else "invalid"
        if isinstance(error, (OSError, TimeoutError)):
            return "transport"
        return "invalid"

    async def _probe_core_observation(self) -> _CoreProbeResult:
        """Probe the configured Core health contract without blocking asyncio."""
        # Keep resolution in tts_manager so Core host/port and bearer-token
        # precedence cannot drift between chat/TTS and process readiness.
        try:
            try:
                from .tts_manager import (
                    CORE_HEALTH_TIMEOUT_SECONDS,
                    TTSManager,
                    _get_core_auth_token,
                    _get_core_base_url,
                )
            except ImportError:  # pragma: no cover - direct module context
                from tts_manager import (
                    CORE_HEALTH_TIMEOUT_SECONDS,
                    TTSManager,
                    _get_core_auth_token,
                    _get_core_base_url,
                )

            base_url = _get_core_base_url()
            token = _get_core_auth_token()
            if not isinstance(base_url, str) or not base_url.strip():
                return _CoreProbeResult(None, "invalid")
            if not isinstance(token, str):
                return _CoreProbeResult(None, "invalid")
            payload = await asyncio.to_thread(
                TTSManager._request_json,
                f"{base_url.rstrip('/')}/api/multimodal/health",
                CORE_HEALTH_TIMEOUT_SECONDS,
                token,
            )
        except Exception as exc:
            # Probe failures are expected while an external Core is starting or
            # stopping.  Do not log exception text: URL/config errors can
            # accidentally carry credential-bearing details.
            return _CoreProbeResult(None, self._core_probe_failure_kind(exc))

        if not isinstance(payload, Mapping):
            return _CoreProbeResult(None, "invalid")
        status = payload.get("status")
        profile = payload.get("desired_profile")
        capabilities = payload.get("capabilities")
        observed_local = payload.get("observed_local")
        if not isinstance(status, str) or status not in {"ok", "degraded"}:
            return _CoreProbeResult(None, "invalid")
        if not isinstance(profile, str) or profile not in {"full", "lite", "potato"}:
            return _CoreProbeResult(None, "invalid")
        if not isinstance(capabilities, Mapping):
            return _CoreProbeResult(None, "invalid")
        # The Core contract always publishes the TTS capability.  Do not
        # coerce strings/numbers here: a spoof-like health response must fail
        # closed rather than accidentally authorize a local launch decision.
        tts_capability = capabilities.get("tts")
        if type(tts_capability) is not bool:
            return _CoreProbeResult(None, "invalid")
        if tts_capability is not (profile != "potato"):
            return _CoreProbeResult(None, "invalid")

        if not isinstance(observed_local, Mapping):
            return _CoreProbeResult(None, "invalid")
        if observed_local.get("profile") != profile:
            return _CoreProbeResult(None, "invalid")
        observed_ready = observed_local.get("ready")
        perception = observed_local.get("perception")
        tts = observed_local.get("tts")
        if type(observed_ready) is not bool:
            return _CoreProbeResult(None, "invalid")
        if not isinstance(perception, Mapping) or not isinstance(tts, Mapping):
            return _CoreProbeResult(None, "invalid")
        perception_required = perception.get("required")
        perception_ready = perception.get("ready")
        tts_required = tts.get("required")
        tts_ready = tts.get("ready")
        if any(
            type(value) is not bool
            for value in (
                perception_required,
                perception_ready,
                tts_required,
                tts_ready,
            )
        ):
            return _CoreProbeResult(None, "invalid")

        required_flags = {
            "full": (True, True),
            "lite": (False, True),
            "potato": (False, False),
        }[profile]
        if (perception_required, tts_required) != required_flags:
            return _CoreProbeResult(None, "invalid")
        expected_ready = (
            (not perception_required or perception_ready)
            and (not tts_required or tts_ready)
        )
        if observed_ready != expected_ready:
            return _CoreProbeResult(None, "invalid")
        if status != ("ok" if expected_ready else "degraded"):
            return _CoreProbeResult(None, "invalid")
        return _CoreProbeResult(
            CoreObservation(
                status=status,
                observed_profile=profile,
                observed_at=time.time(),
                observed_local_ready=observed_ready,
                perception_required=perception_required,
                perception_ready=perception_ready,
                tts_required=tts_required,
                tts_ready=tts_ready,
            )
        )

    async def refresh_core_observation(
        self,
        *,
        force: bool = True,
    ) -> CoreObservation | None:
        """Refresh verified external-Core evidence asynchronously.

        Concurrent status polls share one in-flight probe. A failed probe
        always clears the strict observation; only a recent transport failure
        can preserve a separate, short-lived display snapshot.
        """
        if not force and self.core_observation is not None:
            return self.core_observation
        previous_external = self._external_core_observation()
        if (
            previous_external is not None
            and self._last_verified_external_core is None
        ):
            self._last_verified_external_core = previous_external
            self._last_verified_external_core_at = time.monotonic()
        task = self._core_probe_task
        if task is None or task.done():
            self._core_probe_generation += 1
            self._core_probe_task_generation = self._core_probe_generation
            task = asyncio.create_task(
                self._probe_core_observation(),
                name="webui:probe-external-core",
            )
            self._core_probe_task = task
        generation = self._core_probe_task_generation
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            result = _CoreProbeResult(None, "invalid")
        if generation != self._core_probe_generation:
            return self.core_observation
        if self._core_probe_task is task and task.done():
            self._core_probe_task = None
        observation = result.observation
        if self._manager_core_takes_precedence():
            # Health from a manager-owned Core is never cached as external
            # evidence; once the managed process stops it must not reappear.
            self.core_observation = None
            self._last_verified_external_core = None
            self._last_verified_external_core_at = None
            self._core_display_grace_until = None
            self._core_last_probe_failure_kind = None
            return observation

        self.core_observation = observation
        if observation is not None:
            self._last_verified_external_core = observation
            self._last_verified_external_core_at = time.monotonic()
            self._core_display_grace_until = None
            self._core_last_probe_failure_kind = None
        elif result.failure_kind == "transport":
            self._core_last_probe_failure_kind = "transport"
            verified_at = self._last_verified_external_core_at
            if (
                verified_at is not None
                and time.monotonic() - verified_at <= _CORE_LAST_VERIFIED_MAX_AGE_SECONDS
                and self._core_display_grace_until is None
            ):
                self._core_display_grace_until = time.monotonic() + _CORE_DISPLAY_GRACE_SECONDS
        else:
            self._last_verified_external_core = None
            self._last_verified_external_core_at = None
            self._core_display_grace_until = None
            self._core_last_probe_failure_kind = "invalid"
        return observation

    async def _fresh_external_core_observation(self) -> CoreObservation | None:
        """Return a fresh external-Core observation for a Core start boundary.

        The health probe itself may also see a manager-owned Core.  In that
        case the manager-owned state remains authoritative and this helper
        deliberately returns no external ownership evidence.
        """
        observation = await self.refresh_core_observation(force=True)
        if self._manager_core_takes_precedence():
            return None
        if observation is None:
            await self._resolve_uncertain_external_core_before_start()
        return observation

    # ---- external adapter observation ------------------------------

    @staticmethod
    def _normalize_external_path(value: object, base: object | None = None) -> str | None:
        """Normalize a process path for exact, case-insensitive comparison.

        ``Path`` follows the host OS.  External process records can still
        contain Windows-style paths in tests or in a copied process snapshot,
        so drive/UNC paths use ``ntpath`` explicitly.  No substring matching
        is performed by the classifier.
        """
        if value is None:
            return None
        raw = str(value).strip().strip('"')
        if not raw:
            return None
        windows_style = bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", raw)) or os.name == "nt"
        if windows_style:
            if base is not None and not ntpath.isabs(raw):
                raw = ntpath.join(str(base), raw)
            return ntpath.normcase(ntpath.normpath(raw)).replace("\\", "/").rstrip("/")
        path = Path(raw)
        if base is not None and not path.is_absolute():
            path = Path(str(base)) / path
        try:
            path = path.resolve(strict=False)
        except Exception:
            path = Path(os.path.abspath(str(path)))
        return os.path.normcase(os.path.normpath(str(path))).replace("\\", "/").rstrip("/")

    @staticmethod
    def _token_basename(token: object) -> str:
        raw = str(token or "").strip().strip('"').replace("\\", "/")
        return raw.rsplit("/", 1)[-1].casefold()

    @classmethod
    def _argv_path_matches(cls, token: object, target: str | None, cwd: str | None) -> bool:
        if target is None:
            return False
        raw = str(token or "").strip().strip('"')
        if not raw or raw.startswith("-"):
            return False
        return cls._normalize_external_path(raw, cwd) == target

    def _build_external_process_snapshot(self) -> _ProcessSnapshot:
        """Collect one bounded psutil snapshot for the adapter classifier.

        Only identity fields and TCP listener ownership are retained.  The
        returned object is ephemeral and never enters a service status/API
        payload; in particular, PIDs and command lines are discarded after
        classification.
        """
        records: list[_ProcessRecord] = []
        failed = False
        attrs = ["pid", "ppid", "cwd", "cmdline", "exe", "name"]
        try:
            iterator = psutil.process_iter(attrs=attrs)
            try:
                for process in iterator:
                    try:
                        info = getattr(process, "info", None)
                        if not isinstance(info, Mapping):
                            info = process.as_dict(attrs=attrs, ad_value=None)
                        pid_raw = info.get("pid", getattr(process, "pid", None))
                        if pid_raw is None:
                            continue
                        pid = int(pid_raw)
                        ppid_raw = info.get("ppid")
                        ppid = int(ppid_raw) if ppid_raw is not None else None
                        cmdline = info.get("cmdline")
                        if cmdline is None:
                            cmdline = []
                        if isinstance(cmdline, str):
                            cmdline = [cmdline]
                        argv = tuple(str(item) for item in cmdline if item is not None)
                        records.append(
                            _ProcessRecord(
                                pid=pid,
                                ppid=ppid,
                                cwd=str(info.get("cwd")) if info.get("cwd") else None,
                                argv=argv,
                                executable=str(info.get("exe")) if info.get("exe") else None,
                                name=str(info.get("name")) if info.get("name") else None,
                            )
                        )
                    except (psutil.NoSuchProcess, psutil.ZombieProcess):
                        continue
                    except Exception:
                        # A single process may disappear or deny access while
                        # the table remains enumerable.  It is not evidence
                        # that the complete process snapshot failed: unrelated
                        # inaccessible records must not make every adapter
                        # indeterminate.  A matching candidate that cannot be
                        # read simply cannot contribute identity evidence.
                        continue
            except Exception:
                failed = True
        except Exception:
            failed = True

        listeners: dict[int, set[int]] = {}
        try:
            connections = psutil.net_connections(kind="tcp")
            for connection in connections:
                try:
                    status = str(getattr(connection, "status", "")).upper()
                    if status not in {"LISTEN", getattr(psutil, "CONN_LISTEN", "LISTEN")}:
                        continue
                    pid_raw = getattr(connection, "pid", None)
                    local = getattr(connection, "laddr", None)
                    port = getattr(local, "port", None)
                    if port is None and isinstance(local, (tuple, list)) and len(local) >= 2:
                        port = local[1]
                    if pid_raw is None or port is None:
                        continue
                    listeners.setdefault(int(port), set()).add(int(pid_raw))
                except Exception:
                    # Ignore one malformed/inaccessible connection record;
                    # only failure to enumerate the listener table globally
                    # makes the snapshot indeterminate.
                    continue
        except Exception:
            failed = True

        frozen_listeners = {port: frozenset(pids) for port, pids in listeners.items()}
        return _ProcessSnapshot(tuple(records), frozen_listeners, failed)

    def _external_service_cwd(self, service_id: str) -> str | None:
        sdef = SERVICE_DEFS.get(service_id)
        if sdef is None:
            return None
        if service_id == "snowluma_runtime":
            try:
                try:
                    from .snowluma_locator import resolve_snowluma_runtime
                except ImportError:  # pragma: no cover - direct module context
                    from snowluma_locator import resolve_snowluma_runtime
                return self._normalize_external_path(resolve_snowluma_runtime(self.root).path)
            except Exception:
                # A missing discovered runtime is an absent candidate unless
                # its configured listener is occupied, which is classified as
                # indeterminate below.
                return None
        cwd = sdef.cwd
        if not cwd:
            return None
        return self._normalize_external_path(cwd, self.root)

    def _external_service_port(self, service_id: str) -> int | None:
        sdef = SERVICE_DEFS.get(service_id)
        return int(sdef.port) if sdef and sdef.port else None

    def _record_has_python(self, record: _ProcessRecord) -> bool:
        candidates = [record.executable, record.name, *record.argv[:2]]
        for candidate in candidates:
            base = self._token_basename(candidate)
            if base in {"uv", "uv.exe", "cmd", "cmd.exe", "powershell", "powershell.exe"}:
                continue
            if base.startswith("python") or base in {"py", "py.exe"}:
                return True
        return False

    def _record_has_node(self, record: _ProcessRecord) -> bool:
        candidates = [record.executable, record.name, *record.argv[:2]]
        return any(self._token_basename(candidate) in {"node", "node.exe"} for candidate in candidates)

    def _record_matches_runtime(self, service_id: str, record: _ProcessRecord) -> bool:
        expected_cwd = self._external_service_cwd(service_id)
        if expected_cwd is None or self._normalize_external_path(record.cwd) != expected_cwd:
            return False
        if service_id in {
            "napcat_adapter",
            "snowluma_adapter",
            "bilibili",
            "koishi_adapter",
            "discordvc",
            "universalvc",
        }:
            if not self._record_has_python(record):
                return False
            target = self._normalize_external_path("main.py", expected_cwd)
            return any(self._argv_path_matches(token, target, record.cwd) for token in record.argv)
        if service_id == "live2d":
            if not self._record_has_python(record):
                return False
            tokens = [str(token).strip().strip('"') for token in record.argv]
            lowered = [token.casefold() for token in tokens]
            if "-m" not in lowered:
                return False
            try:
                module_index = lowered.index("-m")
            except ValueError:
                return False
            if module_index + 1 >= len(lowered) or lowered[module_index + 1] != "live2d_adapter":
                return False
            if "--config" not in lowered:
                return False
            config_index = lowered.index("--config")
            if config_index + 1 >= len(tokens):
                return False
            target = self._normalize_external_path("config.toml", expected_cwd)
            return self._argv_path_matches(tokens[config_index + 1], target, record.cwd)
        if service_id == "snowluma_runtime":
            if not self._record_has_node(record):
                return False
            target = self._normalize_external_path("index.mjs", expected_cwd)
            return any(self._argv_path_matches(token, target, record.cwd) for token in record.argv)
        if service_id == "koishi":
            if not self._record_has_node(record):
                return False
            tokens = [str(token).strip().strip('"') for token in record.argv]
            lowered = [token.casefold() for token in tokens]
            koishi_index = next(
                (
                    index
                    for index, token in enumerate(lowered)
                    if self._token_basename(token) in {"koishi", "koishi.js", "koishi.cjs", "koishi.mjs"}
                    or "koishijs" in token
                ),
                None,
            )
            if koishi_index is None:
                return False
            trailing = [token for token in lowered[koishi_index + 1:] if token]
            return bool(trailing) and trailing[-1] == "start"
        return False

    def _record_matches_napcat_shell_anchor(self, record: _ProcessRecord) -> bool:
        """Match only the explicit NapCat launcher or runtime executable.

        A shell opened in ``NapCat.Shell`` inherits the same cwd as the real
        launcher, so cwd alone is not process identity.  Descendant QQ evidence
        is considered only after one of these exact anchors has been found.
        """
        expected_cwd = self._external_service_cwd("napcat_shell")
        if expected_cwd is None or self._normalize_external_path(record.cwd) != expected_cwd:
            return False
        if any(
            self._token_basename(candidate) == "napcatwinbootmain.exe"
            for candidate in (record.name, record.executable)
        ):
            return True
        launcher = self._normalize_external_path("launcher-user.bat", expected_cwd)
        return any(
            self._argv_path_matches(token, launcher, record.cwd)
            for token in record.argv
        )

    def _external_candidate_pids(
        self,
        service_id: str,
        records: tuple[_ProcessRecord, ...],
    ) -> set[int]:
        return {
            record.pid
            for record in records
            if self._record_matches_runtime(service_id, record)
        }

    @staticmethod
    def _process_descendants(
        root_pid: int,
        records_by_pid: Mapping[int, _ProcessRecord],
    ) -> set[int]:
        descendants = {root_pid}
        changed = True
        while changed:
            changed = False
            for record in records_by_pid.values():
                if record.pid not in descendants and record.ppid in descendants:
                    descendants.add(record.pid)
                    changed = True
        return descendants

    @classmethod
    def _collapse_external_candidates(
        cls,
        candidate_pids: set[int],
        records_by_pid: Mapping[int, _ProcessRecord],
    ) -> set[int] | None:
        if not candidate_pids:
            return set()
        roots = []
        for pid in candidate_pids:
            current = records_by_pid.get(pid)
            ancestor = current.ppid if current else None
            seen: set[int] = set()
            related = False
            while ancestor is not None and ancestor not in seen:
                if ancestor in candidate_pids:
                    related = True
                    break
                seen.add(ancestor)
                parent = records_by_pid.get(ancestor)
                ancestor = parent.ppid if parent else None
            if not related:
                roots.append(pid)
        if len(roots) != 1:
            return None
        return cls._process_descendants(roots[0], records_by_pid)

    def _classify_external_adapters(
        self,
        snapshot: _ProcessSnapshot,
        *,
        exclude_pids: frozenset[int] = frozenset(),
    ) -> dict[str, AdapterObservation]:
        now = time.time()
        services = tuple(EXTERNAL_ADAPTER_SERVICE_IDS)
        if snapshot.failed:
            return {
                service_id: AdapterObservation(
                    service_id, "indeterminate", now, _EXTERNAL_INDETERMINATE_DETAIL
                )
                for service_id in services
            }

        records = tuple(record for record in snapshot.processes if record.pid not in exclude_pids)
        listeners = {
            port: frozenset(pid for pid in owners if pid not in exclude_pids)
            for port, owners in snapshot.listeners.items()
        }
        records_by_pid = {record.pid: record for record in records}
        matches: dict[str, set[int]] = {
            service_id: self._external_candidate_pids(service_id, records)
            for service_id in services
        }
        matched_by_pid: dict[int, set[str]] = {}
        for service_id, pids in matches.items():
            for pid in pids:
                matched_by_pid.setdefault(pid, set()).add(service_id)
        cross_service_pids = {
            pid for pid, service_ids in matched_by_pid.items() if len(service_ids) > 1
        }

        results: dict[str, AdapterObservation] = {}
        for service_id in services:
            candidate_pids = matches[service_id]
            if any(pid in cross_service_pids for pid in candidate_pids):
                results[service_id] = AdapterObservation(
                    service_id, "indeterminate", now, _EXTERNAL_INDETERMINATE_DETAIL
                )
                continue

            if service_id == "napcat_shell":
                anchors = {
                    record.pid
                    for record in records
                    if self._record_matches_napcat_shell_anchor(record)
                }
                tree = self._collapse_external_candidates(anchors, records_by_pid)
                if tree is None:
                    results[service_id] = AdapterObservation(
                        service_id, "indeterminate", now, _EXTERNAL_INDETERMINATE_DETAIL
                    )
                    continue
                if not tree:
                    results[service_id] = AdapterObservation(service_id, "absent", now)
                    continue
                live_names = {
                    self._token_basename(record.name or record.executable)
                    for pid, record in records_by_pid.items()
                    if pid in tree
                }
                live_qq = {
                    name for name in live_names
                    if name in {
                        "napcatwinbootmain.exe",
                        "qq.exe",
                        "qqnt.exe",
                        "qqnt.exe",
                    }
                    or name.startswith("qq")
                }
                outcome = "external_ready" if live_qq else "external_present_unready"
                results[service_id] = AdapterObservation(
                    service_id,
                    outcome,
                    now,
                    _EXTERNAL_READY_DETAIL if outcome == "external_ready" else _EXTERNAL_PRESENT_UNREADY_DETAIL,
                )
                continue

            tree = self._collapse_external_candidates(candidate_pids, records_by_pid)
            if tree is None:
                results[service_id] = AdapterObservation(
                    service_id, "indeterminate", now, _EXTERNAL_INDETERMINATE_DETAIL
                )
                continue
            if not tree:
                port = self._external_service_port(service_id)
                owners = listeners.get(port, frozenset()) if port else frozenset()
                if owners:
                    results[service_id] = AdapterObservation(
                        service_id, "indeterminate", now, _EXTERNAL_INDETERMINATE_DETAIL
                    )
                else:
                    results[service_id] = AdapterObservation(service_id, "absent", now)
                continue

            port = self._external_service_port(service_id)
            if port is None:
                results[service_id] = AdapterObservation(
                    service_id, "external_ready", now, _EXTERNAL_READY_DETAIL
                )
                continue
            owners = listeners.get(port, frozenset())
            if not owners:
                results[service_id] = AdapterObservation(
                    service_id,
                    "external_present_unready",
                    now,
                    _EXTERNAL_PRESENT_UNREADY_DETAIL,
                )
            elif not owners.issubset(tree):
                results[service_id] = AdapterObservation(
                    service_id, "indeterminate", now, _EXTERNAL_INDETERMINATE_DETAIL
                )
            else:
                results[service_id] = AdapterObservation(
                    service_id, "external_ready", now, _EXTERNAL_READY_DETAIL
                )
        return results

    def _scan_external_adapters(
        self,
        *,
        exclude_pids: frozenset[int] = frozenset(),
    ) -> dict[str, AdapterObservation]:
        snapshot = self._build_external_process_snapshot()
        return self._classify_external_adapters(snapshot, exclude_pids=exclude_pids)

    async def refresh_adapter_observation(
        self,
        *,
        force: bool = True,
    ) -> dict[str, AdapterObservation]:
        """Refresh all adapter identities from one off-loop process snapshot."""
        if not force and self.adapter_observation_cache:
            return dict(self.adapter_observation_cache)
        task = self._adapter_probe_task
        if task is None or task.done():
            self._adapter_observation_generation += 1
            self._adapter_probe_task_generation = self._adapter_observation_generation
            task = asyncio.create_task(
                asyncio.to_thread(self._scan_external_adapters),
                name="webui:probe-external-adapters",
            )
            self._adapter_probe_task = task
        generation = self._adapter_probe_task_generation
        try:
            observations = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            now = time.time()
            observations = {
                service_id: AdapterObservation(
                    service_id, "indeterminate", now, _EXTERNAL_INDETERMINATE_DETAIL
                )
                for service_id in EXTERNAL_ADAPTER_SERVICE_IDS
            }
        if self._adapter_probe_task is task and task.done():
            self._adapter_probe_task = None
        if generation != self._adapter_observation_generation:
            return dict(self.adapter_observation_cache)
        self.adapter_observation_cache = dict(observations)
        self.external_adapter_observations = self.adapter_observation_cache
        return dict(observations)

    async def refresh_external_observation(
        self,
        *,
        force: bool = True,
    ) -> dict[str, Any]:
        """Refresh Core health and adapter identities for status/mutation APIs."""
        core, adapters = await asyncio.gather(
            self.refresh_core_observation(force=force),
            self.refresh_adapter_observation(force=force),
        )
        return {"core": core, "adapters": adapters}

    # Plural spelling is kept as a small compatibility convenience for callers
    # that describe this as an observation set rather than a single cache.
    refresh_external_observations = refresh_external_observation

    async def _fresh_external_adapter_observations(
        self,
        service_ids: tuple[str, ...],
        *,
        exclude_pids: frozenset[int] = frozenset(),
    ) -> dict[str, AdapterObservation]:
        # A mutation-fresh scan must not be overwritten by an older ordinary
        # UI poll that happens to finish after this boundary check.
        self._adapter_observation_generation += 1
        generation = self._adapter_observation_generation
        try:
            observations = await asyncio.to_thread(
                self._scan_external_adapters,
                exclude_pids=exclude_pids,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            now = time.time()
            observations = {
                service_id: AdapterObservation(
                    service_id, "indeterminate", now, _EXTERNAL_INDETERMINATE_DETAIL
                )
                for service_id in EXTERNAL_ADAPTER_SERVICE_IDS
            }
        normalized = {
            service_id: observations.get(
                service_id,
                AdapterObservation(service_id, "indeterminate", time.time(), _EXTERNAL_INDETERMINATE_DETAIL),
            )
            for service_id in service_ids
        }
        if generation == self._adapter_observation_generation:
            self.adapter_observation_cache.update(normalized)
        return normalized

    async def refresh_adapter_observation_for_mutation(
        self,
    ) -> dict[str, AdapterObservation]:
        """Take a boundary-fresh adapter snapshot for a state-changing request.

        Unlike status polling, this deliberately never joins
        ``_adapter_probe_task``: that task may have started before the request
        reached its mutation boundary.
        """
        return await self._fresh_external_adapter_observations(
            EXTERNAL_ADAPTER_SERVICE_IDS
        )

    def _manager_service_takes_precedence(self, service_id: str) -> bool:
        state = self.states.get(service_id)
        if state is None or service_state_is_pure_start_reservation(state):
            return False
        return (
            state.status in {
                ServiceStatus.STARTING,
                ServiceStatus.RUNNING,
                ServiceStatus.STOPPING,
                ServiceStatus.ERROR,
            }
            or service_state_retains_runtime(state)
        )

    def _external_adapter_observation(self, service_id: str) -> AdapterObservation | None:
        return self.adapter_observation_cache.get(service_id)

    def _external_start_blocked(self, service_id: str) -> bool:
        observation = self._external_adapter_observation(service_id)
        return (
            not self._manager_service_takes_precedence(service_id)
            and observation is not None
            and observation.outcome in _EXTERNAL_BLOCKING_OUTCOMES
        )

    @staticmethod
    def _external_presence_detail(observation: AdapterObservation) -> str:
        return observation.detail or {
            "external_present_unready": _EXTERNAL_PRESENT_UNREADY_DETAIL,
            "indeterminate": _EXTERNAL_INDETERMINATE_DETAIL,
        }.get(observation.outcome, "")

    def _remember_adapter_start_preflight(
        self,
        service_ids: tuple[str, ...],
        observations: Mapping[str, AdapterObservation],
    ) -> None:
        for service_id in service_ids:
            if service_id in EXTERNAL_ADAPTER_SERVICE_IDS and service_id in observations:
                self._adapter_start_preflight[service_id] = observations[service_id]

    def _check_cached_adapter_start_allowed(self, service_ids: tuple[str, ...]) -> frozenset[str]:
        """Validate cached adapter evidence and return externally-ready IDs."""
        ready: set[str] = set()
        for service_id in service_ids:
            if service_id not in EXTERNAL_ADAPTER_SERVICE_IDS:
                continue
            if self._manager_service_takes_precedence(service_id):
                continue
            observation = self._external_adapter_observation(service_id)
            if observation is None:
                continue
            if observation.outcome == "external_ready":
                ready.add(service_id)
            elif observation.outcome in _EXTERNAL_BLOCKING_OUTCOMES:
                raise RuntimeError(
                    f"Cannot start {SERVICE_DEFS[service_id].name}: "
                    f"{self._external_presence_detail(observation)}"
                )
        return frozenset(ready)

    @staticmethod
    def _qq_conflicting_service_ids(service_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Return the non-selected QQ backend services for a start request."""
        selected_backends = {
            _QQ_SERVICE_BACKEND[service_id]
            for service_id in service_ids
            if service_id in _QQ_SERVICE_BACKEND
        }
        if not selected_backends:
            return ()
        return tuple(
            service_id
            for service_id in QQ_SERVICE_IDS
            if _QQ_SERVICE_BACKEND.get(service_id) not in selected_backends
        )

    def _validate_qq_conflicts(
        self,
        service_ids: tuple[str, ...],
        *,
        observations: Mapping[str, AdapterObservation] | None = None,
    ) -> None:
        """Reject managed or externally-present services from the other QQ backend."""
        conflict_ids = self._qq_conflicting_service_ids(service_ids)
        if not conflict_ids:
            return
        observations = observations or self.adapter_observation_cache
        for other_id in conflict_ids:
            operation = self._operation_tasks.get(f"service:{other_id}")
            if operation and not operation.done():
                raise RuntimeError(
                    f"Cannot start QQ services: {SERVICE_DEFS[other_id].name} is already changing state."
                )
            state = self.states.get(other_id)
            if state and (
                state.status in QQ_BUSY_STATUSES
                or service_state_retains_runtime(state)
            ):
                raise RuntimeError(
                    f"Cannot start QQ services: {SERVICE_DEFS[other_id].name} is already active. Stop it first."
                )
            observation = observations.get(other_id)
            if observation and observation.outcome in _EXTERNAL_PRESENCE_OUTCOMES:
                raise RuntimeError(
                    f"Cannot start QQ services: external {SERVICE_DEFS[other_id].name} is present. "
                    "Stop it in the original launcher first."
                )

    async def _fresh_qq_start_guard(
        self,
        service_ids: tuple[str, ...],
    ) -> dict[str, AdapterObservation]:
        """Refresh/cache all four QQ IDs, then enforce cross-backend exclusion."""
        selected = tuple(service_id for service_id in service_ids if service_id in QQ_SERVICE_IDS)
        if not selected:
            return {}
        observations = await self._fresh_external_adapter_observations(QQ_SERVICE_IDS)
        self._validate_qq_conflicts(selected, observations=observations)
        return observations

    async def _fresh_adapter_start_guard(
        self,
        service_id: str,
    ) -> bool:
        """Close the preflight/spawn race; return True when external is ready."""
        if service_id not in EXTERNAL_ADAPTER_SERVICE_IDS:
            return False
        if self._manager_service_takes_precedence(service_id):
            return False
        if service_id in QQ_SERVICE_IDS:
            observations = await self._fresh_qq_start_guard((service_id,))
        else:
            observations = await self._fresh_external_adapter_observations((service_id,))
        observation = observations[service_id]
        previous = self._adapter_start_preflight.pop(service_id, None)
        if previous is not None and previous.outcome in _EXTERNAL_PRESENCE_OUTCOMES:
            if observation.outcome in {"absent", "indeterminate"}:
                raise RuntimeError(
                    f"外部 {SERVICE_DEFS[service_id].name} 状态发生变化，拒绝替换或接管"
                )
        if observation.outcome == "external_ready":
            return True
        if observation.outcome in _EXTERNAL_BLOCKING_OUTCOMES:
            raise RuntimeError(
                f"Cannot start {SERVICE_DEFS[service_id].name}: "
                f"{self._external_presence_detail(observation)}"
            )
        return False

    def core_readiness_status(self) -> str:
        """Return Core status from manager state or bounded display evidence."""
        state = self.states.get("nachobot")
        if state is not None and self._manager_core_takes_precedence():
            return state.status.value
        if self._external_core_display_observation() is not None:
            return ServiceStatus.RUNNING.value
        if state is not None:
            return state.status.value
        return ServiceStatus.STOPPED.value

    def core_chat_delivery_allowed(self) -> bool:
        """Allow Chat only with current or recent verified Core evidence."""
        state = self.states.get("nachobot")
        if state is not None and self._manager_core_takes_precedence():
            return state.status == ServiceStatus.RUNNING
        return self._external_core_display_observation() is not None

    def core_is_ready(self) -> bool:
        """Whether Core is manager-running or externally verified and ready."""
        state = self.states.get("nachobot")
        if state is not None and self._manager_core_takes_precedence():
            return state.status == ServiceStatus.RUNNING
        return self.core_observation is not None

    async def prepare_start_launch(self, profile_id: str, runtime: str | None = None) -> None:
        """Force a fresh Core probe before a launch request is scheduled."""
        _register_services(self.root)
        profile_id = str(profile_id or "").strip().lower()
        group_id = LAUNCH_PROFILE_GROUPS.get(profile_id)
        if group_id is None:
            raise ValueError(f"Unknown launch profile: {profile_id}")
        if profile_id != "potato":
            resolved = MultimodalRuntimeManager.normalize_profile(runtime or self._launch_runtime)
            MultimodalRuntimeManager.require_python(resolved)
        previous = self._external_core_observation()
        observation = await self._fresh_external_core_observation()
        if previous is not None and observation is None and not self._manager_core_takes_precedence():
            raise RuntimeError("外部 NachoBot Core 已消失，拒绝启动以避免接管或替换它")
        if observation is not None and not self._manager_core_takes_precedence():
            if observation.observed_profile != profile_id:
                raise RuntimeError(
                    f"外部 NachoBot Core 当前为 {observation.observed_profile.upper()} 模式，"
                    f"无法切换为 {profile_id.upper()}；请在外部启动器中切换"
                )
        self._validate_group_start(group_id)

    async def prepare_start_group(self, group_id: str) -> None:
        """Force Core readiness for QQ group starts before scheduling work."""
        _register_services(self.root)
        if group_id not in GROUP_DEFS:
            raise ValueError(f"Unknown group: {group_id}")
        gdef = GROUP_DEFS[group_id]
        adapter_observations: dict[str, AdapterObservation] = {}
        adapter_skip = frozenset()
        if group_id == "qq_adapter":
            adapter_observations = await self._fresh_qq_start_guard(tuple(gdef.services))
            adapter_skip = self._check_cached_adapter_start_allowed(tuple(gdef.services))
            self._remember_adapter_start_preflight(tuple(gdef.services), adapter_observations)
        elif any(service_id in EXTERNAL_ADAPTER_SERVICE_IDS for service_id in gdef.services):
            adapter_observations = await self.refresh_adapter_observation_for_mutation()
            adapter_skip = self._check_cached_adapter_start_allowed(tuple(gdef.services))
            self._remember_adapter_start_preflight(tuple(gdef.services), adapter_observations)
        if group_id == "core":
            # Starting Core directly must recognize an already-running
            # external Core before any port/spawn decision is scheduled.
            if await self._fresh_external_core_observation() is not None:
                return
        elif group_id == "qq_adapter":
            await self.refresh_core_observation(force=True)
        elif group_id in LAUNCH_PROFILE_GROUPS.values():
            profile_id = next(
                profile
                for profile, candidate_group in LAUNCH_PROFILE_GROUPS.items()
                if candidate_group == group_id
            )
            previous_external = self._external_core_observation()
            observation = await self._fresh_external_core_observation()
            if (
                previous_external is not None
                and observation is None
                and not self._manager_core_takes_precedence()
            ):
                raise RuntimeError("外部 NachoBot Core 已消失，拒绝启动 WebUI 服务")
            if observation is not None and observation.observed_profile != profile_id:
                raise RuntimeError(
                    f"外部 NachoBot Core 当前为 {observation.observed_profile.upper()} 模式，"
                    f"无法启动 {profile_id.upper()}；请在外部启动器中切换"
                )
        self._validate_group_start(group_id, skip_service_ids=adapter_skip)

    async def prepare_start_service(self, service_id: str) -> None:
        """Force Core readiness for direct QQ-adapter starts."""
        _register_services(self.root)
        if service_id not in SERVICE_DEFS:
            raise ValueError(f"Unknown service: {service_id}")
        if service_id in QQ_SERVICE_IDS:
            observations = await self._fresh_qq_start_guard((service_id,))
            self._check_cached_adapter_start_allowed((service_id,))
            self._remember_adapter_start_preflight((service_id,), observations)
        elif service_id in EXTERNAL_ADAPTER_SERVICE_IDS:
            observations = await self.refresh_adapter_observation_for_mutation()
            self._check_cached_adapter_start_allowed((service_id,))
            self._remember_adapter_start_preflight((service_id,), observations)
        if service_id == "nachobot":
            # A Core-only request is a safe no-op when an external Core is
            # already verified; do not let validation reach its occupied port.
            if await self._fresh_external_core_observation() is not None:
                return
        elif service_id in {
            "tts_runtime_full",
            "tts_runtime_lite",
            "perception",
        }:
            profile_id = next(
                (
                    profile
                    for profile, group_id in LAUNCH_PROFILE_GROUPS.items()
                    if service_id in GROUP_DEFS[group_id].services
                ),
                None,
            )
            previous_external = self._external_core_observation()
            observation = await self._fresh_external_core_observation()
            if (
                previous_external is not None
                and observation is None
                and not self._manager_core_takes_precedence()
            ):
                raise RuntimeError("外部 NachoBot Core 已消失，拒绝启动 WebUI 服务")
            if (
                observation is not None
                and profile_id is not None
                and observation.observed_profile != profile_id
            ):
                raise RuntimeError(
                    f"外部 NachoBot Core 当前为 {observation.observed_profile.upper()} 模式，"
                    f"无法启动 {service_id}；请在外部启动器中切换"
                )
            if (
                observation is not None
                and service_id in self._external_ready_service_ids(profile_id or "", observation)
            ):
                return
        elif service_id in {"napcat_adapter", "snowluma_adapter", "koishi_adapter"}:
            await self.refresh_core_observation(force=True)
        self._ensure_required_components((service_id,))
        self._validate_service_start(service_id)

    # ---- public API ----

    def get_service_status(self, service_id: str) -> dict[str, Any]:
        """Get status info for a single service."""
        sdef = SERVICE_DEFS.get(service_id)
        if not sdef:
            raise ValueError(f"Unknown service: {service_id}")
        state = self.states.get(service_id)
        manager_owned = bool(
            state
            and not service_state_is_pure_start_reservation(state)
            and (
                state.status != ServiceStatus.STOPPED
                or service_state_retains_runtime(state)
            )
        )
        status = state.status.value if state else ServiceStatus.STOPPED.value
        origin: str | None = "webui" if manager_owned else None
        observed_profile: str | None = None
        started_port = state.started_port if state else None
        pid = state.pid if state else None
        started_at = state.started_at if state else None
        external_state: str | None = None

        if service_id == "nachobot" and manager_owned:
            observed_profile = self._core_runtime_profile

        if service_id == "nachobot" and not manager_owned:
            observation = self._external_core_display_observation()
            if observation is not None:
                status = ServiceStatus.RUNNING.value
                origin = "external"
                observed_profile = observation.observed_profile
                # External health evidence never carries process identity or
                # manager-owned cleanup capability.
                started_port = sdef.port
                pid = None
                started_at = None

        adapter_observation = self._external_adapter_observation(service_id)
        if (
            service_id in EXTERNAL_ADAPTER_SERVICE_IDS
            and not manager_owned
            and adapter_observation is not None
        ):
            external_state = adapter_observation.external_state
            if adapter_observation.outcome == "external_ready":
                status = ServiceStatus.RUNNING.value
                origin = "external"
                started_port = sdef.port
                pid = None
                started_at = None
            elif adapter_observation.outcome == "external_present_unready":
                status = ServiceStatus.ERROR.value
                origin = "external"
                started_port = sdef.port
                pid = None
                started_at = None
            elif adapter_observation.outcome == "indeterminate":
                status = ServiceStatus.ERROR.value
                origin = None
                started_port = sdef.port
                pid = None
                started_at = None

        if not manager_owned:
            observation = self._external_core_display_observation()
            if (
                observation is not None
                and service_id in self._external_ready_service_ids(
                    observation.observed_profile,
                    observation,
                )
            ):
                status = ServiceStatus.RUNNING.value
                origin = "external"
                observed_profile = observation.observed_profile
                # External health evidence never carries process identity or
                # manager-owned cleanup capability.
                started_port = sdef.port
                pid = None
                started_at = None

        return {
            "id": service_id,
            "name": sdef.name,
            "group_id": sdef.group_id,
            "port": started_port if status == ServiceStatus.RUNNING and started_port is not None else sdef.port,
            "detail": (
                self._external_presence_detail(adapter_observation)
                if (
                    adapter_observation is not None
                    and not manager_owned
                    and adapter_observation.outcome in _EXTERNAL_BLOCKING_OUTCOMES
                )
                else sdef.detail
            ),
            "status": status,
            "pid": pid,
            "started_at": started_at,
            "managed": manager_owned,
            "origin": origin,
            "observed_profile": observed_profile,
            "external_state": external_state,
        }

    def get_all_statuses(self) -> list[dict[str, Any]]:
        _register_services(self.root)
        return [self.get_service_status(sid) for sid in SERVICE_DEFS]

    def get_group_statuses(self) -> list[dict[str, Any]]:
        _register_services(self.root)
        result = []
        for gid, gdef in GROUP_DEFS.items():
            services = [self.get_service_status(sid) for sid in gdef.services]
            result.append({
                "id": gid,
                "name": gdef.name,
                "icon": gdef.icon,
                "detail": gdef.detail,
                "services": services,
            })
        return result

    def get_launch_status(self) -> dict[str, Any]:
        """Return the user-facing Core + mutually-exclusive runtime profile state."""
        _register_services(self.root)
        core = self.get_service_status("nachobot")
        external_observation = self._external_core_display_observation()
        profiles: list[dict[str, Any]] = []
        active_profile: str | None = None
        error_profile: str | None = None

        for profile_id, group_id in LAUNCH_PROFILE_GROUPS.items():
            gdef = GROUP_DEFS[group_id]
            services = [self.get_service_status(sid) for sid in gdef.services]
            statuses = [service["status"] for service in services]
            if (
                profile_id == "potato"
                and (
                    self._core_runtime_profile == "potato"
                    or (
                        core.get("origin") == "external"
                        and core.get("observed_profile") == "potato"
                    )
                )
                and core["status"] == ServiceStatus.RUNNING.value
            ):
                # POTATO is deliberately represented by the manager-owned Core
                # only. Its group has no child services, so an ordinary
                # ``all([])`` check would incorrectly report it as stopped.
                status = ServiceStatus.RUNNING.value
                active_profile = active_profile or profile_id
            elif any(status == ServiceStatus.ERROR.value for status in statuses):
                status = ServiceStatus.ERROR.value
                error_profile = error_profile or profile_id
            elif any(status == ServiceStatus.STOPPING.value for status in statuses):
                status = ServiceStatus.STOPPING.value
                active_profile = active_profile or profile_id
            elif any(status == ServiceStatus.STARTING.value for status in statuses):
                status = ServiceStatus.STARTING.value
                active_profile = active_profile or profile_id
            elif services and all(status == ServiceStatus.RUNNING.value for status in statuses):
                status = ServiceStatus.RUNNING.value
                active_profile = active_profile or profile_id
            elif any(status == ServiceStatus.RUNNING.value for status in statuses):
                status = "partial"
                active_profile = active_profile or profile_id
            elif (
                external_observation is not None
                and external_observation.observed_profile == profile_id
                and external_observation.status == "degraded"
            ):
                # Preserve an authenticated but degraded external profile as
                # partial even when none of its required local components is
                # ready enough to synthesize a running service row.
                status = "partial"
                active_profile = active_profile or profile_id
            else:
                status = ServiceStatus.STOPPED.value

            profiles.append({
                "id": profile_id,
                "group_id": group_id,
                "status": status,
                "services": services,
            })

        # A verified external Core carries the active product profile even
        # when WebUI has not started that profile's dependent services yet.
        if active_profile is None and core.get("origin") == "external":
            observed_profile = core.get("observed_profile")
            if observed_profile in LAUNCH_PROFILE_GROUPS:
                active_profile = observed_profile

        launch_task = self._operation_tasks.get("launch")
        launch_kind = self._operation_kinds.get("launch") if launch_task and not launch_task.done() else None
        if launch_kind == "start":
            status = ServiceStatus.STARTING.value
        elif launch_kind == "stop":
            status = ServiceStatus.STOPPING.value
        elif core["status"] == ServiceStatus.ERROR.value:
            status = ServiceStatus.ERROR.value
        elif core["status"] == ServiceStatus.RUNNING.value and active_profile and any(
            profile["id"] == active_profile and profile["status"] == ServiceStatus.RUNNING.value
            for profile in profiles
        ):
            # A currently active healthy profile wins over stale ERROR state left
            # behind by another mutually-exclusive profile.
            status = ServiceStatus.RUNNING.value
        elif active_profile:
            status = "partial"
        elif error_profile:
            status = ServiceStatus.ERROR.value
        elif core["status"] != ServiceStatus.STOPPED.value:
            status = "partial"
        else:
            status = ServiceStatus.STOPPED.value

        return {
            "status": status,
            "active_profile": active_profile or error_profile,
            "runtime": self._launch_runtime,
            "operation": launch_kind,
            "core": core,
            "external_core": core.get("origin") == "external",
            "profiles": profiles,
        }

    def get_log_history(self, service_id: str) -> list[str]:
        """Return buffered log lines for a service."""
        state = self.states.get(service_id)
        if state:
            return list(state.log_buffer)
        return []

    # ---- start / stop ----

    def _clear_start_reservation(self, service_id: str, *, remove: bool = False) -> None:
        """Clear a queued request marker without touching external processes."""
        state = self.states.get(service_id)
        if state is None or not state.start_reservation:
            return
        pure_reservation = service_state_is_pure_start_reservation(state)
        state.start_reservation = False
        if remove and pure_reservation and self.states.get(service_id) is state:
            self.states.pop(service_id, None)

    def _schedule_operation(
        self,
        key: str,
        operation: Callable[[], Awaitable[None]],
        affected_services: tuple[str, ...],
        *,
        operation_kind: str,
        replace: bool = False,
    ) -> asyncio.Task[None]:
        existing = self._operation_tasks.get(key)
        if existing and not existing.done():
            if not replace:
                return existing
            existing.cancel()

        task = asyncio.create_task(operation(), name=f"webui:{key}")
        self._operation_tasks[key] = task
        self._operation_kinds[key] = operation_kind

        def operation_done(completed: asyncio.Task[None]) -> None:
            if self._operation_tasks.get(key) is completed:
                self._operation_tasks.pop(key, None)
                self._operation_kinds.pop(key, None)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error is None:
                return
            logger.error("Managed WebUI operation %s failed: %s", key, error, exc_info=error)
            for service_id in affected_services:
                state = self.states.get(service_id)
                if state and state.status != ServiceStatus.STOPPED:
                    state.status = ServiceStatus.ERROR

        task.add_done_callback(operation_done)
        return task

    def request_start_service(self, service_id: str) -> None:
        """Validate and schedule a service start whose failures remain observable."""
        _register_services(self.root)
        sdef = SERVICE_DEFS.get(service_id)
        if sdef is None:
            raise ValueError(f"Unknown service: {service_id}")
        adapter_ready = self._check_cached_adapter_start_allowed((service_id,))
        if service_id in adapter_ready:
            self._validate_qq_conflicts((service_id,))
            self._clear_start_reservation(service_id, remove=True)
            return
        # A verified external Core is already the requested resource.  A
        # direct Core start must never adopt, replace, or terminate it.
        if service_id == "nachobot" and self._external_core_observation() is not None:
            return
        if service_id in {"tts_runtime_full", "tts_runtime_lite", "perception"}:
            observation = self._external_core_observation()
            if observation is not None:
                expected_profile = next(
                    (
                        profile
                        for profile, group_id in LAUNCH_PROFILE_GROUPS.items()
                        if service_id in GROUP_DEFS[group_id].services
                    ),
                    None,
                )
                if expected_profile is not None and observation.observed_profile != expected_profile:
                    raise RuntimeError(
                        f"外部 NachoBot Core 当前为 {observation.observed_profile.upper()} 模式，"
                        f"无法启动 {service_id}；请在外部启动器中切换"
                    )
                if service_id in self._external_ready_service_ids(
                    observation.observed_profile,
                    observation,
                ):
                    return
        # Perform component discovery synchronously, before creating the
        # operation task.  This keeps a missing selected backend from ever
        # entering the scheduler.
        self._ensure_required_components((service_id,))
        state = self.states.get(service_id)
        if state and state.status in (ServiceStatus.RUNNING, ServiceStatus.STARTING):
            return
        self._validate_service_start(service_id)
        group_task = self._operation_tasks.get(f"group:{sdef.group_id}")
        service_task = self._operation_tasks.get(f"service:{service_id}")
        if (group_task and not group_task.done()) or (service_task and not service_task.done()):
            raise RuntimeError(f"Service {service_id} is already changing state")
        if state is None:
            state = ServiceState()
            self.states[service_id] = state
        # Reserve the requested slot without claiming manager ownership or
        # masking a Core/dependency observation that may arrive before start.
        state.start_reservation = not service_state_has_runtime_capability(state)
        state.status = ServiceStatus.STARTING
        self._schedule_operation(
            f"service:{service_id}",
            lambda: self.start_service(service_id, _prepared=True),
            (service_id,),
            operation_kind="start",
        )

    def request_stop_service(self, service_id: str) -> None:
        """Cancel an in-flight start and schedule an orderly service stop."""
        _register_services(self.root)
        sdef = SERVICE_DEFS.get(service_id)
        if sdef is None:
            raise ValueError(f"Unknown service: {service_id}")
        group_task = self._operation_tasks.get(f"group:{sdef.group_id}")
        if group_task and not group_task.done():
            group_task.cancel()
        state = self.states.get(service_id)
        if not state:
            return
        if (
            state.status == ServiceStatus.STOPPED
            and state.process_group_id is None
            and not state.windows_owned_processes
            and state.windows_job is None
            and state.process is None
        ):
            self._clear_start_reservation(service_id, remove=True)
            return
        if state.status == ServiceStatus.STOPPING:
            operation = self._operation_tasks.get(f"service:{service_id}")
            if operation and not operation.done():
                return
        state.status = ServiceStatus.STOPPING
        self._schedule_operation(
            f"service:{service_id}",
            lambda: self.stop_service(service_id, _prepared=True),
            (service_id,),
            operation_kind="stop",
            replace=True,
        )

    def request_start_group(self, group_id: str) -> None:
        """Validate and schedule a group start as one managed operation."""
        _register_services(self.root)
        gdef = GROUP_DEFS.get(group_id)
        if gdef is None:
            raise ValueError(f"Unknown group: {group_id}")
        existing = self._operation_tasks.get(f"group:{group_id}")
        if existing and not existing.done():
            raise RuntimeError(f"Group {group_id} is already changing state")
        self._validate_qq_conflicts(tuple(gdef.services))
        profile_id = next(
            (
                profile
                for profile, candidate_group in LAUNCH_PROFILE_GROUPS.items()
                if candidate_group == group_id
            ),
            None,
        )
        skip_service_ids = (
            self._external_ready_service_ids(profile_id or "")
            if profile_id is not None
            else frozenset()
        )
        adapter_skip = self._check_cached_adapter_start_allowed(tuple(gdef.services))
        skip_service_ids = frozenset(set(skip_service_ids) | set(adapter_skip))
        observation = self._external_core_observation()
        if (
            profile_id is not None
            and observation is not None
            and observation.observed_profile != profile_id
        ):
            raise RuntimeError(
                f"外部 NachoBot Core 当前为 {observation.observed_profile.upper()} 模式，"
                f"无法启动 {profile_id.upper()}；请在外部启动器中切换"
            )
        if profile_id is None:
            self._validate_group_start(group_id)
        else:
            self._validate_group_start(group_id, skip_service_ids=skip_service_ids)
        self._schedule_operation(
            f"group:{group_id}",
            lambda: self.start_group(group_id),
            tuple(gdef.services),
            operation_kind="start",
        )

    def request_stop_group(self, group_id: str) -> None:
        """Cancel an in-flight group start and schedule reverse-order shutdown."""
        _register_services(self.root)
        gdef = GROUP_DEFS.get(group_id)
        if gdef is None:
            raise ValueError(f"Unknown group: {group_id}")
        existing = self._operation_tasks.get(f"group:{group_id}")
        if (
            existing
            and not existing.done()
            and self._operation_kinds.get(f"group:{group_id}") == "stop"
        ):
            return
        self._schedule_operation(
            f"group:{group_id}",
            lambda: self.stop_group(group_id),
            tuple(gdef.services),
            operation_kind="stop",
            replace=True,
        )

    def request_start_launch(self, profile_id: str, runtime: str | None = None) -> None:
        """Start Core and exactly one functionality profile."""
        _register_services(self.root)
        profile_id = str(profile_id or "").strip().lower()
        group_id = LAUNCH_PROFILE_GROUPS.get(profile_id)
        if group_id is None:
            raise ValueError(f"Unknown launch profile: {profile_id}")

        resolved_runtime: str | None = None
        if profile_id != "potato":
            resolved_runtime = MultimodalRuntimeManager.normalize_profile(runtime or "gpu")
            MultimodalRuntimeManager.require_python(resolved_runtime)
            self._launch_runtime = resolved_runtime

        observation = self._external_core_observation()
        if observation is not None and observation.observed_profile != profile_id:
            raise RuntimeError(
                f"外部 NachoBot Core 当前为 {observation.observed_profile.upper()} 模式，"
                f"无法切换为 {profile_id.upper()}；请在外部启动器中切换"
            )

        existing = self._operation_tasks.get("launch")
        if existing and not existing.done():
            raise RuntimeError("NachoBot launch is already changing state")
        for group in ("core", *LAUNCH_PROFILE_GROUPS.values()):
            operation = self._operation_tasks.get(f"group:{group}")
            if operation and not operation.done():
                raise RuntimeError(f"Group {group} is already changing state")

        skip_service_ids = self._external_ready_service_ids(
            profile_id,
            observation,
        ) if observation is not None else frozenset()
        self._validate_group_start(group_id, skip_service_ids=skip_service_ids)
        affected = tuple(GROUP_DEFS["core"].services + GROUP_DEFS[group_id].services)
        self._schedule_operation(
            "launch",
            # start_launch owns its fresh probe and race-closing recheck;
            # do not request a duplicate preflight from the scheduler.
            lambda: self.start_launch(profile_id, resolved_runtime),
            affected,
            operation_kind="start",
        )

    def request_stop_launch(self) -> None:
        """Cancel an in-flight launch and stop every runtime profile plus Core."""
        _register_services(self.root)
        existing = self._operation_tasks.get("launch")
        if existing and not existing.done() and self._operation_kinds.get("launch") == "stop":
            return
        affected: list[str] = list(GROUP_DEFS["core"].services)
        for group_id in LAUNCH_PROFILE_GROUPS.values():
            affected.extend(GROUP_DEFS[group_id].services)
        self._schedule_operation(
            "launch",
            self.stop_launch,
            tuple(affected),
            operation_kind="stop",
            replace=True,
        )

    async def start_launch(
        self,
        profile_id: str,
        runtime: str | None = None,
        *,
        _force_external_probe: bool = False,
    ) -> None:
        """Run Core + selected profile transactionally, rolling back this launch on failure."""
        _register_services(self.root)
        group_id = LAUNCH_PROFILE_GROUPS.get(profile_id)
        if group_id is None:
            raise ValueError(f"Unknown launch profile: {profile_id}")

        resolved_runtime: str | None = None
        if profile_id != "potato":
            resolved_runtime = MultimodalRuntimeManager.normalize_profile(runtime or self._launch_runtime)
            MultimodalRuntimeManager.require_python(resolved_runtime)
            self._launch_runtime = resolved_runtime

        core_state = self.states.get("nachobot")
        manager_core_running = bool(
            core_state
            and core_state.status == ServiceStatus.RUNNING
            and self._manager_core_takes_precedence()
        )
        # Preserve the prior verified observation so a disappearance between
        # the HTTP preflight and this transaction cannot silently turn into a
        # replacement Core launch.
        previous_external = self._external_core_observation()
        # This method is also a valid internal entrypoint, so it must not rely
        # on the HTTP preflight or a prior UI poll for external-Core safety.
        # ``_force_external_probe`` remains accepted for older callers, but
        # the fresh probe is now unconditional.
        observation = await self.refresh_core_observation(force=True)
        external_core = False

        if not manager_core_running and not self._manager_core_takes_precedence():
            if observation is None:
                await self._resolve_uncertain_external_core_before_start()
            if previous_external is not None and observation is None:
                raise RuntimeError(
                    "外部 NachoBot Core 已消失，拒绝启动以避免接管或替换它"
                )
            if observation is not None:
                if observation.observed_profile != profile_id:
                    raise RuntimeError(
                        f"外部 NachoBot Core 当前为 {observation.observed_profile.upper()} 模式，"
                        f"无法切换为 {profile_id.upper()}；请在外部启动器中切换"
                    )
                external_core = True

        # Never silently reuse a manager-owned Core with a different or
        # unknown product profile. Stop it in this transaction and recreate it
        # with the requested environment before starting the profile group.
        core_started_by_transaction = False
        core_was_running = manager_core_running
        if not external_core and core_was_running and self._core_runtime_profile != profile_id:
            await self.stop_group("core")
            core_was_running = False
        if not external_core:
            core_env = {"NACHOBOT_RUNTIME_PROFILE": profile_id}
            if profile_id != "potato":
                core_env["NACHOBOT_TTS_ENDPOINT"] = _read_tts_service_endpoint(self.root)
                if profile_id == "full":
                    core_env["NACHOBOT_MULTIMODAL_ENDPOINT"] = _read_perception_service_endpoint(self.root)
            self._active_group_env["core"] = core_env
        self._external_profile_start_verified = profile_id if external_core else None
        try:
            if not external_core:
                await self.start_group("core")
                core_state = self.states.get("nachobot")
                if not core_state or core_state.status != ServiceStatus.RUNNING:
                    return
                core_started_by_transaction = not core_was_running
                self._core_runtime_profile = profile_id

            if external_core:
                # Close the preflight-to-dependent-start race.  A vanished or
                # changed external Core is never replaced; only dependents
                # already owned by WebUI may be rolled back below.
                rechecked = await self.refresh_core_observation(force=True)
                if rechecked is None or rechecked.observed_profile != profile_id:
                    raise RuntimeError(
                        "外部 NachoBot Core 已消失或模式不匹配，拒绝启动 WebUI 服务"
                    )

            await self.start_group(group_id)
            profile_ready = all(
                self.get_service_status(service_id)["status"] == ServiceStatus.RUNNING.value
                for service_id in GROUP_DEFS[group_id].services
            )
            if profile_ready:
                return

            await self.stop_group(group_id)
            if core_started_by_transaction:
                await self.stop_group("core")
        except asyncio.CancelledError:
            await self.stop_group(group_id)
            if core_started_by_transaction:
                await self.stop_group("core")
            raise
        except Exception:
            try:
                await self.stop_group(group_id)
                if core_started_by_transaction:
                    await self.stop_group("core")
            except Exception:
                logger.exception("Failed to roll back launch profile %s", profile_id)
            raise
        finally:
            if self._external_profile_start_verified == profile_id:
                self._external_profile_start_verified = None

    async def stop_launch(self) -> None:
        """Stop all mutually-exclusive runtime profiles, then stop Core."""
        _register_services(self.root)
        for group_id in LAUNCH_PROFILE_GROUPS.values():
            await self.stop_group(group_id)
        await self.stop_group("core")
        self._core_runtime_profile = None

    def _require_core_ready(self, consumer_service_id: str) -> None:
        """Require a manager-owned or verified external Core bus."""

        consumer_name = SERVICE_DEFS[consumer_service_id].name
        core_state = self.states.get("nachobot")
        if core_state is not None and self._manager_core_takes_precedence():
            if core_state.status == ServiceStatus.RUNNING:
                return
        elif self.core_observation is not None:
            return
        raise RuntimeError(
            f"Cannot start {consumer_name}: NachoBot Core is not ready. "
            "Start NachoBot Core first."
        )

    # Kept as a narrow compatibility alias for existing internal/test callers;
    # readiness, rather than ownership, is now the actual contract.
    def _require_core_owner(self, consumer_service_id: str) -> None:
        self._require_core_ready(consumer_service_id)

    def _ensure_required_components(self, service_ids: tuple[str, ...]):
        """Reject selected QQ starts before any process/task is scheduled."""
        selected = set(service_ids)
        if not selected.intersection(QQ_SERVICE_IDS):
            return None

        if selected.intersection({"snowluma_runtime", "snowluma_adapter"}):
            try:
                from .snowluma_manager import SnowLumaManager, SNOWLUMA_RELEASE_URL
            except ImportError:  # pragma: no cover - direct script context
                from snowluma_manager import SnowLumaManager, SNOWLUMA_RELEASE_URL
            try:
                resolution = SnowLumaManager.runtime_info(self.root)
            except Exception as exc:
                raise RuntimeError(
                    f"SnowLuma 运行时无法定位：{exc}；下载地址: {SNOWLUMA_RELEASE_URL}"
                ) from exc
            missing = SnowLumaManager.required_components(
                self.root, "snowluma", resolution=resolution
            )
            if missing:
                raise RuntimeError(
                    "SnowLuma 组件缺失，请重新部署 SnowLuma；"
                    f"缺少: {', '.join(missing)}；下载地址: {SNOWLUMA_RELEASE_URL}"
                )
            return resolution

        if selected.intersection({"napcat_adapter", "napcat_shell"}):
            try:
                from .snowluma_manager import SnowLumaManager
            except ImportError:  # pragma: no cover - direct script context
                from snowluma_manager import SnowLumaManager
            missing = SnowLumaManager.required_components(self.root, "napcat")
            if missing:
                raise RuntimeError(
                    "NapCat 组件缺失，请重新部署 NapCat；"
                    f"缺少: {', '.join(missing)}"
                )
        return None

    def _ensure_snowluma_launch_boundary(self, *, require_free_ports: bool = True) -> None:
        """Reject unsafe SnowLuma host/port state before spawning the launcher.

        The bundled runtime owns both the WebUI and OneBot listeners. Keep
        authoritative parsing and the loopback policy in snowluma_manager so
        the WebUI process manager and standalone BAT launchers share the same
        safety boundary.
        """
        try:
            from .snowluma_manager import SnowLumaError, SnowLumaManager
        except ImportError:  # pragma: no cover - direct script context
            from snowluma_manager import SnowLumaError, SnowLumaManager
        try:
            SnowLumaManager.validate_launch_boundary(
                self.root,
                require_free_ports=require_free_ports,
            )
        except SnowLumaError as exc:
            raise RuntimeError(f"Cannot start SnowLuma: {exc}") from exc

    def _validate_service_start(self, service_id: str) -> None:
        if service_id in QQ_SERVICE_IDS and _QQ_ADAPTER_SELECTOR_ERROR is not None:
            raise RuntimeError(_QQ_ADAPTER_SELECTOR_ERROR)

        if service_id in QQ_SERVICE_IDS:
            self._validate_qq_conflicts((service_id,))

        if service_id in EXTERNAL_ADAPTER_SERVICE_IDS:
            if service_id in self._check_cached_adapter_start_allowed((service_id,)):
                return

        # A direct adapter start may reuse an already-running SnowLuma
        # runtime. Otherwise validate webuiHost and both listeners before the
        # scheduler accepts the launch.
        if service_id == "snowluma_runtime":
            self._ensure_snowluma_launch_boundary()
        elif service_id == "snowluma_adapter":
            runtime_status = self.states.get("snowluma_runtime", ServiceState()).status
            runtime_external_ready = (
                not self._manager_service_takes_precedence("snowluma_runtime")
                and (
                    runtime_observation := self._external_adapter_observation("snowluma_runtime")
                ) is not None
                and runtime_observation.outcome == "external_ready"
            )
            self._ensure_snowluma_launch_boundary(
                require_free_ports=(
                    runtime_status != ServiceStatus.RUNNING and not runtime_external_ready
                ),
            )

        if service_id in QQ_SERVICE_IDS:
            qq_group = GROUP_DEFS.get("qq_adapter")
            selected_services = tuple(qq_group.services if qq_group else ())
            if service_id not in selected_services:
                raise RuntimeError(
                    f"Cannot start {SERVICE_DEFS[service_id].name}: "
                    "this QQ adapter is not selected in qq_adapter."
                )
        # Platform adapters connect directly to the Core WebSocket.
        if service_id in ("napcat_adapter", "snowluma_adapter", "koishi_adapter"):
            self._require_core_ready(service_id)

        # Direct service starts must preserve the same mutual exclusion that
        # group starts enforce for the shared public TTS endpoint.
        runtime_services = ("tts_runtime_full", "tts_runtime_lite")
        conflict_set = runtime_services if service_id in runtime_services else ()
        for other_id in conflict_set:
            if other_id == service_id:
                continue
            operation = self._operation_tasks.get(f"service:{other_id}")
            if operation and not operation.done():
                raise RuntimeError(
                    f"Cannot start {SERVICE_DEFS[service_id].name}: "
                    f"{SERVICE_DEFS[other_id].name} is already changing state."
                )
            state = self.states.get(other_id)
            if state and state.status in (ServiceStatus.RUNNING, ServiceStatus.STARTING):
                raise RuntimeError(
                    f"Cannot start {SERVICE_DEFS[service_id].name}: "
                    f"{SERVICE_DEFS[other_id].name} already owns the shared endpoint."
                )

    def _validate_group_start(
        self,
        group_id: str,
        *,
        skip_service_ids: frozenset[str] = frozenset(),
    ) -> None:
        gdef = GROUP_DEFS[group_id]
        self._validate_qq_conflicts(tuple(gdef.services))
        cached_adapter_skip = self._check_cached_adapter_start_allowed(tuple(gdef.services))
        skip_service_ids = frozenset(set(skip_service_ids) | set(cached_adapter_skip))
        services_to_start = tuple(
            service_id
            for service_id in gdef.services
            if service_id not in skip_service_ids
        )
        self._ensure_required_components(services_to_start)
        if "snowluma_runtime" in gdef.services or "snowluma_adapter" in gdef.services:
            # A QQ group can be recovered after its adapter task failed while
            # the manager-owned Runtime stayed healthy. In that case its
            # listeners are expected to be occupied by our own Runtime, so
            # only re-check the host policy; a fresh Runtime still requires
            # both configured ports to be free.
            runtime_running = (
                self.states.get("snowluma_runtime", ServiceState()).status
                == ServiceStatus.RUNNING
            )
            runtime_running = runtime_running or (
                not self._manager_service_takes_precedence("snowluma_runtime")
                and (
                    runtime_observation := self._external_adapter_observation("snowluma_runtime")
                ) is not None
                and runtime_observation.outcome == "external_ready"
            )
            self._ensure_snowluma_launch_boundary(require_free_ports=not runtime_running)
        for service_id in services_to_start:
            state = self.states.get(service_id)
            if not state or state.status not in (ServiceStatus.RUNNING, ServiceStatus.STARTING):
                self._validate_service_start(service_id)
            operation = self._operation_tasks.get(f"service:{service_id}")
            if operation and not operation.done():
                raise RuntimeError(
                    f"Cannot start {gdef.name}: {SERVICE_DEFS[service_id].name} is already changing state."
                )
        multimodal_groups = ("tts_full", "tts_lite", "potato")
        if group_id not in multimodal_groups:
            return
        for other in multimodal_groups:
            if other == group_id:
                continue
            operation = self._operation_tasks.get(f"group:{other}")
            if operation and not operation.done():
                raise RuntimeError(
                    f"Cannot start {gdef.name}: {GROUP_DEFS[other].name} is already changing state."
                )
            for sid in GROUP_DEFS[other].services:
                state = self.states.get(sid)
                if state and state.status in (ServiceStatus.RUNNING, ServiceStatus.STARTING):
                    raise RuntimeError(
                        f"Cannot start {gdef.name}: {GROUP_DEFS[other].name} is already running. Stop it first."
                    )

    async def start_service(self, service_id: str, *, _prepared: bool = False) -> None:
        _register_services(self.root)
        sdef = SERVICE_DEFS.get(service_id)
        if not sdef:
            raise ValueError(f"Unknown service: {service_id}")
        if service_id in EXTERNAL_ADAPTER_SERVICE_IDS:
            state_before_probe = self.states.get(service_id)
            try:
                if await self._fresh_adapter_start_guard(service_id):
                    self._clear_start_reservation(service_id, remove=True)
                    return
            except Exception:
                if service_state_is_pure_start_reservation(state_before_probe):
                    self._clear_start_reservation(service_id, remove=True)
                raise
        self._ensure_required_components((service_id,))
        if service_id == "nachobot":
            # Direct/internal Core starts must independently establish fresh
            # external evidence before creating a ServiceState or spawning.
            if await self._fresh_external_core_observation() is not None:
                self._clear_start_reservation(service_id, remove=True)
                return
        elif service_id in {"tts_runtime_full", "tts_runtime_lite", "perception"}:
            profile_id = next(
                (
                    profile
                    for profile, group_id in LAUNCH_PROFILE_GROUPS.items()
                    if service_id in GROUP_DEFS[group_id].services
                ),
                None,
            )
            previous_external = self._external_core_observation()
            observation = await self._fresh_external_core_observation()
            if (
                previous_external is not None
                and observation is None
                and not self._manager_core_takes_precedence()
            ):
                raise RuntimeError("外部 NachoBot Core 已消失，拒绝启动 WebUI 服务")
            if observation is not None:
                if profile_id is not None and observation.observed_profile != profile_id:
                    raise RuntimeError(
                        f"外部 NachoBot Core 当前为 {observation.observed_profile.upper()} 模式，"
                        f"无法启动 {service_id}；请在外部启动器中切换"
                    )
                if service_id in self._external_ready_service_ids(profile_id or "", observation):
                    self._clear_start_reservation(service_id, remove=True)
                    return
        elif service_id in {"napcat_adapter", "snowluma_adapter", "koishi_adapter"}:
            # Direct/internal callers must not rely on a prior UI poll for
            # Core readiness.  This probe is non-blocking to the event loop.
            await self.refresh_core_observation(force=True)
            self._require_core_ready(service_id)

        # The fresh probe above won the race only when it returned from the
        # external branches.  Reaching this point converts the reservation
        # into ordinary manager-owned startup immediately before the lock.
        self._clear_start_reservation(service_id)
        lock = self._service_locks.setdefault(service_id, asyncio.Lock())
        async with lock:
            await self._start_service_locked(service_id, sdef, _prepared=_prepared)

    async def _start_service_locked(
        self,
        service_id: str,
        sdef: ServiceDef,
        *,
        _prepared: bool,
    ) -> None:
        state = self.states.get(service_id)
        if state:
            if state.status == ServiceStatus.RUNNING:
                return
            if state.status == ServiceStatus.STARTING and not _prepared:
                return
            if service_state_retains_runtime(state):
                state.status = ServiceStatus.ERROR
                await self._broadcast(
                    service_id,
                    "[WebUI] ERROR: previous process/group still requires stop before starting again\n",
                )
                return

        if state is None:
            state = ServiceState()
            self.states[service_id] = state

        # ``start_service`` is also used directly by internal callers and
        # therefore cannot rely solely on request_start_group's synchronous
        # validation. Once the runtime is already active, an adapter-only
        # start must not mistake the runtime's own listeners for a conflict.
        if service_id == "snowluma_runtime" or service_id == "snowluma_adapter":
            runtime_status = self.states.get("snowluma_runtime", ServiceState()).status
            runtime_external_ready = (
                service_id == "snowluma_adapter"
                and not self._manager_service_takes_precedence("snowluma_runtime")
                and (
                    runtime_observation := self._external_adapter_observation("snowluma_runtime")
                ) is not None
                and runtime_observation.outcome == "external_ready"
            )
            try:
                self._ensure_snowluma_launch_boundary(
                    require_free_ports=(
                        service_id == "snowluma_runtime"
                        or (
                            runtime_status != ServiceStatus.RUNNING
                            and not runtime_external_ready
                        )
                    ),
                )
            except RuntimeError as exc:
                state.status = ServiceStatus.ERROR
                await self._broadcast(service_id, f"[WebUI] ERROR: {exc}\n")
                return
        state.start_reservation = False
        state.status = ServiceStatus.STARTING
        state.process = None
        state.pid = None
        state.process_group_id = None

        if sdef.wait_port and sdef.port and await asyncio.to_thread(self._port_is_open, sdef.port):
            state.status = ServiceStatus.ERROR
            await self._broadcast(
                service_id,
                f"[WebUI] ERROR: 端口 {sdef.port} 已被其他进程占用，已拒绝启动 {sdef.name}\n",
            )
            return

        await self._broadcast(service_id, f"[WebUI] Starting {sdef.name}...\n")

        # Resolve TTS engine dynamically
        try:
            cmd, cwd, env_extra = self._resolve_cmd(sdef)
            cmd, env_extra = self._resolve_multimodal_runtime_cmd(service_id, cmd, env_extra)
        except (FileNotFoundError, RuntimeError, ValueError) as e:
            state.status = ServiceStatus.ERROR
            await self._broadcast(service_id, f"[WebUI] ERROR: {e}\n")
            return
        if not cmd:
            state.status = ServiceStatus.ERROR
            await self._broadcast(service_id, f"[WebUI] ERROR: Cannot resolve command for {sdef.name}\n")
            return

        full_cwd = Path(cwd) if cwd and Path(cwd).is_absolute() else self.root / cwd if cwd else self.root

        # Build environment — remove WebUI's own venv to avoid
        # 'VIRTUAL_ENV does not match' warnings in child uv processes
        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["PYTHONNOUSERSITE"] = "1"
        # Force Python subprocesses to use UTF-8 output encoding
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env.update(sdef.env_extra)
        env.update(env_extra)
        core_profile = self._active_group_env.get("core", {}).get("NACHOBOT_RUNTIME_PROFILE")
        if service_id == "nachobot" and core_profile != "potato":
            # FULL/LITE Core calls the public TTS Runtime. Only FULL also
            # receives the local perception endpoint; POTATO receives neither.
            env["NACHOBOT_TTS_ENDPOINT"] = _read_tts_service_endpoint(self.root)
            if core_profile == "full":
                env["NACHOBOT_MULTIMODAL_ENDPOINT"] = _read_perception_service_endpoint(self.root)
            else:
                env.pop("NACHOBOT_MULTIMODAL_ENDPOINT", None)
        elif service_id == "nachobot":
            env.pop("NACHOBOT_TTS_ENDPOINT", None)
            env.pop("NACHOBOT_MULTIMODAL_ENDPOINT", None)
        env.update(self._active_group_env.get(sdef.group_id, {}))
        # The WebUI control-plane token is never a child-service credential,
        # including when a service/group override attempts to inject it.
        env.pop("NACHOBOT_WEBUI_TOKEN", None)

        if service_id == "nachobot":
            try:
                await self._prepare_playwright_chromium(service_id, full_cwd, env)
            except asyncio.CancelledError:
                state.status = ServiceStatus.STOPPED
                raise

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                # SnowLuma's launcher ends with ``pause`` for interactive
                # manual use.  A manager-owned runtime receives EOF instead
                # of an open pipe, so an early launcher failure cannot be
                # hidden behind that prompt.  Other services retain their
                # interactive stdin contract.
                stdin=(
                    asyncio.subprocess.DEVNULL
                    if service_id == "snowluma_runtime"
                    else asyncio.subprocess.PIPE
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(full_cwd),
                env=env,
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | _WINDOWS_CREATE_SUSPENDED
                    if os.name == "nt"
                    else 0
                ),
                start_new_session=os.name != "nt",
            )
            state.process = proc
            state.pid = proc.pid
            state.process_group_id = proc.pid if os.name != "nt" else None
            if os.name == "nt":
                try:
                    if not proc.pid:
                        raise RuntimeError("Windows child has no PID for Job Object assignment")
                    state.windows_job = await asyncio.to_thread(
                        self._get_windows_job_facade().create_assign_resume,
                        proc.pid,
                    )
                except Exception as exc:
                    pending_job = getattr(exc, "windows_job", None)
                    if pending_job is not None and not getattr(pending_job, "closed", False):
                        state.windows_job = pending_job
                    cleanup_ok = await self._reap_leader_after_start_failure(proc)
                    state.status = ServiceStatus.ERROR
                    if cleanup_ok:
                        state.process = None
                        state.pid = None
                    await self._broadcast(service_id, f"[WebUI] ERROR: cannot create Windows Job Object: {exc}\n")
                    return
                # Capture the manager-owned tree while the leader is known
                # alive.  This survives a later leader EOF/exit and avoids
                # making an unprovable descendant claim from a reused PID.
                if state.windows_job is None:
                    try:
                        owned_parent = psutil.Process(proc.pid)
                        state.windows_owned_processes = [owned_parent, *owned_parent.children(recursive=True)]
                    except psutil.NoSuchProcess:
                        state.windows_owned_processes = []
                    except Exception as exc:
                        state.status = ServiceStatus.ERROR
                        await self._broadcast(
                            service_id,
                            f"[WebUI] ERROR: cannot capture Windows process ownership: {exc}\n",
                        )
                        return
            state.started_at = time.time()

            # Start reading output
            state._read_task = asyncio.create_task(
                self._read_output(service_id, proc),
                name=f"webui:output:{service_id}",
            )

            await self._broadcast(service_id, f"[WebUI] {sdef.name} spawned (PID: {proc.pid})\n")
            if sdef.wait_port and sdef.port:
                # TTS engines may spend an arbitrary amount of time downloading
                # model assets on first launch. Do not treat a fixed readiness
                # deadline as a startup failure while the managed process is alive.
                readiness_timeout = None if service_id in ("tts_runtime_full", "tts_runtime_lite") else 180
                ready = await self._wait_for_port(service_id, sdef.port, timeout=readiness_timeout)
                if not ready:
                    await self._terminate_state_process(state)
                    state.status = ServiceStatus.ERROR
                    state.process = None
                    state.pid = None
                    await self._broadcast(
                        service_id,
                        f"[WebUI] ERROR: {sdef.name} 未通过就绪检查，进程已终止\n",
                    )
                    return
            if service_id in EXTERNAL_ADAPTER_SERVICE_IDS:
                await self._external_duplicate_after_spawn(service_id, state)
            # The output reader may observe EOF (or another failure) while
            # this readiness/broadcast await is yielding.  Never overwrite
            # that state with RUNNING, and retain the live process handle so
            # the caller can still request a bounded stop.
            if (
                state.status != ServiceStatus.STARTING
                or proc.returncode is not None
                or state.process is not proc
            ):
                if state.status == ServiceStatus.STARTING:
                    state.status = ServiceStatus.ERROR
                return
            state.status = ServiceStatus.RUNNING
            state.started_port = sdef.port
            await self._broadcast(service_id, f"[WebUI] {sdef.name} is ready.\n")

        except asyncio.CancelledError:
            await self._terminate_state_process(state)
            if state.status != ServiceStatus.STOPPING:
                state.status = ServiceStatus.STOPPED
            state.process = None
            state.pid = None
            state.started_port = None
            raise
        except Exception as e:
            try:
                await self._terminate_state_process(state)
            except Exception:
                state.status = ServiceStatus.ERROR
                await self._broadcast(service_id, f"[WebUI] ERROR starting {sdef.name}: {e}\n")
                return
            state.status = ServiceStatus.ERROR
            state.process = None
            state.pid = None
            state.started_port = None
            await self._broadcast(service_id, f"[WebUI] ERROR starting {sdef.name}: {e}\n")

    async def _external_duplicate_after_spawn(
        self,
        service_id: str,
        state: ServiceState,
    ) -> dict[str, AdapterObservation]:
        """Reject external duplicates after spawn, excluding only our process tree."""
        snapshot = await asyncio.to_thread(self._build_external_process_snapshot)
        records_by_pid = {record.pid: record for record in snapshot.processes}
        owned: set[int] = set()
        if state.pid is not None:
            owned.update(self._process_descendants(int(state.pid), records_by_pid))
        for process in state.windows_owned_processes:
            pid = getattr(process, "pid", None)
            if pid is not None:
                owned.add(int(pid))
        observations = self._classify_external_adapters(
            snapshot,
            exclude_pids=frozenset(owned),
        )
        self._adapter_observation_generation += 1
        self.adapter_observation_cache.update(observations)
        duplicate = observations.get(service_id)
        if duplicate is None or duplicate.outcome != "absent":
            raise RuntimeError(
                f"检测到并发外部 {SERVICE_DEFS[service_id].name}，已停止本次 WebUI 启动"
            )
        if service_id in QQ_SERVICE_IDS:
            self._validate_qq_conflicts((service_id,), observations=observations)
        return observations

    async def _reap_leader_after_start_failure(self, process: asyncio.subprocess.Process) -> bool:
        """Bounded terminate/kill/reap for a process that never became RUNNING."""
        try:
            if getattr(process, "returncode", None) is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=_PROCESS_REAP_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=_PROCESS_REAP_TIMEOUT)
                return True
            except Exception:
                return False
        except ProcessLookupError:
            return True
        except Exception:
            return False

    async def stop_service(self, service_id: str, *, _prepared: bool = False) -> None:
        _register_services(self.root)
        if service_id not in SERVICE_DEFS:
            raise ValueError(f"Unknown service: {service_id}")
        lock = self._service_locks.setdefault(service_id, asyncio.Lock())
        async with lock:
            await self._stop_service_locked(service_id, _prepared=_prepared)

    async def _stop_service_locked(self, service_id: str, *, _prepared: bool) -> None:
        state = self.states.get(service_id)
        if not state:
            return
        if (
            state.status == ServiceStatus.STOPPED
            and state.process_group_id is None
            and not state.windows_owned_processes
            and state.windows_job is None
            and state.process is None
        ):
            self._clear_start_reservation(service_id, remove=True)
            return

        sdef = SERVICE_DEFS[service_id]
        reservation_only = service_state_is_pure_start_reservation(state)
        if state.status == ServiceStatus.STOPPING and not _prepared:
            return
        state.status = ServiceStatus.STOPPING
        await self._broadcast(service_id, f"[WebUI] Stopping {sdef.name}...\n")
        try:
            await self._terminate_state_process(state)
        except Exception:
            state.status = ServiceStatus.ERROR
            await self._broadcast(
                service_id,
                f"[WebUI] ERROR: {sdef.name} 停止失败，可再次请求停止\n",
            )
            raise

        state.status = ServiceStatus.STOPPED
        state.process = None
        state.pid = None
        state.started_port = None
        await self._broadcast(service_id, f"[WebUI] {sdef.name} stopped.\n")
        if reservation_only:
            self._clear_start_reservation(service_id, remove=True)

    async def start_group(self, group_id: str) -> None:
        _register_services(self.root)
        gdef = GROUP_DEFS.get(group_id)
        if not gdef:
            raise ValueError(f"Unknown group: {group_id}")

        if group_id == "qq_adapter":
            # Internal callers can bypass the HTTP preflight. Refresh all
            # four QQ IDs here as well so a stale cache cannot let one backend
            # start beside an externally-present conflicting backend.
            await self._fresh_qq_start_guard(tuple(gdef.services))

        if group_id == "core":
            # Core group starts are valid internal entrypoints.  Reuse a
            # verified external Core instead of reaching the occupied port.
            if await self._fresh_external_core_observation() is not None:
                return

        external_observation: CoreObservation | None = None
        profile_id = next(
            (
                profile
                for profile, candidate_group in LAUNCH_PROFILE_GROUPS.items()
                if candidate_group == group_id
            ),
            None,
        )
        if profile_id is not None:
            previous_external = self._external_core_observation()
            if self._external_profile_start_verified == profile_id:
                external_observation = self._external_core_observation()
            else:
                external_observation = await self._fresh_external_core_observation()
            if (
                previous_external is not None
                and external_observation is None
                and not self._manager_core_takes_precedence()
            ):
                raise RuntimeError("外部 NachoBot Core 已消失，拒绝启动 WebUI 服务")
            if (
                external_observation is not None
                and external_observation.observed_profile != profile_id
            ):
                raise RuntimeError(
                    f"外部 NachoBot Core 当前为 {external_observation.observed_profile.upper()} 模式，"
                    f"无法启动 {profile_id.upper()}；请在外部启动器中切换"
                )

        skip_service_ids = (
            self._external_ready_service_ids(profile_id, external_observation)
            if profile_id is not None and external_observation is not None
            else frozenset()
        )
        if group_id == "qq_adapter":
            skip_service_ids = frozenset(
                set(skip_service_ids)
                | set(self._check_cached_adapter_start_allowed(tuple(gdef.services)))
            )
        self._ensure_required_components(
            tuple(service_id for service_id in gdef.services if service_id not in skip_service_ids)
        )
        if group_id == "qq_adapter":
            # Group starts can be requested without the HTTP preflight (for
            # example by an internal caller), so force readiness here too.
            await self.refresh_core_observation(force=True)

        # FULL and LITE own mutually-exclusive local TTS stacks. POTATO has no
        # child service and is represented by Core profile state only.
        if profile_id is None:
            self._validate_group_start(group_id)
        else:
            self._validate_group_start(group_id, skip_service_ids=skip_service_ids)

        started_here: list[str] = []
        try:
            if group_id == "vrchat":
                self._active_group_env[group_id] = {
                    VRCHAT_CAPABILITY_ENV: secrets.token_hex(32),
                }

            for sid in gdef.services:
                if sid in skip_service_ids:
                    continue
                prior = self.states.get(sid)
                was_running = bool(prior and prior.status == ServiceStatus.RUNNING)
                await self.start_service(sid)
                state = self.states.get(sid)
                if state and state.status == ServiceStatus.ERROR:
                    await self._broadcast(
                        sid,
                        f"[WebUI] {SERVICE_DEFS[sid].name} 启动失败，回滚本次已启动服务\n",
                    )
                    for started_id in reversed(started_here):
                        await self.stop_service(started_id)
                    self._active_group_env.pop(group_id, None)
                    return
                if not was_running and state and state.status == ServiceStatus.RUNNING:
                    started_here.append(sid)
                observed_status = self.get_service_status(sid)
                externally_ready = (
                    observed_status.get("status") == ServiceStatus.RUNNING.value
                    and observed_status.get("origin") == "external"
                )
                if externally_ready:
                    continue
                # start_service does not return until its readiness check succeeds.
                sdef = SERVICE_DEFS[sid]
                if not (sdef.wait_port and sdef.port):
                    await asyncio.sleep(2)
                    state = self.states.get(sid)
                    if not state or state.status != ServiceStatus.RUNNING:
                        for started_id in reversed(started_here):
                            await self.stop_service(started_id)
                        self._active_group_env.pop(group_id, None)
                        return
        except asyncio.CancelledError:
            for started_id in reversed(started_here):
                await self.stop_service(started_id)
            self._active_group_env.pop(group_id, None)
            raise
        except Exception:
            for started_id in reversed(started_here):
                try:
                    await self.stop_service(started_id)
                except Exception:
                    logger.exception(
                        "Failed to roll back %s after group %s start error",
                        started_id,
                        group_id,
                    )
            self._active_group_env.pop(group_id, None)
            raise

    async def stop_group(self, group_id: str) -> None:
        _register_services(self.root)
        gdef = GROUP_DEFS.get(group_id)
        if not gdef:
            raise ValueError(f"Unknown group: {group_id}")

        # Stop in reverse order
        for sid in reversed(gdef.services):
            await self.stop_service(sid)
        self._active_group_env.pop(group_id, None)
        if group_id == "core":
            self._core_runtime_profile = None

    async def shutdown(self) -> None:
        """Cancel managed operations, then stop every owned subprocess."""
        tasks = [task for task in self._operation_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._operation_tasks.clear()
        self._operation_kinds.clear()
        for service_id in list(self.states):
            try:
                await self.stop_service(service_id, _prepared=True)
            except Exception:
                logger.exception("Failed to stop WebUI service %s during shutdown", service_id)

    async def send_input(self, service_id: str, text: str) -> None:
        state = self.states.get(service_id)
        # Interactive startup prompts (notably NachoBot's EULA confirmation)
        # occur before the service readiness check can mark it RUNNING.  Once
        # the managed child exists, STARTING is therefore a valid stdin state.
        if (
            not state
            or state.status not in (ServiceStatus.STARTING, ServiceStatus.RUNNING)
            or not state.process
        ):
            raise ValueError(f"Service {service_id} is not accepting input")
        if not state.process.stdin:
            raise ValueError(f"Service {service_id} does not accept input")

        state.process.stdin.write(text.encode("utf-8"))
        await state.process.stdin.drain()

    # ---- WebSocket subscriber management ----

    def subscribe(self, service_id: str, callback: Callable):
        if service_id == "all":
            self._all_subscribers.append(callback)
        else:
            self._ws_subscribers.setdefault(service_id, []).append(callback)

    def unsubscribe(self, service_id: str, callback: Callable):
        if service_id == "all":
            self._all_subscribers = [c for c in self._all_subscribers if c is not callback]
        else:
            subs = self._ws_subscribers.get(service_id, [])
            self._ws_subscribers[service_id] = [c for c in subs if c is not callback]

    # ---- internal helpers ----

    async def _prepare_playwright_chromium(
        self,
        service_id: str,
        cwd: Path,
        env: dict[str, str],
    ) -> None:
        """Prepare Core's optional browser backend without blocking Core fallback startup."""
        script = cwd / "scripts" / "ensure_playwright.py"
        if not script.is_file():
            await self._broadcast(
                service_id,
                "[WebUI] WARN: Playwright preparation script is missing; web search may use HTTP fallback\n",
            )
            return

        await self._broadcast(service_id, "[WebUI] Checking Playwright Chromium...\n")
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "uv",
                "run",
                "python",
                "scripts/ensure_playwright.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
                env=env,
            )
            fallback_enc = locale.getpreferredencoding(False) or "gbk"
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8")
                except UnicodeDecodeError:
                    text = line.decode(fallback_enc, errors="replace")
                await self._broadcast(service_id, _ANSI_RE.sub("", text))
            await proc.wait()
            if proc.returncode != 0:
                await self._broadcast(
                    service_id,
                    "[WebUI] WARN: Playwright Chromium preparation failed; web search will use HTTP fallback\n",
                )
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.terminate()
                await proc.wait()
            raise
        except Exception as exc:
            await self._broadcast(
                service_id,
                f"[WebUI] WARN: Playwright Chromium preparation failed: {exc}; "
                "web search will use HTTP fallback\n",
            )

    async def _broadcast(self, service_id: str, line: str):
        """Push a log line to subscribers and the buffer."""
        state = self.states.get(service_id)
        if state is None:
            state = ServiceState()
            self.states[service_id] = state
        state.log_buffer.append(line)

        tagged = f"[{service_id}] {line}"

        for cb in self._ws_subscribers.get(service_id, []):
            try:
                await cb(line)
            except Exception:
                pass
        for cb in self._all_subscribers:
            try:
                await cb(tagged)
            except Exception:
                pass

    async def _read_output(self, service_id: str, proc: asyncio.subprocess.Process):
        """Continuously read stdout/stderr and broadcast."""
        # Determine fallback encoding for non-UTF-8 output (GBK on Chinese Windows)
        fallback_enc = locale.getpreferredencoding(False) or "gbk"
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                # Smart decode: try UTF-8 first, fall back to system encoding
                try:
                    text = line.decode("utf-8")
                except UnicodeDecodeError:
                    text = line.decode(fallback_enc, errors="replace")
                # Strip ANSI escape codes — the web terminal uses CSS styling
                text = _ANSI_RE.sub('', text)
                await self._broadcast(service_id, text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await self._broadcast(service_id, f"[WebUI] Read error: {e}\n")

        # Process ended
        state = self.states.get(service_id)
        if state and state.process is proc:
            rc = proc.returncode
            if rc is None:
                # A closed stdout pipe is not proof that the child exited.
                # Retain the process handle (and any owned POSIX group) so a
                # subsequent stop can still terminate it safely.
                if state.status != ServiceStatus.STOPPING:
                    state.status = ServiceStatus.ERROR
                    await self._broadcast(
                        service_id,
                        "[WebUI] Output stream closed while process is still running; stop required\n",
                    )
                return
            retain_windows_handle = os.name == "nt" and (
                state.status == ServiceStatus.STOPPING
                or bool(state.windows_owned_processes)
                or state.windows_job is not None
            )
            if not retain_windows_handle:
                state.process = None
                state.pid = None
            group_alive = False
            if os.name != "nt" and state.process_group_id is not None:
                try:
                    group_alive = self._posix_process_group_exists(state.process_group_id)
                except Exception as exc:
                    state.status = ServiceStatus.ERROR
                    await self._broadcast(
                        service_id,
                        f"[WebUI] Process leader exited (code: {rc}); cannot verify process group: {exc}\n",
                    )
                    return
                if not group_alive:
                    state.process_group_id = None
            if group_alive:
                # The leader is gone but descendants still own the group. Do
                # not report a clean STOPPED state; stop/shutdown can still
                # use the retained manager-owned group id.
                state.status = ServiceStatus.ERROR
                await self._broadcast(
                    service_id,
                    f"[WebUI] Process leader exited (code: {rc}); process group remains\n",
                )
            else:
                if os.name == "nt" and (state.windows_owned_processes or state.windows_job is not None):
                    # The leader's EOF/exit does not prove that the captured
                    # descendants are gone.  Keep the owned capability and
                    # require an explicit stop/reap pass.
                    state.status = ServiceStatus.ERROR
                    await self._broadcast(
                        service_id,
                        f"[WebUI] Process leader exited (code: {rc}); captured descendants require stop\n",
                    )
                else:
                    state.status = ServiceStatus.ERROR if rc and rc != 0 else ServiceStatus.STOPPED
                    await self._broadcast(service_id, f"[WebUI] Process exited (code: {rc})\n")

    @staticmethod
    def _port_is_open(port: int) -> bool:
        import socket

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except (ConnectionRefusedError, OSError, socket.timeout):
            return False

    async def _terminate_state_process(self, state: ServiceState) -> None:
        process = state.process
        if os.name == "nt":
            job_managed = state.windows_job is not None
            if state.windows_job is not None:
                job = state.windows_job
                facade = self._get_windows_job_facade()
                try:
                    await asyncio.to_thread(facade.terminate, job)
                    deadline = asyncio.get_running_loop().time() + _PROCESS_REAP_TIMEOUT
                    while True:
                        active = await asyncio.to_thread(facade.active_processes, job)
                        if active == 0:
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            raise RuntimeError("Windows Job Object still has active processes")
                        await asyncio.sleep(min(_WINDOWS_JOB_POLL_INTERVAL, deadline - asyncio.get_running_loop().time()))
                except Exception:
                    # Retain the capability for a later retry.  The caller
                    # will set ERROR and keep process/pid intact.
                    raise
                await asyncio.to_thread(facade.close, job)
                state.windows_job = None
                state.windows_owned_processes = []

            captured: list[Any] = [] if job_managed else list(state.windows_owned_processes)
            captured_by_pid: set[int] = {
                int(owned_pid)
                for owned_pid in (getattr(item, "pid", None) for item in captured)
                if owned_pid is not None
            }
            pid = state.pid or getattr(process, "pid", None)

            # Capture the complete manager-owned tree before sending any
            # signal.  The asyncio leader may exit as a side effect of the
            # first terminate, but these psutil handles remain usable for the
            # descendant kill/reap pass.
            if pid and not job_managed:
                try:
                    parent = psutil.Process(pid)
                except psutil.NoSuchProcess:
                    parent = None
                except Exception as exc:
                    raise RuntimeError(f"cannot inspect managed Windows process {pid}") from exc
                if parent is not None:
                    # Refresh descendants immediately before signalling; the
                    # initially captured set remains included even if the
                    # leader exits during this enumeration.
                    parent_pid = int(getattr(parent, "pid", pid))
                    if parent_pid not in captured_by_pid:
                        captured.append(parent)
                        captured_by_pid.add(parent_pid)
                    try:
                        descendants = parent.children(recursive=True)
                    except psutil.NoSuchProcess:
                        # The leader can disappear between Process() and
                        # children().  Retain the parent handle and continue;
                        # this must not discard any handles captured earlier.
                        descendants = []
                    except Exception as exc:
                        raise RuntimeError(f"cannot enumerate descendants of managed process {pid}") from exc
                    for child in descendants:
                        child_pid = getattr(child, "pid", None)
                        if child_pid is None or int(child_pid) in captured_by_pid:
                            continue
                        captured.append(child)
                        captured_by_pid.add(int(child_pid))

            if captured:
                state.windows_owned_processes = captured
                for owned in captured:
                    try:
                        owned.terminate()
                    except psutil.NoSuchProcess:
                        continue
                    except Exception as exc:
                        raise RuntimeError("cannot terminate a managed Windows process") from exc

                try:
                    _, survivors = await asyncio.to_thread(
                        psutil.wait_procs,
                        captured,
                        timeout=_PROCESS_REAP_TIMEOUT,
                    )
                except psutil.NoSuchProcess:
                    survivors = []
                except Exception as exc:
                    raise RuntimeError("cannot verify managed Windows process termination") from exc
                survivors = list(survivors or [])

                for remaining in survivors:
                    try:
                        remaining.kill()
                    except psutil.NoSuchProcess:
                        continue
                    except Exception as exc:
                        raise RuntimeError("cannot kill surviving managed Windows process") from exc

                if survivors:
                    try:
                        _, survivors_after_kill = await asyncio.to_thread(
                            psutil.wait_procs,
                            survivors,
                            timeout=_PROCESS_REAP_TIMEOUT,
                        )
                    except psutil.NoSuchProcess:
                        survivors_after_kill = []
                    except Exception as exc:
                        raise RuntimeError("cannot verify managed Windows process kill") from exc
                    survivors_after_kill = list(survivors_after_kill or [])
                else:
                    survivors_after_kill = []

                # wait_procs is advisory; verify each captured handle.  An
                # AccessDenied/unknown error is intentionally not interpreted
                # as success because STOPPED must mean confirmed gone.
                if survivors_after_kill:
                    raise RuntimeError("managed Windows process survived kill")
                for owned in captured:
                    try:
                        is_running = getattr(owned, "is_running", None)
                        if is_running is None:
                            raise RuntimeError("managed Windows process liveness API unavailable")
                        if is_running():
                            raise RuntimeError("managed Windows process remained alive")
                    except psutil.NoSuchProcess:
                        continue
                    except RuntimeError:
                        raise
                    except Exception as exc:
                        raise RuntimeError("cannot confirm managed Windows process liveness") from exc

                state.windows_owned_processes = []

            if process is not None:
                # Always reap the asyncio handle, including the case where the
                # leader exited before shutdown entered and psutil.Process(pid)
                # was already gone.  Without a PID/tree capability, this is
                # only leader cleanup; unknown descendants are not claimed.
                try:
                    if not captured and getattr(process, "returncode", None) is None:
                        try:
                            process.terminate()
                        except ProcessLookupError:
                            pass
                        except Exception as exc:
                            raise RuntimeError("cannot terminate managed Windows process leader") from exc
                    await asyncio.wait_for(process.wait(), timeout=_PROCESS_REAP_TIMEOUT)
                except ProcessLookupError:
                    pass
                except asyncio.TimeoutError as exc:
                    try:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=_PROCESS_REAP_TIMEOUT)
                    except ProcessLookupError:
                        pass
                    except asyncio.TimeoutError as kill_exc:
                        raise RuntimeError("managed Windows process leader was not reaped") from kill_exc
                    except Exception as kill_exc:
                        raise RuntimeError("cannot reap managed Windows process leader") from kill_exc
                except Exception as exc:
                    raise RuntimeError("cannot reap managed Windows process leader") from exc
        else:
            pgid = state.process_group_id
            group_gone = pgid is None
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    group_gone = True
                if not group_gone:
                    group_gone = await self._wait_for_posix_process_group_exit(
                        pgid,
                        _PROCESS_GROUP_TERM_TIMEOUT,
                    )
                if not group_gone:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        group_gone = True
                    if not group_gone:
                        group_gone = await self._wait_for_posix_process_group_exit(
                            pgid,
                            _PROCESS_GROUP_KILL_TIMEOUT,
                        )
                if not group_gone:
                    raise RuntimeError(f"process group {pgid} remained alive after SIGKILL")
                # Clear only after liveness confirmation. A failed shutdown
                # deliberately retains this capability for retry.
                state.process_group_id = None

            if process and process.returncode is None:
                try:
                    if pgid is None:
                        # Defensive fallback for a state assembled outside
                        # this manager. Real POSIX starts always have an owned
                        # pgid, so this path never derives one from a PID.
                        process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=_PROCESS_REAP_TIMEOUT)
                except asyncio.TimeoutError as exc:
                    if pgid is None:
                        process.kill()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=_PROCESS_REAP_TIMEOUT)
                        except asyncio.TimeoutError:
                            raise RuntimeError("managed process leader was not reaped") from exc
                    else:
                        raise RuntimeError("managed process leader was not reaped") from exc

        read_task = state._read_task
        if read_task and not read_task.done():
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
        state._read_task = None

    @staticmethod
    def _posix_process_group_exists(pgid: int) -> bool:
        """Check a manager-owned POSIX process group without signalling it."""
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            raise
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            raise
        return True

    async def _wait_for_posix_process_group_exit(self, pgid: int, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            if not self._posix_process_group_exists(pgid):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_PROCESS_GROUP_POLL_INTERVAL, remaining))

    @staticmethod
    def _health_mode_ready(port: int, expected_mode: str) -> bool:
        """Return whether the local adapter health endpoint reports the expected mode."""
        import json
        from urllib.request import Request, ProxyHandler, build_opener

        try:
            opener = build_opener(ProxyHandler({}))
            request = Request(
                f"http://127.0.0.1:{port}/api/health",
                headers={"Accept": "application/json"},
            )
            with opener.open(request, timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload.get("status") == "ok" and payload.get("mode") == expected_mode
        except Exception:
            return False

    async def _wait_for_port(self, service_id: str, port: int, timeout: int | None = 180) -> bool:
        """Wait for a port to become available until ready, process exit, or timeout."""
        import socket

        sdef = SERVICE_DEFS.get(service_id)
        if timeout is None:
            await self._broadcast(service_id, f"[WebUI] 等待端口 {port} 就绪（模型下载期间不会超时）...\n")
        else:
            await self._broadcast(service_id, f"[WebUI] 等待端口 {port} 就绪 (最长 {timeout}s)...\n")

        elapsed = 0
        while timeout is None or elapsed < timeout:
            # Check if the process died while we're waiting.
            state = self.states.get(service_id)
            if state and state.status in (ServiceStatus.STOPPED, ServiceStatus.ERROR):
                await self._broadcast(service_id, "[WebUI] 进程已退出，停止等待端口\n")
                return False

            if sdef and sdef.health_mode:
                ready = await asyncio.to_thread(
                    self._health_mode_ready,
                    port,
                    sdef.health_mode,
                )
                if ready:
                    await self._broadcast(
                        service_id,
                        f"[WebUI] Port {port} is ready ({sdef.health_mode}). ({elapsed + 1}s)\n",
                    )
                    return True
            else:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        await self._broadcast(
                            service_id,
                            f"[WebUI] Port {port} is ready. ({elapsed + 1}s)\n",
                        )
                        return True
                except (ConnectionRefusedError, OSError, socket.timeout):
                    pass

            await asyncio.sleep(1)
            elapsed += 1

        await self._broadcast(
            service_id,
            f"[WebUI] WARNING: Port {port} not ready after {timeout}s.\n",
        )
        return False

    def _resolve_multimodal_runtime_cmd(
        self,
        service_id: str,
        cmd: list[str],
        env_extra: dict[str, str],
    ) -> tuple[list[str], dict[str, str]]:
        """Replace local multimodal `uv run` commands with selected Python."""
        adapter_services = {
            "perception": ["-m", "nachobot_multimodal.api_server"],
        }
        args = adapter_services.get(service_id)
        if args is None:
            return cmd, env_extra

        runtime = self._launch_runtime
        python = MultimodalRuntimeManager.require_python(runtime)
        return [str(python), *args], dict(env_extra)

    def _resolve_cmd(self, sdef: ServiceDef) -> tuple[list[str], str, dict[str, str]]:
        """Resolve dynamic commands (e.g., TTS engine based on config)."""
        if sdef.id in ("tts_runtime_full", "tts_runtime_lite"):
            return self._resolve_tts_runtime_cmd()
        if sdef.id == "bilibili":
            nachobot_dir = self.root / "NachoBot"
            bili_dir = self.root / "NachoBot-Bilibili-Adapter"
            cmd = ["uv", "run", "--project", str(nachobot_dir), "python", "main.py"]
            env_extra = {"PYTHONPATH": f"{nachobot_dir};{bili_dir}"}
            return cmd, sdef.cwd, env_extra
        if sdef.id == "snowluma_runtime":
            try:
                from .snowluma_locator import resolve_snowluma_runtime
            except ImportError:  # pragma: no cover - direct module context
                from snowluma_locator import resolve_snowluma_runtime
            runtime = resolve_snowluma_runtime(self.root)
            launcher = runtime.path / "launcher.bat"
            if not launcher.is_file():
                raise FileNotFoundError(f"SnowLuma launcher.bat 不存在于已发现的 Runtime 目录")
            # The launcher owns the runtime's compatible Node selection.  Use
            # cmd's /d /s /c form without ``start`` so the process manager can
            # observe early launcher failure and keep it inside its Job Object.
            # Do not prepend the runtime directory to PATH: a release may not
            # contain a bundled Node executable, and startup is intentionally delegated to the
            # launcher's system ``node`` command.  The launcher remains the
            # only runtime command we execute.
            return ["cmd", "/d", "/s", "/c", "launcher.bat"], str(runtime.path), {}
        return sdef.cmd, sdef.cwd, {}

    def _resolve_tts_runtime_cmd(self) -> tuple[list[str], str, dict[str, str]]:
        """Start one public 9880 runtime that supervises its private backend."""
        adapter_dir = self.root / "NachoBot-Multimodal-Adapter"
        entrypoint = adapter_dir / "scripts" / "container_tts_entrypoint.py"
        if not entrypoint.is_file():
            raise FileNotFoundError(f"统一 TTS Runtime entrypoint 不存在: {entrypoint}")

        runtime = MultimodalRuntimeManager.normalize_profile(self._launch_runtime)
        python = MultimodalRuntimeManager.require_python(runtime)
        cmd = [
            str(python), str(entrypoint),
            "--host", "0.0.0.0",
            "--port", "9880",
            "--backend-port", "9881",
        ]
        torch_index = (
            "https://download.pytorch.org/whl/cpu"
            if runtime == "cpu"
            else "https://download.pytorch.org/whl/cu128"
        )
        return cmd, str(adapter_dir), {
            "NACHOBOT_TTS_TORCH_INDEX": torch_index,
            "NACHOBOT_TTS_RUNTIME_PROFILE": runtime,
        }
