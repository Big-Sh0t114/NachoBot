"""Windows lifecycle and console-log launcher for the standalone Live2D adapter."""

from __future__ import annotations

import argparse
import codecs
import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .config import AdapterConfig, load_config


POLL_SECONDS = 0.25
LOG_POLL_SECONDS = 0.02
CORE_PORT = 8000
BRIDGE_PORT = 8789
DEFAULT_RENDERER_READY_SECONDS = 35
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)


class LauncherError(RuntimeError):
    """Raised when a managed service cannot be started safely."""


@dataclass(frozen=True)
class LaunchIdentity:
    mode: str
    pet_pid: int
    pet_created: int


@dataclass(frozen=True)
class LaunchOutcome:
    exit_code: int
    status: str
    identity: LaunchIdentity | None = None
    log_tailers: tuple[LogTailer, ...] | None = field(default=None, compare=False, repr=False)


def _adapter_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _local_url_port(value: str) -> int | None:
    parsed = urlsplit(value)
    if parsed.hostname and parsed.hostname.casefold() in {"127.0.0.1", "localhost", "::1"}:
        return parsed.port
    return None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class WindowsMutex:
    """Named mutex used to serialize starts, switches, and shutdowns per adapter."""

    WAIT_OBJECT_0 = 0
    WAIT_ABANDONED = 0x80
    WAIT_TIMEOUT = 0x102
    INFINITE = 0xFFFFFFFF

    def __init__(self, name: str, *, initial_owner: bool = False) -> None:
        if os.name != "nt":
            raise LauncherError("Live2D lifecycle management requires Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        self._kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        self._kernel32.ReleaseMutex.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int
        ctypes.set_last_error(0)
        self.handle = self._kernel32.CreateMutexW(None, int(initial_owner), name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        already_exists = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        self.owned = bool(initial_owner and not already_exists)
        if initial_owner and already_exists:
            self.acquire(0)

    def acquire(self, timeout_ms: int = INFINITE) -> bool:
        result = self._kernel32.WaitForSingleObject(self.handle, timeout_ms)
        self.owned = result in {self.WAIT_OBJECT_0, self.WAIT_ABANDONED}
        if result not in {self.WAIT_OBJECT_0, self.WAIT_ABANDONED, self.WAIT_TIMEOUT}:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.owned

    def release(self) -> None:
        if self.owned:
            if not self._kernel32.ReleaseMutex(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.owned = False

    def close(self) -> None:
        if self.handle:
            if self.owned:
                self.release()
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaults", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJob:
    """Owns a process and every descendant started while it is in the job."""

    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self) -> None:
        self.handle: int | None = None
        if os.name != "nt":
            return
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._kernel32.SetInformationJobObject.restype = ctypes.c_int
        self._kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        self._kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.TerminateJobObject.restype = ctypes.c_int
        self._kernel32.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._kernel32.QueryInformationJobObject.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int
        self._kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self._kernel32.IsProcessInJob.restype = ctypes.c_int
        self._kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self._kernel32.OpenProcess.restype = ctypes.c_void_p

        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        info = _JobExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            handle,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: subprocess.Popen[Any]) -> None:
        if self.handle is not None and not self._kernel32.AssignProcessToJobObject(
            self.handle, ctypes.c_void_p(int(process._handle))
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def contains(self, pid: int) -> bool:
        if self.handle is None:
            return False
        process_handle = self._kernel32.OpenProcess(0x1000, False, pid)
        if not process_handle:
            return False
        try:
            result = ctypes.c_int()
            if not self._kernel32.IsProcessInJob(process_handle, self.handle, ctypes.byref(result)):
                return False
            return bool(result.value)
        finally:
            self._kernel32.CloseHandle(process_handle)

    def terminate(self, exit_code: int = 1) -> None:
        if self.handle is not None and not self._kernel32.TerminateJobObject(self.handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self) -> int:
        if self.handle is None:
            return 0
        info = _JobBasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            self.handle,
            1,  # JobObjectBasicAccountingInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            raise LauncherError(
                f"Could not query active processes in the owned Job Object: {ctypes.WinError(ctypes.get_last_error())}"
            )
        return int(info.ActiveProcesses)

    def wait_until_empty(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self.active_processes() == 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))

    def close(self) -> None:
        if self.handle is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[Any]
    job: WindowsJob
    stdout_path: Path
    stderr_path: Path
    _closed: bool = False
    listener_port: int | None = None
    listener_owner: int | None = None

    @classmethod
    def start(
        cls,
        name: str,
        command: list[str],
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        env: dict[str, str] | None = None,
    ) -> ManagedProcess:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_file = stdout_path.open("ab", buffering=0)
        stderr_file = stderr_path.open("ab", buffering=0)
        job = WindowsJob()
        try:
            if os.name == "nt":
                # Keep the venv's Python redirector suspended until it belongs
                # to the job. Its real interpreter and later descendants then
                # inherit job ownership before any user code can run.
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    creationflags=CREATE_NO_WINDOW | 0x00000004,
                )
            else:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
        except BaseException:
            job.close()
            raise
        finally:
            stdout_file.close()
            stderr_file.close()
        managed = cls(name, process, job, stdout_path, stderr_path)
        try:
            job.assign(process)
            if os.name == "nt":
                ntdll = ctypes.WinDLL("ntdll")
                ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
                ntdll.NtResumeProcess.restype = ctypes.c_long
                status = ntdll.NtResumeProcess(ctypes.c_void_p(int(process._handle)))
                if status < 0:
                    raise LauncherError(f"Could not resume managed process {name} (NTSTATUS {status:#x})")
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            managed.job.close()
            managed._closed = True
            raise
        return managed

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> int | None:
        return self.process.poll()

    def stop(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        deadline = time.monotonic() + timeout
        try:
            # Background model services may acquire their listener long after
            # startup. Record it only if it belongs to this process tree.
            if self.listener_port is not None and self.listener_owner is None:
                owner = listener_pid(self.listener_port)
                if owner is not None and (owner == self.pid or self.job.contains(owner)):
                    self.listener_owner = owner
            if self.job.handle is not None:
                self.job.terminate()
            elif self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                if self.job.handle is not None:
                    self.job.terminate()
                else:
                    self.process.kill()
                    self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            if self.job.handle is not None:
                remaining = max(0.0, deadline - time.monotonic())
                if not self.job.wait_until_empty(remaining):
                    # Closing a kill-on-close job will still terminate any members,
                    # but report that bounded orderly cleanup could not be confirmed.
                    raise LauncherError(
                        f"Timed out waiting for the {self.name} process tree to exit from its owned Job Object"
                    )
                self.process.wait(timeout=0)
            # TCP owner tables can briefly retain an exiting process after its
            # Job Object is empty. Confirm release of this exact owned listener;
            # a different owner on the same port is never touched or waited on.
            if self.listener_port is not None and self.listener_owner is not None:
                while listener_pid(self.listener_port) == self.listener_owner:
                    if time.monotonic() >= deadline:
                        raise LauncherError(f"Timed out releasing the {self.name} owned TCP listener")
                    time.sleep(0.01)
        finally:
            # Closing a kill-on-close job also removes descendants of a root
            # which exited before its children did.
            self.job.close()
            self._closed = True


def _tcp_listener_pids(port: int) -> list[int]:
    """Return listener PIDs from Windows' TCP owner tables."""
    if os.name != "nt":
        return []

    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_table = iphlpapi.GetExtendedTcpTable
    get_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    get_table.restype = ctypes.c_ulong
    pids: list[int] = []
    # AF_INET and AF_INET6; TCP_TABLE_OWNER_PID_LISTENER is 3.
    for family, row_size, port_offset, pid_offset in (
        (2, 24, 8, 20),
        (23, 56, 20, 52),
    ):
        size = ctypes.c_ulong()
        get_table(None, ctypes.byref(size), 0, family, 3, 0)
        if not size.value:
            continue
        buffer = ctypes.create_string_buffer(size.value)
        result = get_table(buffer, ctypes.byref(size), 0, family, 3, 0)
        if result != 0:
            continue
        count = ctypes.c_uint32.from_buffer(buffer).value
        for index in range(count):
            offset = ctypes.sizeof(ctypes.c_uint32) + index * row_size
            local_port = int.from_bytes(buffer[offset + port_offset : offset + port_offset + 4], "little")
            local_port = socket.ntohs(local_port & 0xFFFF)
            if local_port == port:
                pid = ctypes.c_uint32.from_buffer(buffer, offset + pid_offset).value
                if pid:
                    pids.append(pid)
    return list(dict.fromkeys(pids))


def listener_pid(port: int) -> int | None:
    pids = _tcp_listener_pids(port)
    if pids:
        return pids[0]
    if os.name != "nt":
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return -1
        except OSError:
            return None
    return None


def _port_is_open(port: int) -> bool:
    return listener_pid(port) is not None


def _wait_for_owned_listener(
    process: ManagedProcess,
    port: int,
    timeout: float,
    *,
    settle_seconds: float = 0.5,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    seen_owned = False
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled():
            raise LauncherError("Launcher console closed during startup")
        owner_pid = listener_pid(port)
        if owner_pid is not None:
            if owner_pid > 0 and (owner_pid == process.pid or process.job.contains(owner_pid)):
                seen_owned = True
                if time.monotonic() + settle_seconds >= deadline:
                    return True
                time.sleep(settle_seconds)
                stable_pid = listener_pid(port)
                if stable_pid == owner_pid and (stable_pid == process.pid or process.job.contains(stable_pid)):
                    return True
            else:
                process.stop()
                raise LauncherError(
                    f"Port {port} was claimed by an unrelated process while {process.name} started"
                )
        if process.poll() is not None:
            return False
        time.sleep(POLL_SECONDS)
    return seen_owned


@dataclass
class OwnedServices:
    log_dir: Path
    processes: list[ManagedProcess] = field(default_factory=list)
    cancelled: Callable[[], bool] | None = None

    def start(
        self,
        name: str,
        command: list[str],
        *,
        port: int,
        cwd: Path,
        timeout: float,
        reuse_existing: bool = False,
        env: dict[str, str] | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> ManagedProcess | None:
        existing = listener_pid(port)
        if existing is not None:
            if reuse_existing:
                return None
            raise LauncherError(f"Port {port} is already used by process {existing}")
        process = ManagedProcess.start(
            name,
            command,
            cwd=cwd,
            stdout_path=stdout_path or self.log_dir / f"{name}.out.log",
            stderr_path=stderr_path or self.log_dir / f"{name}.err.log",
            env=env,
        )
        self.processes.append(process)
        try:
            if not _wait_for_owned_listener(process, port, timeout, cancelled=self.cancelled):
                tail = _tail_file(process.stderr_path)
                raise LauncherError(f"{name} failed to become ready on port {port}.{tail}")
            current_owner = listener_pid(port)
            if current_owner is not None and (current_owner == process.pid or process.job.contains(current_owner)):
                process.listener_port = port
                process.listener_owner = current_owner
        except BaseException:
            self.stop()
            raise
        return process

    def stop(self) -> None:
        failure: Exception | None = None
        for process in reversed(self.processes):
            try:
                process.stop()
            except Exception as exc:
                # Still close every other owned job if one shutdown fails.
                if failure is None:
                    failure = exc
        self.processes.clear()
        if failure is not None:
            raise failure


def _tail_file(path: Path, limit: int = 30) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return ""
    return "\n" + "\n".join(lines) if lines else ""


@dataclass
class LaunchSession:
    mode: str
    config: AdapterConfig
    pet: ManagedProcess
    services: OwnedServices
    config_path: Path
    pet_created: int = 0
    log_viewers: list[subprocess.Popen[Any]] = field(default_factory=list)
    console_owners: list[tuple[int, int]] = field(default_factory=list)

    @property
    def identity(self) -> LaunchIdentity:
        return LaunchIdentity(self.mode, self.pet.pid, self.pet_created)

    def stop(self) -> None:
        self.services.stop()


class SessionStarter:
    """Starts a configured mode while tracking only its own process trees."""

    def __init__(self, adapter_dir: Path) -> None:
        self.adapter_dir = adapter_dir.resolve()
        self.repository_dir = self.adapter_dir.parent
        self.log_dir = self.adapter_dir / "logs"
        self.cancelled: Callable[[], bool] | None = None

    def start(self, config_path: Path, mode: str) -> LaunchSession:
        resolved_config = config_path.resolve()
        config = load_config(resolved_config, runtime_mode_override=mode)
        services = OwnedServices(self.log_dir, cancelled=self.cancelled)
        try:
            if mode == "desktop_pet" and not config.components.core:
                print("[Standalone] Rendering, tray and model controls only; no backend connection or capture.", flush=True)
            if mode == "desktop_pet":
                self._start_chat_services(config, services)
            python = self.adapter_dir / ".venv" / "Scripts" / "python.exe"
            if not python.is_file():
                raise LauncherError(f"Live2D Python environment is missing: {python}")
            command = [
                str(python),
                "-m",
                "live2d_adapter",
                "--config",
                str(resolved_config),
                "--mode",
                mode,
                "--log-file",
                str(self.log_dir / "live2d.log"),
            ]
            env = os.environ.copy()
            env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            if mode == "desktop_pet":
                env["NACHOBOT_LIVE2D_CONSOLE_COLOR"] = "1"
            pet = services.start(
                "live2d",
                command,
                port=config.server.port,
                cwd=self.adapter_dir,
                timeout=DEFAULT_RENDERER_READY_SECONDS,
                reuse_existing=False,
                env=env,
                stdout_path=self.log_dir / "live2d.out.log",
                stderr_path=self.log_dir / "live2d.err.log",
            )
            if pet is None:
                raise LauncherError("Renderer startup was unexpectedly reused")
            return LaunchSession(
                mode,
                config,
                pet,
                services,
                resolved_config,
                pet_created=_process_creation_ticks(pet.pid),
            )
        except BaseException:
            services.stop()
            raise

    def _start_chat_services(self, config: AdapterConfig, services: OwnedServices) -> None:
        chat = config.desktop_pet.chat
        if not chat.enabled:
            print("[INFO] Desktop chat is disabled; Core and bridge are not required.", flush=True)
            return

        core_port: int | None = None
        bridge_needed = False
        if chat.transport == "core":
            core_port = _local_url_port(chat.core_url)
            if core_port != CORE_PORT:
                core_port = None
        elif _local_url_port(chat.backend_url) == BRIDGE_PORT:
            core_port = CORE_PORT
            bridge_needed = True

        if core_port is not None and config.components.core:
            core_dir = self.repository_dir / "NachoBot"
            core_python = core_dir / ".venv" / "Scripts" / "python.exe"
            if not core_python.is_file() and listener_pid(CORE_PORT) is None:
                raise LauncherError(f"NachoBot Core environment is missing: {core_python}")
            if listener_pid(CORE_PORT) is None:
                services.start(
                    "core",
                    [str(core_python), "bot.py"],
                    port=CORE_PORT,
                    cwd=core_dir,
                    timeout=75,
                    reuse_existing=True,
                    env=_child_environment(),
                )

        if not bridge_needed:
            return

        bridge_dir = self.repository_dir / "NachoBot-Local-Host-Adapter"
        bridge_python = bridge_dir / ".venv" / "Scripts" / "python.exe"
        bridge_entry = bridge_dir / "main.py"
        if not bridge_python.is_file() or not bridge_entry.is_file():
            raise LauncherError(f"Desktop chat bridge environment is missing: {bridge_dir}")
        bridge_env = _child_environment()
        bridge_env["NACHOBOT_LOCAL_HOST_DESKTOP_MODE"] = "1"
        if listener_pid(BRIDGE_PORT) is None:
            services.start(
                "bridge",
                [str(bridge_python), str(bridge_entry)],
                port=BRIDGE_PORT,
                cwd=bridge_dir,
                timeout=30,
                reuse_existing=True,
                env=bridge_env,
                stdout_path=bridge_dir / "runtime" / "desktop-pet-local-host.out.log",
                stderr_path=bridge_dir / "runtime" / "desktop-pet-local-host.err.log",
            )
        if not _wait_bridge_connected():
            raise LauncherError("Desktop chat bridge started, but it did not connect to NachoBot Core")



def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return env


def _wait_bridge_connected(timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8789/api/status", timeout=2) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("status") == "ok" and data.get("core_connected"):
                return True
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    return False


class SessionSupervisor:
    def __init__(self, adapter_dir: Path, mutex_name: str | None = None) -> None:
        self.adapter_dir = adapter_dir.resolve()
        self.log_dir = self.adapter_dir / "logs"
        self.requests_dir = self.log_dir / "launcher-requests"
        self.state_path = self.log_dir / "launcher-state.json"
        self.mutex_name = mutex_name or _mutex_name(self.adapter_dir)
        self.session: LaunchSession | None = None
        self.starter = SessionStarter(self.adapter_dir)

    def run(self) -> int:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        mutex = WindowsMutex(self.mutex_name)
        try:
            mutex.acquire()
            self._state(status="idle", supervisor_pid=os.getpid())
            idle_since: float | None = None
            while True:
                requests = self._pending_requests()
                if requests:
                    idle_since = None
                    for request_path in requests:
                        self._handle_request(request_path)
                    continue
                if self.session is None:
                    if idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since >= 1.0:
                        break
                    time.sleep(POLL_SECONDS)
                    continue
                if self._stop_for_closed_console():
                    idle_since = time.monotonic()
                    continue
                try:
                    self.session.pet.process.wait(timeout=POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    continue
                else:
                    print("[STOP] Live2D process exited; cleaning its managed services.", flush=True)
                    old_session = self.session
                    self.session = None
                    old_session.stop()
                    self._state(status="stopped", mode=old_session.mode)
                    idle_since = time.monotonic()
            return 0
        finally:
            if self.session is not None:
                self.session.stop()
                self.session = None
            self._state(status="stopped", supervisor_pid=os.getpid())
            mutex.close()

    def _stop_for_closed_console(self) -> bool:
        session = self.session
        if session is None:
            return False
        viewer_closed = any(viewer.poll() is not None for viewer in session.log_viewers)
        launcher_closed = any(not _owner_alive(pid, created) for pid, created in session.console_owners)
        if not viewer_closed and not launcher_closed:
            return False
        self.session = None
        self._state(status="stopping", mode=session.mode)
        session.stop()
        self._state(status="stopped", mode=session.mode)
        print("[STOP] Session console closed; desktop pet and owned services stopped.", flush=True)
        return True

    def _pending_requests(self) -> list[Path]:
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        paths = list(self.requests_dir.glob("*.request.json"))
        return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))

    def _handle_request(self, request_path: Path) -> None:
        request = _read_json(request_path)
        request_path.unlink(missing_ok=True)
        if request is None:
            return
        request_id = str(request.get("id", ""))
        result_path = self.requests_dir / f"{request_id}.result.json"
        if request.get("action") == "stop":
            requested_mode = str(request.get("mode", ""))
            requested_pid = int(request.get("pet_pid", 0))
            requested_created = int(request.get("pet_created", 0))
            if (
                self.session is not None
                and self.session.mode == requested_mode
                and self.session.pet.pid == requested_pid
                and self.session.pet_created == requested_created
            ):
                previous = self.session
                self.session = None
                self._state(status="stopping", mode=previous.mode, request_id=request_id)
                previous.stop()
                self._state(status="stopped", mode=previous.mode)
                self._result(result_path, request_id, "stopped", pet_pid=requested_pid)
            else:
                self._result(result_path, request_id, "stale", pet_pid=requested_pid)
            return
        config_path = Path(str(request.get("config", ""))).resolve()
        mode = str(request.get("mode", ""))
        try:
            if mode not in {"desktop_pet", "live"}:
                raise LauncherError(f"Unknown runtime mode: {mode}")
            same_backend_mode = True
            if self.session and mode == "desktop_pet" and isinstance(self.starter, SessionStarter):
                requested_config = load_config(config_path, runtime_mode_override=mode)
                same_backend_mode = requested_config.components.core == self.session.config.components.core
            if self.session and self.session.pet.poll() is None and self.session.mode == mode and same_backend_mode:
                self._remember_launch_console(self.session, request)
                identity = self.session.identity
                self._result(
                    result_path,
                    request_id,
                    "reused",
                    mode=identity.mode,
                    pet_pid=identity.pet_pid,
                    pet_created=identity.pet_created,
                )
                return
            if self.session is not None:
                previous = self.session
                self.session = None
                self._state(status="stopping", mode=previous.mode, request_id=request_id)
                previous.stop()
            self._state(status="starting", mode=mode, request_id=request_id)
            startup_viewers: list[subprocess.Popen[Any]] = []
            if isinstance(self.starter, SessionStarter):
                config = load_config(config_path, runtime_mode_override=mode)
                if mode == "desktop_pet":
                    startup_viewers = self._open_configured_log_windows(config, request_id)
                pid, created = request.get("console_pid"), request.get("console_created")
                self.starter.cancelled = (
                    lambda: any(viewer.poll() is not None for viewer in startup_viewers)
                    or (isinstance(pid, int) and pid > 0 and isinstance(created, int) and created > 0
                        and not _owner_alive(pid, created))
                )
            session = self.starter.start(config_path, mode)
            session.log_viewers.extend(startup_viewers)
            self.session = session
            self._remember_launch_console(session, request)
            self._state(
                status="ready",
                mode=mode,
                request_id=request_id,
                pet_pid=session.pet.pid,
                pet_created=session.pet_created,
            )
            identity = session.identity
            self._result(
                result_path,
                request_id,
                "started",
                mode=identity.mode,
                pet_pid=identity.pet_pid,
                pet_created=identity.pet_created,
            )
        except BaseException as exc:
            if self.session is not None:
                failed = self.session
                self.session = None
                failed.stop()
            self._state(status="failed", mode=mode, request_id=request_id, error=str(exc))
            self._result(result_path, request_id, "failed", mode=mode, error=str(exc))

    def _state(self, **values: Any) -> None:
        previous = _read_json(self.state_path) or {}
        if values.get("status") != "ready":
            previous.pop("pet_pid", None)
            previous.pop("pet_created", None)
        previous.update(values)
        previous["updated_at"] = time.time()
        _write_json(self.state_path, previous)

    @staticmethod
    def _result(path: Path, request_id: str, status: str, **values: Any) -> None:
        _write_json(path, {"id": request_id, "status": status, **values})

    @staticmethod
    def _remember_launch_console(session: LaunchSession, request: dict[str, Any]) -> None:
        pid, created = request.get("console_pid"), request.get("console_created")
        if isinstance(pid, int) and pid > 0 and isinstance(created, int) and created > 0:
            owner = (pid, created)
            if owner not in session.console_owners:
                session.console_owners.append(owner)

    def _open_configured_log_windows(self, config: AdapterConfig, request_id: str) -> list[subprocess.Popen[Any]]:
        viewers = []
        chat = config.desktop_pet.chat
        needs_core = config.components.core and chat.enabled and (
            (chat.transport == "core" and _local_url_port(chat.core_url) == CORE_PORT)
            or (chat.transport == "http" and _local_url_port(chat.backend_url) == BRIDGE_PORT)
        )
        if config.desktop_pet.show_core_console and needs_core and listener_pid(CORE_PORT) is None:
            viewer = _open_log_viewer(self.adapter_dir, "CORE", os.getpid(), _process_creation_ticks(os.getpid()),
                                      session_id=request_id, start_at_end=True)
            if viewer is not None:
                viewers.append(viewer)
        return viewers


def _process_creation_ticks(pid: int) -> int:
    if os.name != "nt":
        return 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return 0
    try:
        created = ctypes.c_uint64()
        exited = ctypes.c_uint64()
        kernel = ctypes.c_uint64()
        user = ctypes.c_uint64()
        if kernel32.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return 0 if exited.value else int(created.value)
        return 0
    finally:
        kernel32.CloseHandle(process)


def _mutex_name(adapter_dir: Path) -> str:
    import hashlib

    key = hashlib.sha256(str(adapter_dir.resolve()).casefold().encode("utf-8")).hexdigest()
    return f"Local\\NachoBotLive2D_{key}"


def _enqueue_request(
    adapter_dir: Path,
    config: Path,
    mode: str,
    *,
    action: str | None = None,
    pet_pid: int | None = None,
    pet_created: int | None = None,
) -> tuple[str, Path]:
    requests_dir = adapter_dir / "logs" / "launcher-requests"
    request_id = uuid.uuid4().hex
    request_path = requests_dir / f"{request_id}.request.json"
    request = {"id": request_id, "config": str(config.resolve()), "mode": mode}
    if action is None:
        request.update(console_pid=os.getpid(), console_created=_process_creation_ticks(os.getpid()))
    if action is not None:
        request.update(action=action, pet_pid=pet_pid, pet_created=pet_created)
    _write_json(request_path, request)
    return request_id, requests_dir


def _spawn_supervisor_if_available(adapter_dir: Path, mutex_name: str) -> subprocess.Popen[Any] | None:
    """Start a supervisor only after acquiring the session mutex."""
    mutex = WindowsMutex(mutex_name, initial_owner=True)
    try:
        if not mutex.owned:
            return None
        process = _spawn_supervisor(adapter_dir, mutex_name)
        mutex.release()
        return process
    finally:
        mutex.close()


def _wait_for_result(
    request_id: str,
    requests_dir: Path,
    timeout: float = 150.0,
    *,
    adapter_dir: Path | None = None,
    mutex_name: str | None = None,
    on_poll: Callable[[], None] | None = None,
) -> dict[str, Any]:
    spawned_supervisor: subprocess.Popen[Any] | None = None
    result_path = requests_dir / f"{request_id}.result.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if on_poll is not None:
            on_poll()
        result = _read_json(result_path)
        if result is not None:
            result_path.unlink(missing_ok=True)
            return result
        if adapter_dir is not None and mutex_name is not None:
            if spawned_supervisor is None or spawned_supervisor.poll() is not None:
                spawned_supervisor = _spawn_supervisor_if_available(adapter_dir, mutex_name)
        time.sleep(LOG_POLL_SECONDS if on_poll is not None else POLL_SECONDS)
    raise LauncherError(f"Timed out waiting for Live2D startup result ({request_id})")


def _spawn_supervisor(adapter_dir: Path, mutex_name: str) -> subprocess.Popen[Any]:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.is_file() else Path(sys.executable)
    log_dir = adapter_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / "desktop-session.out.log").open("ab", buffering=0)
    stderr = (log_dir / "desktop-session.err.log").open("ab", buffering=0)
    try:
        return subprocess.Popen(
            [
                str(executable),
                "-m",
                "live2d_adapter.launcher",
                "--_supervisor",
                "--_adapter-dir",
                str(adapter_dir),
                "--_mutex-name",
                mutex_name,
            ],
            cwd=adapter_dir,
            env=_child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    finally:
        stdout.close()
        stderr.close()


def launch(config_path: Path, mode: str) -> LaunchOutcome:
    adapter_dir = _adapter_root()
    config_path = config_path.resolve()
    log_dir = adapter_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    mutex_name = _mutex_name(adapter_dir)
    tailers = tuple(LogTailer(log_dir / name) for name in
                    ("live2d.out.log", "live2d.err.log"))
    for tailer in tailers:
        tailer.seek_end()

    def print_startup_logs() -> None:
        for tailer in tailers:
            content = tailer.read_available()
            if content:
                _print_log_content(content)

    request_id, requests_dir = _enqueue_request(adapter_dir, config_path, mode)
    result = _wait_for_result(
        request_id,
        requests_dir,
        adapter_dir=adapter_dir,
        mutex_name=mutex_name,
        on_poll=print_startup_logs,
    )
    if result.get("status") == "failed":
        print(f"[ERROR] {result.get('error', 'Live2D startup failed')}", file=sys.stderr)
        return LaunchOutcome(1, "failed")
    status = result.get("status")
    result_mode = result.get("mode")
    pet_pid = result.get("pet_pid")
    pet_created = result.get("pet_created")
    if (
        status not in {"started", "reused"}
        or result_mode != mode
        or not isinstance(pet_pid, int)
        or pet_pid <= 0
        or not isinstance(pet_created, int)
    ):
        raise LauncherError("Supervisor returned an invalid Live2D session identity")
    identity = LaunchIdentity(result_mode, pet_pid, pet_created)
    if status == "started":
        print(f"[OK] Live2D {mode} mode is ready (PID {result.get('pet_pid')}).")
    else:
        print(f"[OK] Live2D {mode} mode is already running.")
    return LaunchOutcome(0, status, identity, tailers)


class LogTailer:
    """Incrementally decodes UTF-8 while preserving ANSI and line bytes."""

    def __init__(self, path: Path, cursor: dict[str, Any] | None = None) -> None:
        self.path = path
        self.position = 0
        self.identity: tuple[int, int] | None = None
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        if cursor:
            offset, identity = cursor.get("offset"), cursor.get("identity")
            if isinstance(offset, int) and offset >= 0 and isinstance(identity, list) and len(identity) == 2:
                self.position = offset
                self.identity = tuple(identity)

    def seek_end(self) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            return
        self.position = stat.st_size
        self.identity = (stat.st_dev, stat.st_ino)
        self.decoder.reset()

    def cursor(self) -> dict[str, Any]:
        return {"offset": self.position, "identity": list(self.identity) if self.identity else None}

    def read_available(self) -> str:
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.identity != identity or stat.st_size < self.position:
                    self.identity = identity
                    self.position = 0
                    self.decoder.reset()
                stream.seek(self.position)
                chunks = []
                while chunk := stream.read(8192):
                    chunks.append(chunk)
                    self.position = stream.tell()
        except OSError:
            return ""
        return "".join(self.decoder.decode(chunk, final=False) for chunk in chunks)


def _print_log_content(content: str) -> None:
    for line in content.splitlines(keepends=True):
        sys.stdout.write(line)
        sys.stdout.flush()


def _log_paths(adapter_dir: Path, source_mode: str) -> list[Path]:
    modes = ("CORE", "LIVE2D") if source_mode == "ALL" else (source_mode,)
    return [adapter_dir / "logs" / f"{mode.lower()}.{stream}.log" for mode in modes for stream in ("out", "err")]


def _log_session_alive(adapter_dir: Path, session_id: str | None) -> bool:
    if not session_id:
        return True
    state = _read_json(adapter_dir / "logs" / "launcher-state.json")
    if state is None:
        # A transient read failure must not make an otherwise healthy log
        # window exit and trigger session cleanup. The supervisor PID is also
        # checked independently on every iteration.
        return True
    return state.get("request_id") == session_id and state.get("status") in {"starting", "ready"}


def _enable_ansi_console() -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [ctypes.c_int]
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetConsoleMode.restype = ctypes.c_int
    kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetConsoleMode.restype = ctypes.c_int
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if handle and kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def _owner_alive(pid: int | None, created: int | None) -> bool:
    if pid is None or pid <= 0:
        return True
    current_created = _process_creation_ticks(pid)
    if current_created == 0:
        return False
    return created in (None, 0, current_created)


def _bind_viewer_console() -> None:
    """A new console must not keep the hidden supervisor's redirected handles."""
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    kernel32.SetConsoleOutputCP.argtypes = [ctypes.c_uint]
    if not kernel32.GetConsoleWindow():
        raise LauncherError("The log viewer has no console attached.")
    kernel32.SetConsoleOutputCP(65001)
    output = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    errors = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.__stdout__ = output
    sys.stderr = sys.__stderr__ = errors


def show_logs(adapter_dir: Path, source_mode: str, owner_pid: int | None = None, owner_created: int | None = None,
              *, session_id: str | None = None, cursors: list[dict[str, Any]] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    _enable_ansi_console()
    if os.name == "nt":
        ctypes.WinDLL("kernel32").SetConsoleTitleW(f"NachoBot {source_mode} Logs")
    print(f"NachoBot · {source_mode} 实时日志", flush=True)
    print("关闭启动时的控制台将结束桌宠和本次启动的服务。等待日志……\n", flush=True)
    paths = _log_paths(adapter_dir, source_mode)
    tailers = [LogTailer(path, cursors[index] if cursors and index < len(cursors) else None)
               for index, path in enumerate(paths)]
    while _owner_alive(owner_pid, owner_created) and _log_session_alive(adapter_dir, session_id):
        for tailer in tailers:
            content = tailer.read_available()
            if content:
                _print_log_content(content)
        time.sleep(LOG_POLL_SECONDS)
    return 0


def _open_log_viewer(adapter_dir: Path, source_mode: str, owner_pid: int, owner_created: int,
                     *, session_id: str | None = None, start_at_end: bool = False) -> subprocess.Popen[Any]:
    python = Path(sys.executable).with_name("python.exe")
    if not python.is_file():
        python = Path(sys.executable)
    extra = []
    if session_id:
        extra.extend(["--_session-id", session_id])
    if start_at_end:
        tailers = [LogTailer(path) for path in _log_paths(adapter_dir, source_mode)]
        for tailer in tailers:
            tailer.seek_end()
        extra.extend(["--_log-cursors", json.dumps([tailer.cursor() for tailer in tailers])])
    return subprocess.Popen(
        [
            str(python),
            "-u",
            "-m",
            "live2d_adapter.launcher",
            "--_view-logs",
            source_mode,
            "--_adapter-dir",
            str(adapter_dir),
            "--_owner-pid",
            str(owner_pid),
            "--_owner-created",
            str(owner_created),
            *extra,
        ],
        cwd=adapter_dir,
        env=_child_environment(),
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NEW_CONSOLE if os.name == "nt" else 0,
    )


def _show_logs_from_entry(adapter_dir: Path, source_mode: str) -> int:
    state = _read_json(adapter_dir / "logs" / "launcher-state.json") or {}
    owner_pid = state.get("pet_pid")
    owner_created = state.get("pet_created")
    python = Path(sys.executable).with_name("python.exe")
    if not python.is_file():
        python = Path(sys.executable)
    subprocess.Popen(
        [
            str(python),
            "-u",
            "-m",
            "live2d_adapter.launcher",
            "--_view-logs",
            source_mode,
            "--_adapter-dir",
            str(adapter_dir),
            "--_owner-pid",
            str(owner_pid or 0),
            "--_owner-created",
            str(owner_created or 0),
        ],
        cwd=adapter_dir,
        env=_child_environment(),
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NEW_CONSOLE if os.name == "nt" else 0,
    )
    print(f"[OK] {source_mode} log window opened.")
    return 0


def follow_live_session(adapter_dir: Path, identity: LaunchIdentity, *, tailers: tuple[LogTailer, ...] | None = None) -> int:
    """Keep either mode's logs in the original adapter console, owning that session."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    _enable_ansi_console()
    print("[Live2D Adapter] 实时日志 · Ctrl+C 或关闭此终端退出本次会话。", flush=True)
    log_dir = adapter_dir / "logs"
    tailers = tailers if tailers is not None else (
        LogTailer(log_dir / "live2d.out.log"),
        LogTailer(log_dir / "live2d.err.log"),
    )
    try:
        while _owner_alive(identity.pet_pid, identity.pet_created):
            for tailer in tailers:
                content = tailer.read_available()
                if content:
                    _print_log_content(content)
            time.sleep(LOG_POLL_SECONDS)
    except KeyboardInterrupt:
        request_id, requests_dir = _enqueue_request(
            adapter_dir,
            adapter_dir / "config.toml",
            identity.mode,
            action="stop",
            pet_pid=identity.pet_pid,
            pet_created=identity.pet_created,
        )
        result = _wait_for_result(request_id, requests_dir, timeout=20)
        if result.get("status") == "stopped":
            label = "桌宠" if identity.mode == "desktop_pet" else "Live mode"
            print(f"\n[STOP] {label} stopped.")
        else:
            print("\n[INFO] Live mode had already ended.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the NachoBot Live2D adapter.")
    parser.add_argument("--config", type=Path, default=_adapter_root() / "config.toml")
    parser.add_argument("--mode", choices=("desktop_pet", "live"))
    parser.add_argument("--show-logs", choices=("ALL", "LIVE2D", "CORE"))
    parser.add_argument("--_supervisor", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_adapter-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_mutex-name", help=argparse.SUPPRESS)
    parser.add_argument("--_view-logs", choices=("ALL", "LIVE2D", "CORE"), help=argparse.SUPPRESS)
    parser.add_argument("--_session-id", help=argparse.SUPPRESS)
    parser.add_argument("--_log-cursors", help=argparse.SUPPRESS)
    parser.add_argument("--_owner-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--_owner-created", type=int, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args._view_logs:
        _bind_viewer_console()
        if args._session_id or args._log_cursors:
            return show_logs(args._adapter_dir or _adapter_root(), args._view_logs, args._owner_pid, args._owner_created,
                             session_id=args._session_id, cursors=json.loads(args._log_cursors) if args._log_cursors else None)
        return show_logs(args._adapter_dir or _adapter_root(), args._view_logs, args._owner_pid, args._owner_created)
    if args._supervisor:
        try:
            return SessionSupervisor(args._adapter_dir or _adapter_root(), args._mutex_name).run()
        except Exception as exc:
            print(f"[ERROR] Live2D supervisor failed: {exc}", file=sys.stderr, flush=True)
            return 1
    if args.show_logs:
        return _show_logs_from_entry(_adapter_root(), args.show_logs)
    try:
        config = load_config(args.config, runtime_mode_override=args.mode)
        _enable_ansi_console()
        result = launch(args.config, config.runtime.mode)
        if result.exit_code != 0:
            return result.exit_code
        if result.identity is not None:
            if result.log_tailers is not None:
                return follow_live_session(_adapter_root(), result.identity, tailers=result.log_tailers)
            return follow_live_session(_adapter_root(), result.identity)
        return result.exit_code
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
