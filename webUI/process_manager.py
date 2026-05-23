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


@dataclass
class GroupDef:
    """Static definition of a launch group."""
    id: str
    name: str
    icon: str
    services: list[str]         # Service IDs in launch order


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
    from urllib.parse import urlparse
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

    # 3. Parse TTS Adapter base.toml & enabled engine port
    tts_adapter_host = "127.0.0.1"
    tts_adapter_port = 8070
    tts_engine_port = 9880
    tts_base_path = ROOT_DIR / "NachoBot-TTS-Adapter" / "configs" / "base.toml"
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
            
            # Read engine api_base port
            plugins_sec = doc.get("plugins", {})
            engine_sec = plugins_sec.get(engine, {})
            api_base = engine_sec.get("api_base", "http://127.0.0.1:9880")
            parsed_url = urlparse(api_base)
            if parsed_url.port:
                tts_engine_port = parsed_url.port
        except Exception:
            pass

    # 4. Parse Perception configs/perception.toml
    perception_host = "127.0.0.1"
    perception_port = 9874
    perception_config_path = ROOT_DIR / "NachoBot-TTS-Adapter" / "configs" / "perception.toml"
    if perception_config_path.exists():
        try:
            doc = tomlkit.parse(perception_config_path.read_text(encoding="utf-8"))
            percep_sec = doc.get("perception", {})
            perception_host = percep_sec.get("host", perception_host)
            perception_port = int(percep_sec.get("port", perception_port))
        except Exception:
            pass

    # 5. Parse Koishi configs/koishi.yml
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
        ServiceDef("nachobot",       "NachoBot Core",      "core", "NachoBot",                 ["uv", "run", "python", "bot.py"],      port=nachobot_port, wait_port=True, order=1,
                   env_extra={"HOST": nachobot_env_host, "PORT": str(nachobot_port)}),
                   
        # ── QQ ──
        ServiceDef("napcat_adapter", "Napcat Adapter",     "qq_adapter", "NachoBot-Napcat-Adapter",   ["uv", "run", "python", "main.py"],     port=napcat_port, wait_port=False, order=1,
                   env_extra={"HOST": napcat_env_host, "PORT": str(napcat_port)}),
        ServiceDef("napcat_shell",   "NapCat Shell",       "qq_adapter", "NapCat.Shell",              ["cmd", "/c", "launcher-user.bat"],      order=2),

        # ── TTS FULL ──
        ServiceDef("tts_engine_full",   "TTS Engine",          "tts_full", "",  [],  port=tts_engine_port, wait_port=True, order=1),  # dynamic
        ServiceDef("tts_adapter_full",  "TTS Adapter",         "tts_full", "NachoBot-TTS-Adapter", ["uv", "run", "python", "main.py"], port=tts_adapter_port, order=2,
                   env_extra={"HOST": tts_adapter_host, "PORT": str(tts_adapter_port)}),
        ServiceDef("perception",        "Perception API",      "tts_full", "NachoBot-TTS-Adapter",
                   ["uv", "run", "python", "-m", "tts_src.plugins.Perception.api_server"], port=perception_port, order=3,
                   env_extra={"HOST": perception_host, "PORT": str(perception_port)}),

        # ── TTS LITE ──
        ServiceDef("tts_engine_lite",   "TTS Engine",          "tts_lite", "",  [],  port=tts_engine_port, wait_port=True, order=1),
        ServiceDef("tts_adapter_lite",  "TTS Adapter (Lite)",  "tts_lite", "NachoBot-TTS-Adapter", ["uv", "run", "python", "main.py"],
                   port=tts_adapter_port, order=2, env_extra={"DISABLE_VLM_ASR": "1", "HOST": tts_adapter_host, "PORT": str(tts_adapter_port)}),

        # ── UniversalVC ──
        ServiceDef("universalvc",   "UniversalVC Adapter", "universalvc", "NachoBot-UniversalVC-Adapter", ["uv", "run", "python", "main.py"], order=1),

        # ── Bilibili ──
        ServiceDef("bilibili",      "Bilibili Adapter",    "bilibili",    "NachoBot-Bilibili-Adapter",    ["uv", "run", "python", "main.py"], order=1),

        # ── Discord ──
        ServiceDef("koishi",            "Koishi",              "discord", "koishi-app",              ["cmd", "/c", "npm", "start"],  port=koishi_port, wait_port=True, order=1,
                   env_extra={"HTTPS_PROXY": webui_config.https_proxy, "HTTP_PROXY": webui_config.http_proxy}),
        ServiceDef("koishi_adapter",    "Koishi Adapter",      "discord", "NachoBot-Koishi-Adapter", ["uv", "run", "python", "main.py"], order=2),
        ServiceDef("discordvc",         "DiscordVC Adapter",   "discord", "NachoBot-DiscordVC-Adapter", ["uv", "run", "python", "main.py"], order=3),
    ]

    SERVICE_DEFS = {d.id: d for d in defs}

    groups = [
        GroupDef("core",        "核心",                "🧠", ["nachobot"]),
        GroupDef("qq_adapter",  "QQ 适配器",           "🐧", ["napcat_adapter", "napcat_shell"]),
        GroupDef("tts_full",    "TTS 语音 (FULL)",     "🎙️", ["tts_engine_full", "tts_adapter_full", "perception"]),
        GroupDef("tts_lite",    "TTS 语音 (LITE)",     "🎙️", ["tts_engine_lite", "tts_adapter_lite"]),
        GroupDef("universalvc", "全局语音适配器",       "🎤", ["universalvc"]),
        GroupDef("bilibili",    "Bilibili 适配器",     "📺", ["bilibili"]),
        GroupDef("discord",     "Discord 适配器",      "💬", ["koishi", "koishi_adapter", "discordvc"]),
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
                asyncio.create_task(self._wait_for_port(service_id, sdef.port))

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

        # Check for TTS engine conflict
        if group_id in ("tts_full", "tts_lite"):
            other = "tts_lite" if group_id == "tts_full" else "tts_full"
            other_gdef = GROUP_DEFS[other]
            for sid in other_gdef.services:
                st = self.states.get(sid)
                if st and st.status in (ServiceStatus.RUNNING, ServiceStatus.STARTING):
                    raise RuntimeError(f"Cannot start {gdef.name}: {GROUP_DEFS[other].name} is already running. Stop it first.")

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
                ready = await self._wait_for_port(sid, sdef.port)
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

    async def _wait_for_port(self, service_id: str, port: int, timeout: int = 180) -> bool:
        """Wait for a port to become available. Returns True if ready, False if timed out."""
        import socket
        await self._broadcast(service_id, f"[WebUI] 等待端口 {port} 就绪 (最长 {timeout}s)...\n")
        for i in range(timeout):
            # Check if the process died while we're waiting
            state = self.states.get(service_id)
            if state and state.status in (ServiceStatus.STOPPED, ServiceStatus.ERROR):
                await self._broadcast(service_id, "[WebUI] 进程已退出，停止等待端口\n")
                return False
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    await self._broadcast(service_id, f"[WebUI] Port {port} is ready. ({i+1}s)\n")
                    return True
            except (ConnectionRefusedError, OSError, socket.timeout):
                await asyncio.sleep(1)
        await self._broadcast(service_id, f"[WebUI] WARNING: Port {port} not ready after {timeout}s.\n")
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
        base_toml = self.root / "NachoBot-TTS-Adapter" / "configs" / "base.toml"
        engine = "GPT_Sovits"  # default
        tts_engine_port = 9880

        if base_toml.exists():
            try:
                import tomlkit
                from urllib.parse import urlparse
                doc = tomlkit.parse(base_toml.read_text(encoding="utf-8"))
                enabled = doc.get("enabled_tts", {}).get("enabled", ["GPT_Sovits"])
                if isinstance(enabled, list) and "Vox" in enabled:
                    engine = "Vox"
                
                # Get dynamic port for the resolved engine
                plugins_sec = doc.get("plugins", {})
                engine_sec = plugins_sec.get(engine, {})
                api_base = engine_sec.get("api_base", "http://127.0.0.1:9880")
                parsed_url = urlparse(api_base)
                if parsed_url.port:
                    tts_engine_port = parsed_url.port
            except Exception:
                pass

        from webui_config import webui_config

        if engine == "Vox":
            # VoxCPM
            voxcpm_dir = webui_config.voxcpm_dir
            if not voxcpm_dir.exists():
                raise FileNotFoundError(
                    f"VoxCPM 目录不存在: {voxcpm_dir}\n"
                    f"请在 WebUI 配置中修改 [paths].voxcpm_dir"
                )
            adapter_dir = self.root / "NachoBot-TTS-Adapter"
            ffmpeg_bin = self.root / "NachoBot" / "plugins" / "bilibili_video_sender_plugin" / "ffmpeg" / "bin"
            py_vox = voxcpm_dir / ".venv" / "Scripts" / "python.exe"
            if not py_vox.exists():
                raise FileNotFoundError(
                    f"VoxCPM Python 解释器不存在: {py_vox}\n"
                    f"请确认 VoxCPM 虚拟环境已正确安装"
                )
            vox_script = adapter_dir / "tts_src" / "plugins" / "Vox" / "vox_api_server.py"
            # 读取 vox.toml 配置文件中的 LoRA 路径与模型路径
            model_dir = voxcpm_dir / "models" / "openbmb__VoxCPM2"
            lora = voxcpm_dir / "lora" / "ncnk"
            vox_toml = adapter_dir / "configs" / "vox.toml"
            if vox_toml.exists():
                try:
                    import tomlkit
                    vox_doc = tomlkit.parse(vox_toml.read_text(encoding="utf-8"))
                    tts_sec = vox_doc.get("tts", {})
                    if tts_sec.get("model_dir"):
                        model_dir = Path(tts_sec["model_dir"])
                    if "lora_weights_path" in tts_sec:
                        lora = tts_sec["lora_weights_path"]
                except Exception:
                    pass

            cmd = [
                str(py_vox), str(vox_script),
                "--host", "127.0.0.1", "--port", str(tts_engine_port),
                "--model-dir", str(model_dir),
                "--lora-weights", str(lora),
            ]
            env_extra = {"PATH": f"{ffmpeg_bin};{os.environ.get('PATH', '')}"}
            return cmd, str(voxcpm_dir), env_extra
        else:
            # GPT-SoVITS
            sovits_dir = webui_config.sovits_dir
            if not sovits_dir.exists():
                raise FileNotFoundError(
                    f"GPT-SoVITS 目录不存在: {sovits_dir}\n"
                    f"请在 WebUI 配置中修改 [paths].sovits_dir"
                )
            py_gpt = sovits_dir / "runtime" / "python.exe"
            if not py_gpt.exists():
                raise FileNotFoundError(
                    f"GPT-SoVITS Python 解释器不存在: {py_gpt}\n"
                    f"请确认 GPT-SoVITS 整合包已正确安装"
                )
            api_file = sovits_dir / "api_v2.py"
            if not api_file.exists():
                api_file = sovits_dir / "api.py"

            cmd = [str(py_gpt), "-s", str(api_file), "--port", str(tts_engine_port)]
            env_extra = {
                "PYTHONPATH": f"{sovits_dir};{sovits_dir / 'GPT_SoVITS'}",
                "CUDA_VISIBLE_DEVICES": "0",
            }
            return cmd, str(sovits_dir), env_extra
