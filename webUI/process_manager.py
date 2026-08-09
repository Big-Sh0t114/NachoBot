"""
NachoBot WebUI — Process Manager
Manages service subprocess lifecycles, log capture, and WebSocket broadcasting.
"""

import asyncio
import locale
import os
import re
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import psutil

# Regex to strip ANSI escape sequences (colors, cursor moves, etc.)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


ROOT_DIR = Path(__file__).resolve().parent.parent

_TTS_ENGINE_CONFIG_FILES = {
    "GPT_Sovits": "gpt-sovits.toml",
    "Vox": "vox.toml",
}


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
                   env_extra={"HOST": tts_adapter_host, "PORT": str(tts_adapter_port)},
                   detail=f"TTS 中继 · :{tts_adapter_port}"),
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
                   env_extra={"DISABLE_VLM_ASR": "1", "HOST": tts_adapter_host,
                              "PORT": str(tts_adapter_port)},
                   detail=f"TTS 中继（不启动 VLM / ASR） · :{tts_adapter_port}"),

        # ── Potato ──
        ServiceDef("potato_relay", "8070 无模型中继", "potato", "NachoBot-Multimodal-Adapter",
                   ["uv", "run", "python", "main.py", "--no-local-models"],
                   port=tts_adapter_port, wait_port=True, order=1,
                   env_extra={"NACHOBOT_NO_LOCAL_MODELS": "1", "DISABLE_VLM_ASR": "1",
                              "HOST": tts_adapter_host, "PORT": str(tts_adapter_port)},
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

        # ── Bilibili ──
        ServiceDef("bilibili", "Bilibili 直播适配器", "bilibili", "NachoBot-Bilibili-Adapter",
                   ["uv", "run", "python", "main.py"], order=1,
                   detail="直播弹幕、评论与私信 → Core"),

        # ── Discord ──
        ServiceDef("koishi", "Koishi 框架", "discord", "koishi-app",
                   ["cmd", "/c", "npm", "start"], port=koishi_port, wait_port=True, order=1,
                   env_extra={"HTTPS_PROXY": webui_config.https_proxy,
                              "HTTP_PROXY": webui_config.http_proxy},
                   detail=f"Discord / OneBot 平台网关 · :{koishi_port}"),
        ServiceDef("koishi_adapter", "Koishi 适配器", "discord", "NachoBot-Koishi-Adapter",
                   ["uv", "run", "python", "main.py"], order=2,
                   detail="Koishi 消息桥接 → Core"),
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
                  "TTS 推理 + 8070 多模态中继 + VLM / ASR 感知服务"),
        GroupDef("tts_lite", "多模态服务（LITE）", "🎙️",
                  ["tts_engine_lite", "tts_adapter_lite"],
                  "TTS 推理 + 8070 中继，不启动 VLM / ASR"),
        GroupDef("potato", "8070 无模型中继（POTATO）", "🥔", ["potato_relay"],
                  "保留 8070 兼容通信，仅转发消息，不加载任何本地模型"),
        GroupDef("bilibili", "Bilibili 直播", "📺", ["bilibili"],
                  "直播弹幕、评论、私信与可选 Live2D 联动"),
        GroupDef("live2d", "Live2D 渲染", "🖼️", ["live2d"],
                  "独立 Live2D WebSocket 渲染服务"),
        GroupDef("discord", "Discord / Koishi", "💬",
                  ["koishi", "koishi_adapter", "discordvc"],
                  "Koishi 平台网关、文字适配器与 Discord 语音适配器"),
        GroupDef("universalvc", "UniversalVC 语音", "🎤", ["universalvc"],
                  "进程音频捕获、实时 ASR 与虚拟声卡输出"),
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
    started_at: float | None = None
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=10000))
    _read_task: asyncio.Task | None = None


