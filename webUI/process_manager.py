"""
NachoBot WebUI — Process Manager
Manages service subprocess lifecycles, log capture, and WebSocket broadcasting.
"""

import asyncio
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import psutil


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
    """Build the service and group lookup tables."""
    global SERVICE_DEFS, GROUP_DEFS

    defs = [
        # ── Core ──
        ServiceDef("nachobot",       "NachoBot Core",      "core", "NachoBot",                 ["uv", "run", "python", "bot.py"],      port=8000, wait_port=True, order=1,
                   env_extra={"HOST": "127.0.0.1", "PORT": "8000"}),
                   
        # ── QQ ──
        ServiceDef("napcat_adapter", "Napcat Adapter",     "qq_adapter", "NachoBot-Napcat-Adapter",   ["uv", "run", "python", "main.py"],     port=8095, wait_port=False, order=1,
                   env_extra={"HOST": "0.0.0.0", "PORT": "8095"}),
        ServiceDef("napcat_shell",   "NapCat Shell",       "qq_adapter", "NapCat.Shell",              ["cmd", "/c", "launcher-user.bat"],      order=2),

        # ── TTS FULL ──
        ServiceDef("tts_engine_full",   "TTS Engine",          "tts_full", "",  [],  port=9880, wait_port=True, order=1),  # dynamic
        ServiceDef("tts_adapter_full",  "TTS Adapter",         "tts_full", "NachoBot-TTS-Adapter", ["uv", "run", "python", "main.py"], port=8070, order=2),
        ServiceDef("perception",        "Perception API",      "tts_full", "NachoBot-TTS-Adapter",
                   ["uv", "run", "python", "-m", "tts_src.plugins.Perception.api_server"], port=9874, order=3),

        # ── TTS LITE ──
        ServiceDef("tts_engine_lite",   "TTS Engine",          "tts_lite", "",  [],  port=9880, wait_port=True, order=1),
        ServiceDef("tts_adapter_lite",  "TTS Adapter (Lite)",  "tts_lite", "NachoBot-TTS-Adapter", ["uv", "run", "python", "main.py"],
                   port=8070, order=2, env_extra={"DISABLE_VLM_ASR": "1"}),

        # ── UniversalVC ──
        ServiceDef("universalvc",   "UniversalVC Adapter", "universalvc", "NachoBot-UniversalVC-Adapter", ["uv", "run", "python", "main.py"], order=1),

        # ── Bilibili ──
        ServiceDef("bilibili",      "Bilibili Adapter",    "bilibili",    "NachoBot-Bilibili-Adapter",    ["uv", "run", "python", "main.py"], order=1),

        # ── Discord ──
        ServiceDef("koishi",            "Koishi",              "discord", "koishi-app",              ["cmd", "/c", "npm", "start"],  port=5140, wait_port=True, order=1,
                   env_extra={"HTTPS_PROXY": "http://127.0.0.1:7897", "HTTP_PROXY": "http://127.0.0.1:7897"}),
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
        return [self.get_service_status(sid) for sid in SERVICE_DEFS]

    def get_group_statuses(self) -> list[dict[str, Any]]:
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
        cmd, cwd, env_extra = self._resolve_cmd(sdef)
        if not cmd:
            state.status = ServiceStatus.ERROR
            await self._broadcast(service_id, f"[WebUI] ERROR: Cannot resolve command for {sdef.name}\n")
            return

        full_cwd = self.root / cwd if cwd else self.root

        # Build environment
        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
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

            # Optionally wait for port
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
            # Small delay between services in the same group
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
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
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

    async def _wait_for_port(self, service_id: str, port: int, timeout: int = 120):
        """Wait for a port to become available."""
        import socket
        for _ in range(timeout):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    await self._broadcast(service_id, f"[WebUI] Port {port} is ready.\n")
                    return
            except (ConnectionRefusedError, OSError, socket.timeout):
                await asyncio.sleep(1)
        await self._broadcast(service_id, f"[WebUI] WARNING: Port {port} not ready after {timeout}s.\n")

    def _resolve_cmd(self, sdef: ServiceDef) -> tuple[list[str], str, dict[str, str]]:
        """Resolve dynamic commands (e.g., TTS engine based on config)."""
        if sdef.id in ("tts_engine_full", "tts_engine_lite"):
            return self._resolve_tts_engine_cmd()
        return sdef.cmd, sdef.cwd, {}

    def _resolve_tts_engine_cmd(self) -> tuple[list[str], str, dict[str, str]]:
        """Determine which TTS engine to start based on base.toml."""
        base_toml = self.root / "NachoBot-TTS-Adapter" / "configs" / "base.toml"
        engine = "GPT_Sovits"  # default

        if base_toml.exists():
            import tomlkit
            doc = tomlkit.parse(base_toml.read_text(encoding="utf-8"))
            enabled = doc.get("enabled_tts", {}).get("enabled", ["GPT_Sovits"])
            if isinstance(enabled, list) and "Vox" in enabled:
                engine = "Vox"

        if engine == "Vox":
            # VoxCPM
            vox_toml = self.root / "NachoBot-TTS-Adapter" / "configs" / "vox.toml"
            voxcpm_dir = Path("C:/Users/BigSh0t/VoxCPM-2.0.2")  # from launchbot.bat
            adapter_dir = self.root / "NachoBot-TTS-Adapter"
            ffmpeg_bin = self.root / "NachoBot" / "plugins" / "bilibili_video_sender_plugin" / "ffmpeg" / "bin"
            py_vox = voxcpm_dir / ".venv" / "Scripts" / "python.exe"
            vox_script = adapter_dir / "tts_src" / "plugins" / "Vox" / "vox_api_server.py"
            model_dir = voxcpm_dir / "models" / "openbmb__VoxCPM2"
            lora = voxcpm_dir / "lora" / "ncnk"

            cmd = [
                str(py_vox), str(vox_script),
                "--host", "127.0.0.1", "--port", "9880",
                "--model-dir", str(model_dir),
                "--lora-weights", str(lora),
            ]
            env_extra = {"PATH": f"{ffmpeg_bin};{os.environ.get('PATH', '')}"}
            return cmd, str(voxcpm_dir), env_extra
        else:
            # GPT-SoVITS
            sovits_dir = Path("C:/Users/BigSh0t/GPT-SoVITS/GPT-SoVITS-v2pro-20250604")
            py_gpt = sovits_dir / "runtime" / "python.exe"
            api_file = sovits_dir / "api_v2.py"
            if not api_file.exists():
                api_file = sovits_dir / "api.py"

            cmd = [str(py_gpt), "-s", str(api_file), "--port", "9880"]
            env_extra = {
                "PYTHONPATH": f"{sovits_dir};{sovits_dir / 'GPT_SoVITS'}",
                "CUDA_VISIBLE_DEVICES": "0",
            }
            return cmd, str(sovits_dir), env_extra
