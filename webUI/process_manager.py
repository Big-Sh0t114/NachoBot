"""
NachoBot WebUI — Process Manager
Manages service subprocess lifecycles, log capture, and WebSocket broadcasting.
"""

import asyncio
import ctypes
import errno
import locale
import logging
import os
import re
import secrets
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from ctypes import wintypes

import psutil

try:
    from .multimodal_runtime import MultimodalRuntimeManager
except ImportError:
    from multimodal_runtime import MultimodalRuntimeManager

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

_TTS_ENGINE_CONFIG_FILES = {
    "GPT_Sovits": "gpt-sovits.toml",
    "Vox": "vox.toml",
}


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


def _read_tts_engine_port(root_dir: Path, engine: str) -> int:
    """Read the selected engine port from its dedicated TOML config."""
    config_name = _TTS_ENGINE_CONFIG_FILES.get(engine)
    if not config_name:
        return 9880

    config_path = root_dir / "NachoBot-Multimodal-Adapter" / "configs" / config_name
    try:
        import tomlkit

        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        return int(document.get("tts", {}).get("port", 9880))
    except Exception:
        return 9880


class ServiceStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


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


def _register_services():
    """Build the service and group lookup tables by dynamically reading adapter configs."""
    global SERVICE_DEFS, GROUP_DEFS
    from webui_config import webui_config
    import tomlkit
    import re

    # 1. Parse NachoBot .env
    nachobot_host = "127.0.0.1"
    nachobot_port = 8000
    env_path = ROOT_DIR / "NachoBot" / ".env"
    if env_path.exists():
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
    napcat_config_path = ROOT_DIR / "NachoBot-Napcat-Adapter" / "config.toml"
    if napcat_config_path.exists():
        try:
            doc = tomlkit.parse(napcat_config_path.read_text(encoding="utf-8"))
            napcat_server = doc.get("napcat_server", {})
            napcat_host = napcat_server.get("host", napcat_host)
            napcat_port = int(napcat_server.get("port", napcat_port))
        except Exception:
            pass

    # 3. Parse multimodal adapter base.toml & enabled engine port
    tts_adapter_host = "127.0.0.1"
    tts_adapter_port = 8070
    tts_engine_port = 9880
    tts_base_path = ROOT_DIR / "NachoBot-Multimodal-Adapter" / "configs" / "base.toml"
    if tts_base_path.exists():
        try:
            doc = tomlkit.parse(tts_base_path.read_text(encoding="utf-8"))
            server_sec = doc.get("server", {})
            tts_adapter_host = server_sec.get("host", tts_adapter_host)
            tts_adapter_port = int(server_sec.get("port", tts_adapter_port))
            
            # Determine enabled engine
            enabled = doc.get("enabled_tts", {}).get("enabled", ["GPT_Sovits"])
            engine = "GPT_Sovits"
            if isinstance(enabled, list) and "Vox" in enabled:
                engine = "Vox"

            tts_engine_port = _read_tts_engine_port(ROOT_DIR, engine)
        except Exception:
            pass

    # 4. Parse Perception configs/perception.toml
    perception_host = "127.0.0.1"
    perception_port = 9874
    perception_config_path = ROOT_DIR / "NachoBot-Multimodal-Adapter" / "configs" / "perception.toml"
    if perception_config_path.exists():
        try:
            doc = tomlkit.parse(perception_config_path.read_text(encoding="utf-8"))
            percep_sec = doc.get("perception", {})
            perception_host = percep_sec.get("host", perception_host)
            perception_port = int(percep_sec.get("port", perception_port))
        except Exception:
            pass

    # 5. Parse standalone Live2D adapter config
    live2d_host = "127.0.0.1"
    live2d_port = 8766
    live2d_config_path = ROOT_DIR / "NachoBot-Live2D-Adapter" / "config.toml"
    if live2d_config_path.exists():
        try:
            doc = tomlkit.parse(live2d_config_path.read_text(encoding="utf-8"))
            live2d_server = doc.get("server", {})
            live2d_host = live2d_server.get("host", live2d_host)
            live2d_port = int(live2d_server.get("port", live2d_port))
        except Exception:
            pass

    # 6. Parse Koishi configs/koishi.yml
    koishi_port = 5140
    koishi_yml_path = ROOT_DIR / "koishi-app" / "koishi.yml"
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

        # ── Multimodal FULL ──
        ServiceDef("tts_engine_full", "TTS 推理运行时", "tts_full", "", [],
                   port=tts_engine_port, wait_port=True, order=1,
                   detail=f"GPT-SoVITS / VoxCPM · :{tts_engine_port}"),  # dynamic
        ServiceDef("tts_adapter_full", "多模态适配器（TTS）", "tts_full",
                   "NachoBot-Multimodal-Adapter", ["uv", "run", "python", "main.py"],
                   port=tts_adapter_port, wait_port=True, order=2,
                   env_extra={"NACHOBOT_MULTIMODAL_HOST": tts_adapter_host,
                              "NACHOBOT_MULTIMODAL_PORT": str(tts_adapter_port)},
                   detail=f"TTS 中继 · :{tts_adapter_port}",
                   health_mode="tts"),
        ServiceDef("perception", "感知 API（VLM / ASR）", "tts_full",
                   "NachoBot-Multimodal-Adapter",
                   ["uv", "run", "python", "-m", "nachobot_multimodal.api_server"],
                   port=perception_port, order=3,
                   env_extra={"HOST": perception_host, "PORT": str(perception_port)},
                   detail=f"共享视觉与语音识别 · :{perception_port}"),

        # ── Multimodal LITE ──
        ServiceDef("tts_engine_lite", "TTS 推理运行时", "tts_lite", "", [],
                   port=tts_engine_port, wait_port=True, order=1,
                   detail=f"GPT-SoVITS / VoxCPM · :{tts_engine_port}"),
        ServiceDef("tts_adapter_lite", "多模态适配器（Lite）", "tts_lite",
                   "NachoBot-Multimodal-Adapter", ["uv", "run", "python", "main.py"],
                   port=tts_adapter_port, wait_port=True, order=2,
                   env_extra={"DISABLE_VLM_ASR": "1",
                              "NACHOBOT_MULTIMODAL_HOST": tts_adapter_host,
                              "NACHOBOT_MULTIMODAL_PORT": str(tts_adapter_port)},
                   detail=f"TTS 中继（不启动 VLM / ASR） · :{tts_adapter_port}",
                   health_mode="tts"),

        # ── Potato ──
        ServiceDef("potato_relay", "无模型中继（POTATO）", "potato", "NachoBot-Multimodal-Adapter",
                   ["uv", "run", "python", "main.py", "--no-local-models"],
                   port=tts_adapter_port, wait_port=True, order=1,
                   env_extra={"NACHOBOT_NO_LOCAL_MODELS": "1", "DISABLE_VLM_ASR": "1",
                              "NACHOBOT_MULTIMODAL_HOST": tts_adapter_host,
                              "NACHOBOT_MULTIMODAL_PORT": str(tts_adapter_port)},
                   detail=f"仅消息转发，不加载 TTS / VLM / ASR · :{tts_adapter_port}",
                   health_mode="relay_only"),

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
                   detail="Koishi 消息桥接 → Multimodal 中继"),
        ServiceDef("discordvc", "DiscordVC 语音适配器", "discord",
                   "NachoBot-DiscordVC-Adapter", ["uv", "run", "python", "main.py"], order=3,
                   detail="Discord 语音频道 → Core"),
    ]

    SERVICE_DEFS = {d.id: d for d in defs}

    groups = [
        GroupDef("core", "核心服务", "🧠", ["nachobot"],
                  "NachoBot Core 消息总线"),
        GroupDef("qq_adapter", "QQ / NapCat", "🐧", ["napcat_adapter", "napcat_shell"],
                  "QQ 消息适配器与 NapCat 客户端"),
        GroupDef("tts_full", "多模态服务（FULL）", "🎙️",
                   ["tts_engine_full", "tts_adapter_full", "perception"],
                   f"TTS 推理 + :{tts_adapter_port} 多模态中继 + VLM / ASR 感知服务"),
        GroupDef("tts_lite", "多模态服务（LITE）", "🎙️",
                   ["tts_engine_lite", "tts_adapter_lite"],
                   f"TTS 推理 + :{tts_adapter_port} 中继，不启动 VLM / ASR"),
        GroupDef("potato", "无模型中继（POTATO）", "🥔", ["potato_relay"],
                   f"保留 :{tts_adapter_port} 兼容通信，仅转发消息，不加载任何本地模型"),
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


class ProcessManager:
    """Manages subprocess lifecycle and log broadcasting."""

    def __init__(self, root_dir: Path | None = None):
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
        # FULL/LITE use gpu|cpu; POTATO is always forced to relay.
        self._launch_runtime: str = "gpu"
        # Lazy so importing/testing on non-Windows never loads Win32 DLLs.
        self._windows_job_facade: _WindowsJobFacade | Any | None = None

    def _get_windows_job_facade(self) -> _WindowsJobFacade | Any:
        if self._windows_job_facade is None:
            self._windows_job_facade = _WindowsJobFacade()
        return self._windows_job_facade

    # ---- public API ----

    def get_service_status(self, service_id: str) -> dict[str, Any]:
        """Get status info for a single service."""
        sdef = SERVICE_DEFS.get(service_id)
        if not sdef:
            raise ValueError(f"Unknown service: {service_id}")
        state = self.states.get(service_id, ServiceState())
        return {
            "id": service_id,
            "name": sdef.name,
            "group_id": sdef.group_id,
            "port": state.started_port if state.status == ServiceStatus.RUNNING and state.started_port is not None else sdef.port,
            "detail": sdef.detail,
            "status": state.status.value,
            "pid": state.pid,
            "started_at": state.started_at,
        }

    def get_all_statuses(self) -> list[dict[str, Any]]:
        _register_services()
        return [self.get_service_status(sid) for sid in SERVICE_DEFS]

    def get_group_statuses(self) -> list[dict[str, Any]]:
        _register_services()
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
        _register_services()
        core = self.get_service_status("nachobot")
        profiles: list[dict[str, Any]] = []
        active_profile: str | None = None
        error_profile: str | None = None

        for profile_id, group_id in LAUNCH_PROFILE_GROUPS.items():
            gdef = GROUP_DEFS[group_id]
            services = [self.get_service_status(sid) for sid in gdef.services]
            statuses = [service["status"] for service in services]
            if any(status == ServiceStatus.ERROR.value for status in statuses):
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
            else:
                status = ServiceStatus.STOPPED.value

            profiles.append({
                "id": profile_id,
                "group_id": group_id,
                "status": status,
                "services": services,
            })

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
            "profiles": profiles,
        }

    def get_log_history(self, service_id: str) -> list[str]:
        """Return buffered log lines for a service."""
        state = self.states.get(service_id)
        if state:
            return list(state.log_buffer)
        return []

    # ---- start / stop ----

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
        _register_services()
        sdef = SERVICE_DEFS.get(service_id)
        if sdef is None:
            raise ValueError(f"Unknown service: {service_id}")
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
        state.status = ServiceStatus.STARTING
        self._schedule_operation(
            f"service:{service_id}",
            lambda: self.start_service(service_id, _prepared=True),
            (service_id,),
            operation_kind="start",
        )

    def request_stop_service(self, service_id: str) -> None:
        """Cancel an in-flight start and schedule an orderly service stop."""
        _register_services()
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
        _register_services()
        gdef = GROUP_DEFS.get(group_id)
        if gdef is None:
            raise ValueError(f"Unknown group: {group_id}")
        existing = self._operation_tasks.get(f"group:{group_id}")
        if existing and not existing.done():
            raise RuntimeError(f"Group {group_id} is already changing state")
        self._validate_group_start(group_id)
        self._schedule_operation(
            f"group:{group_id}",
            lambda: self.start_group(group_id),
            tuple(gdef.services),
            operation_kind="start",
        )

    def request_stop_group(self, group_id: str) -> None:
        """Cancel an in-flight group start and schedule reverse-order shutdown."""
        _register_services()
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

    def _resolve_potato_runtime(self, preferred: str | None = None) -> str:
        """Prefer Relay for POTATO, falling back to an installed GPU/CPU runtime."""
        if MultimodalRuntimeManager.get_status("relay")["installed"]:
            return "relay"

        candidates: list[str] = []
        for candidate in (preferred, self._launch_runtime, "gpu", "cpu"):
            try:
                normalized = MultimodalRuntimeManager.normalize_profile(candidate)
            except ValueError:
                continue
            if normalized in {"gpu", "cpu"} and normalized not in candidates:
                candidates.append(normalized)

        for candidate in candidates:
            if MultimodalRuntimeManager.get_status(candidate)["installed"]:
                return candidate

        raise RuntimeError(
            "POTATO Relay 环境不可用，且没有已安装的 GPU/CPU Multimodal 环境"
        )

    def request_start_launch(self, profile_id: str, runtime: str | None = None) -> None:
        """Start Core and exactly one functionality profile with an explicit runtime."""
        _register_services()
        profile_id = str(profile_id or "").strip().lower()
        group_id = LAUNCH_PROFILE_GROUPS.get(profile_id)
        if group_id is None:
            raise ValueError(f"Unknown launch profile: {profile_id}")

        if profile_id == "potato":
            resolved_runtime = self._resolve_potato_runtime(runtime)
        else:
            resolved_runtime = MultimodalRuntimeManager.normalize_profile(runtime or "gpu")
            if resolved_runtime == "relay":
                raise ValueError("FULL/LITE 模式只能使用 GPU 或 CPU 环境")
        MultimodalRuntimeManager.require_python(resolved_runtime)
        self._launch_runtime = resolved_runtime

        existing = self._operation_tasks.get("launch")
        if existing and not existing.done():
            raise RuntimeError("NachoBot launch is already changing state")
        for group in ("core", *LAUNCH_PROFILE_GROUPS.values()):
            operation = self._operation_tasks.get(f"group:{group}")
            if operation and not operation.done():
                raise RuntimeError(f"Group {group} is already changing state")

        self._validate_group_start(group_id)
        affected = tuple(GROUP_DEFS["core"].services + GROUP_DEFS[group_id].services)
        self._schedule_operation(
            "launch",
            lambda: self.start_launch(profile_id, resolved_runtime),
            affected,
            operation_kind="start",
        )

    def request_stop_launch(self) -> None:
        """Cancel an in-flight launch and stop every runtime profile plus Core."""
        _register_services()
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

    async def start_launch(self, profile_id: str, runtime: str | None = None) -> None:
        """Run Core + selected profile transactionally, rolling back this launch on failure."""
        _register_services()
        group_id = LAUNCH_PROFILE_GROUPS.get(profile_id)
        if group_id is None:
            raise ValueError(f"Unknown launch profile: {profile_id}")

        resolved_runtime = (
            self._resolve_potato_runtime(runtime)
            if profile_id == "potato"
            else MultimodalRuntimeManager.normalize_profile(runtime or self._launch_runtime)
        )
        if profile_id != "potato" and resolved_runtime == "relay":
            raise ValueError("FULL/LITE 模式只能使用 GPU 或 CPU 环境")
        MultimodalRuntimeManager.require_python(resolved_runtime)
        self._launch_runtime = resolved_runtime

        core_state = self.states.get("nachobot")
        core_was_running = bool(core_state and core_state.status == ServiceStatus.RUNNING)
        try:
            await self.start_group("core")
            core_state = self.states.get("nachobot")
            if not core_state or core_state.status != ServiceStatus.RUNNING:
                return

            await self.start_group(group_id)
            profile_ready = all(
                self.states.get(service_id)
                and self.states[service_id].status == ServiceStatus.RUNNING
                for service_id in GROUP_DEFS[group_id].services
            )
            if profile_ready:
                return

            await self.stop_group(group_id)
            if not core_was_running:
                await self.stop_group("core")
        except asyncio.CancelledError:
            await self.stop_group(group_id)
            if not core_was_running:
                await self.stop_group("core")
            raise
        except Exception:
            try:
                await self.stop_group(group_id)
                if not core_was_running:
                    await self.stop_group("core")
            except Exception:
                logger.exception("Failed to roll back launch profile %s", profile_id)
            raise

    async def stop_launch(self) -> None:
        """Stop all mutually-exclusive runtime profiles, then stop Core."""
        _register_services()
        for group_id in LAUNCH_PROFILE_GROUPS.values():
            await self.stop_group(group_id)
        await self.stop_group("core")

    def _active_relay_port(self) -> int | None:
        """Return the port actually used when the current relay owner started."""
        for service_id in ("tts_adapter_full", "tts_adapter_lite", "potato_relay"):
            state = self.states.get(service_id)
            if state and state.status == ServiceStatus.RUNNING:
                if state.started_port is not None:
                    return state.started_port
                return SERVICE_DEFS[service_id].port
        return None

    def _configured_consumer_relay_port(self, service_id: str) -> int:
        """Read the platform adapter's configured upstream relay port."""
        relay_port = SERVICE_DEFS["potato_relay"].port
        config_paths = {
            "napcat_adapter": self.root / "NachoBot-Napcat-Adapter" / "config.toml",
            "koishi_adapter": self.root / "NachoBot-Koishi-Adapter" / "config.toml",
        }
        config_path = config_paths.get(service_id)
        if config_path is None:
            return relay_port
        try:
            import tomlkit

            document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
            return int(document.get("nachobot_server", {}).get("port", relay_port))
        except Exception as exc:
            raise RuntimeError(
                f"Cannot read relay endpoint from {config_path}: {exc}"
            ) from exc

    def _require_relay_owner(self, consumer_service_id: str) -> None:
        consumer_name = SERVICE_DEFS[consumer_service_id].name
        configured_relay_port = SERVICE_DEFS["potato_relay"].port
        relay_port = self._active_relay_port()
        if relay_port is None:
            raise RuntimeError(
                f"Cannot start {consumer_name}: relay :{configured_relay_port} is not ready. "
                "Start one of FULL, LITE, or POTATO first."
            )

        consumer_port = self._configured_consumer_relay_port(consumer_service_id)
        if consumer_port != relay_port:
            raise RuntimeError(
                f"Cannot start {consumer_name}: configured upstream port :{consumer_port} "
                f"does not match the active Multimodal relay port :{relay_port}."
            )

    def _validate_service_start(self, service_id: str) -> None:
        # QQ/Koishi text adapters target the configured Multimodal relay endpoint.
        if service_id in ("napcat_adapter", "koishi_adapter"):
            self._require_relay_owner(service_id)

        # Direct service starts must preserve the same mutual exclusion that
        # group starts enforce for the configured relay and shared TTS engine resources.
        relay_services = ("tts_adapter_full", "tts_adapter_lite", "potato_relay")
        engine_services = ("tts_engine_full", "tts_engine_lite")
        conflict_set = relay_services if service_id in relay_services else engine_services if service_id in engine_services else ()
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

    def _validate_group_start(self, group_id: str) -> None:
        gdef = GROUP_DEFS[group_id]
        for service_id in gdef.services:
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
        _register_services()
        sdef = SERVICE_DEFS.get(service_id)
        if not sdef:
            raise ValueError(f"Unknown service: {service_id}")

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
            process_live = state.process is not None and getattr(state.process, "returncode", None) is None
            group_owned = state.process_group_id is not None
            windows_owned = bool(state.windows_owned_processes)
            windows_job_owned = state.windows_job is not None
            if state.status == ServiceStatus.RUNNING:
                return
            if state.status == ServiceStatus.STARTING and not _prepared:
                return
            if process_live or group_owned or windows_owned or windows_job_owned:
                state.status = ServiceStatus.ERROR
                await self._broadcast(
                    service_id,
                    "[WebUI] ERROR: previous process/group still requires stop before starting again\n",
                )
                return
        if state is None:
            state = ServiceState()
            self.states[service_id] = state
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

        full_cwd = self.root / cwd if cwd else self.root

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
                stdin=asyncio.subprocess.PIPE,
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
                readiness_timeout = None if service_id in ("tts_engine_full", "tts_engine_lite") else 180
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
        _register_services()
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
            return

        sdef = SERVICE_DEFS[service_id]
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

    async def start_group(self, group_id: str) -> None:
        _register_services()
        gdef = GROUP_DEFS.get(group_id)
        if not gdef:
            raise ValueError(f"Unknown group: {group_id}")

        # FULL, LITE, and POTATO all own the configured relay endpoint;
        # only one of them can be active at a time.
        self._validate_group_start(group_id)

        started_here: list[str] = []
        try:
            if group_id == "vrchat":
                self._active_group_env[group_id] = {
                    VRCHAT_CAPABILITY_ENV: secrets.token_hex(32),
                }

            for sid in gdef.services:
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

    async def stop_group(self, group_id: str) -> None:
        _register_services()
        gdef = GROUP_DEFS.get(group_id)
        if not gdef:
            raise ValueError(f"Unknown group: {group_id}")

        # Stop in reverse order
        for sid in reversed(gdef.services):
            await self.stop_service(sid)
        self._active_group_env.pop(group_id, None)

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
        """Replace Multimodal `uv run` commands with the selected venv Python."""
        adapter_services = {
            "tts_adapter_full": ["main.py"],
            "tts_adapter_lite": ["main.py"],
            "perception": ["-m", "nachobot_multimodal.api_server"],
            "potato_relay": ["main.py", "--no-local-models"],
        }
        args = adapter_services.get(service_id)
        if args is None:
            return cmd, env_extra

        runtime = self._launch_runtime
        if runtime == "relay" and service_id != "potato_relay":
            raise ValueError("FULL/LITE Multimodal 服务不能使用 Relay 环境")
        python = MultimodalRuntimeManager.require_python(runtime)
        return [str(python), *args], dict(env_extra)

    def _resolve_cmd(self, sdef: ServiceDef) -> tuple[list[str], str, dict[str, str]]:
        """Resolve dynamic commands (e.g., TTS engine based on config)."""
        if sdef.id in ("tts_engine_full", "tts_engine_lite"):
            return self._resolve_tts_engine_cmd()
        if sdef.id == "bilibili":
            nachobot_dir = self.root / "NachoBot"
            bili_dir = self.root / "NachoBot-Bilibili-Adapter"
            cmd = ["uv", "run", "--project", str(nachobot_dir), "python", "main.py"]
            env_extra = {"PYTHONPATH": f"{nachobot_dir};{bili_dir}"}
            return cmd, sdef.cwd, env_extra
        return sdef.cmd, sdef.cwd, {}

    def _resolve_tts_engine_cmd(self) -> tuple[list[str], str, dict[str, str]]:
        """Determine which TTS engine to start based on base.toml."""
        base_toml = self.root / "NachoBot-Multimodal-Adapter" / "configs" / "base.toml"
        engine = "GPT_Sovits"  # default
        tts_engine_port = 9880

        if base_toml.exists():
            try:
                import tomlkit

                doc = tomlkit.parse(base_toml.read_text(encoding="utf-8"))
                enabled = doc.get("enabled_tts", {}).get("enabled", ["GPT_Sovits"])
                if isinstance(enabled, list) and "Vox" in enabled:
                    engine = "Vox"

                tts_engine_port = _read_tts_engine_port(self.root, engine)
            except Exception:
                pass

        adapter_dir = self.root / "NachoBot-Multimodal-Adapter"
        manager = adapter_dir / "scripts" / "tts_runtime_manager.py"
        if not manager.is_file():
            raise FileNotFoundError(f"TTS runtime manager 不存在: {manager}")

        managed_engine = "voxcpm" if engine == "Vox" else "gpt-sovits"
        runtime = MultimodalRuntimeManager.normalize_profile(self._launch_runtime)
        if runtime == "relay":
            raise ValueError("Relay/POTATO 模式不启动本地 TTS 推理运行时")
        python = MultimodalRuntimeManager.require_python(runtime)
        cmd = [
            str(python), str(manager),
            "serve",
            "--engine", managed_engine,
            "--port", str(tts_engine_port),
        ]
        torch_index = (
            "https://download.pytorch.org/whl/cpu"
            if runtime == "cpu"
            else "https://download.pytorch.org/whl/cu128"
        )
        return cmd, str(adapter_dir), {"NACHOBOT_TTS_TORCH_INDEX": torch_index}