class ProcessManager:
    """Manages subprocess lifecycle and log broadcasting."""

    def __init__(self, root_dir: Path | None = None):
        self.root = root_dir or ROOT_DIR
        self.states: dict[str, ServiceState] = {}
        self._ws_subscribers: dict[str, list[Callable]] = {}  # service_id -> [callback]
        self._all_subscribers: list[Callable] = []  # "all" channel

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
            "port": sdef.port,
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

    def get_log_history(self, service_id: str) -> list[str]:
        """Return buffered log lines for a service."""
        state = self.states.get(service_id)
        if state:
            return list(state.log_buffer)
        return []

    # ---- start / stop ----

    async def start_service(self, service_id: str) -> None:
        _register_services()
        sdef = SERVICE_DEFS.get(service_id)
        if not sdef:
            raise ValueError(f"Unknown service: {service_id}")

        state = self.states.get(service_id)
        if state and state.status in (ServiceStatus.RUNNING, ServiceStatus.STARTING):
            return  # Already running

        state = ServiceState(status=ServiceStatus.STARTING)
        self.states[service_id] = state

        await self._broadcast(service_id, f"[WebUI] Starting {sdef.name}...\n")

        # Resolve TTS engine dynamically
        try:
            cmd, cwd, env_extra = self._resolve_cmd(sdef)
        except FileNotFoundError as e:
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

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(full_cwd),
                env=env,
                creationflags=getattr(signal, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
            )
            state.process = proc
            state.pid = proc.pid
            state.started_at = time.time()
            state.status = ServiceStatus.RUNNING

            # Start reading output
            state._read_task = asyncio.create_task(self._read_output(service_id, proc))

            await self._broadcast(service_id, f"[WebUI] {sdef.name} started (PID: {proc.pid})\n")

            # Optionally wait for port (background when started individually)
            if sdef.wait_port and sdef.port:
                asyncio.create_task(self._wait_for_port(service_id, sdef.port, timeout=180))

        except Exception as e:
            state.status = ServiceStatus.ERROR
            await self._broadcast(service_id, f"[WebUI] ERROR starting {sdef.name}: {e}\n")

    async def stop_service(self, service_id: str) -> None:
        state = self.states.get(service_id)
        if not state or state.status == ServiceStatus.STOPPED:
            return

        sdef = SERVICE_DEFS[service_id]
        state.status = ServiceStatus.STOPPING
        await self._broadcast(service_id, f"[WebUI] Stopping {sdef.name}...\n")

        if state.process and state.process.returncode is None:
            try:
                # On Windows, terminate the process tree
                if os.name == "nt" and state.pid:
                    try:
                        parent = psutil.Process(state.pid)
                        for child in parent.children(recursive=True):
                            try:
                                child.terminate()
                            except psutil.NoSuchProcess:
                                pass
                        parent.terminate()
                        # Wait a bit, then kill if needed
                        gone, alive = psutil.wait_procs([parent] + parent.children(recursive=True), timeout=5)
                        for p in alive:
                            p.kill()
                    except psutil.NoSuchProcess:
                        pass
                else:
                    state.process.terminate()
                    try:
                        await asyncio.wait_for(state.process.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        state.process.kill()
            except ProcessLookupError:
                pass

        if state._read_task and not state._read_task.done():
            state._read_task.cancel()

        state.status = ServiceStatus.STOPPED
        state.process = None
        state.pid = None
        await self._broadcast(service_id, f"[WebUI] {sdef.name} stopped.\n")

    async def start_group(self, group_id: str) -> None:
        gdef = GROUP_DEFS.get(group_id)
        if not gdef:
            raise ValueError(f"Unknown group: {group_id}")

        # The FULL, LITE, and POTATO groups all own the 8070 adapter endpoint;
        # only one of them can be active at a time.
        multimodal_groups = ("tts_full", "tts_lite", "potato")
        if group_id in multimodal_groups:
            for other in multimodal_groups:
                if other == group_id:
                    continue
                other_gdef = GROUP_DEFS[other]
                for sid in other_gdef.services:
                    st = self.states.get(sid)
                    if st and st.status in (ServiceStatus.RUNNING, ServiceStatus.STARTING):
                        raise RuntimeError(
                            f"Cannot start {gdef.name}: {GROUP_DEFS[other].name} is already running. Stop it first."
                        )

        for sid in gdef.services:
            await self.start_service(sid)
            # Check if the service actually started; abort group if it failed
            state = self.states.get(sid)
            if state and state.status == ServiceStatus.ERROR:
                await self._broadcast(sid, f"[WebUI] {SERVICE_DEFS[sid].name} 启动失败，中止启动组内后续服务\n")
                return
            # If this service has a port dependency, wait until it's ready
            # before starting downstream services
            sdef = SERVICE_DEFS[sid]
            if sdef.wait_port and sdef.port:
                ready = await self._wait_for_port(sid, sdef.port, timeout=180)
                if not ready:
                    state.status = ServiceStatus.ERROR
                    await self._broadcast(sid, f"[WebUI] {sdef.name} 端口 {sdef.port} 超时未就绪，中止启动组内后续服务\n")
                    return
            else:
                # Small delay between services without port checks
                await asyncio.sleep(2)

    async def stop_group(self, group_id: str) -> None:
        gdef = GROUP_DEFS.get(group_id)
        if not gdef:
            raise ValueError(f"Unknown group: {group_id}")

        # Stop in reverse order
        for sid in reversed(gdef.services):
            await self.stop_service(sid)

    async def send_input(self, service_id: str, text: str) -> None:
        state = self.states.get(service_id)
        if not state or state.status != ServiceStatus.RUNNING or not state.process:
            raise ValueError(f"Service {service_id} is not running")
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
        if state and state.status not in (ServiceStatus.STOPPED, ServiceStatus.STOPPING):
            rc = proc.returncode
            state.status = ServiceStatus.ERROR if rc and rc != 0 else ServiceStatus.STOPPED
            await self._broadcast(service_id, f"[WebUI] Process exited (code: {rc})\n")

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

    async def _wait_for_port(self, service_id: str, port: int, timeout: int = 180) -> bool:
        """Wait for a port to become available. Returns True if ready, False if timed out."""
        import socket
        sdef = SERVICE_DEFS.get(service_id)
        await self._broadcast(service_id, f"[WebUI] 等待端口 {port} 就绪 (最长 {timeout}s)...\n")
        for i in range(timeout):
            # Check if the process died while we're waiting
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
                        f"[WebUI] Port {port} is ready ({sdef.health_mode}). ({i+1}s)\n",
                    )
                    return True
            else:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        await self._broadcast(service_id, f"[WebUI] Port {port} is ready. ({i+1}s)\n")
                        return True
                except (ConnectionRefusedError, OSError, socket.timeout):
                    pass
            await asyncio.sleep(1)
        message = f"[WebUI] WARNING: Port {port} not ready after {timeout}s."
        if service_id in ("tts_engine_full", "tts_engine_lite"):
            message += " 如果你是初次启动，等待模型下载完毕后重启该服务。"
        await self._broadcast(service_id, message + "\n")
        return False

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
        cmd = [
            "uv", "run", "python", str(manager),
            "serve",
            "--engine", managed_engine,
            "--port", str(tts_engine_port),
        ]
        return cmd, str(adapter_dir), {}
