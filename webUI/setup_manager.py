"""
NachoBot WebUI — Setup Manager
Deployment wizard backend: environment checks, config generation, dependency installation.
"""

import asyncio
import os
import shutil
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tomlkit

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT_DIR / "config-save" / "setup_backups"

# Maximum number of backup copies to keep per file
MAX_BACKUPS_PER_FILE = 5


# ---------------------------------------------------------------------------
# Template → target mapping
# ---------------------------------------------------------------------------

TEMPLATE_MAP: dict[str, str] = {
    "NachoBot/template/bot_config_template.toml": "NachoBot/config/bot_config.toml",
    "NachoBot/template/model_config_template.toml": "NachoBot/config/model_config.toml",
    "NachoBot/template/topics_config_template.toml": "NachoBot/config/topics_config.toml",
    "NachoBot/template/template.env": "NachoBot/.env",
    "NachoBot-Napcat-Adapter/template/template_config.toml": "NachoBot-Napcat-Adapter/config.toml",
    "NachoBot-TTS-Adapter/template_configs/base_template.toml": "NachoBot-TTS-Adapter/configs/base.toml",
    "NachoBot-TTS-Adapter/template_configs/gpt-sovits_template.toml": "NachoBot-TTS-Adapter/configs/gpt-sovits.toml",
    "NachoBot-TTS-Adapter/template_configs/vox_template.toml": "NachoBot-TTS-Adapter/configs/vox.toml",
    "NachoBot-UniversalVC-Adapter/template/config_template.toml": "NachoBot-UniversalVC-Adapter/config.toml",
}


# ---------------------------------------------------------------------------
# Known ports used by services
# ---------------------------------------------------------------------------

KNOWN_PORTS: dict[str, int] = {
    "NachoBot Core": 8000,
    "Napcat Adapter": 8095,
    "TTS Adapter": 8070,
    "TTS Engine": 9880,
    "Perception API": 9874,
    "Koishi": 5140,
    "WebUI": 8088,
}


# =========================================================================
# Environment Checker
# =========================================================================


class EnvironmentChecker:
    """Checks the runtime environment required for deployment."""

    @staticmethod
    def check_all() -> dict[str, Any]:
        """Run all environment checks and return results."""
        return {
            "python": EnvironmentChecker.check_python(),
            "node": EnvironmentChecker.check_node(),
            "docker": EnvironmentChecker.check_docker(),
            "gpu": EnvironmentChecker.check_gpu(),
            "ports": EnvironmentChecker.check_ports(),
            "configs": EnvironmentChecker.check_configs(),
        }

    @staticmethod
    def check_python() -> dict[str, Any]:
        """Check Python and uv availability."""
        result = {"status": "error", "python": None, "uv": None, "message": ""}

        # Check Python
        try:
            out = subprocess.run(
                ["python", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                version_str = out.stdout.strip() or out.stderr.strip()
                result["python"] = version_str
            else:
                result["message"] = "Python 未找到"
                return result
        except FileNotFoundError:
            result["message"] = "Python 未安装或不在 PATH 中"
            return result
        except Exception as e:
            result["message"] = f"检测 Python 时出错: {e}"
            return result

        # Check uv
        try:
            out = subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["uv"] = out.stdout.strip()
                result["status"] = "ok"
                result["message"] = f"{result['python']} · {result['uv']}"
            else:
                result["status"] = "warning"
                result["message"] = f"{result['python']} (uv 未安装 — 建议安装)"
        except FileNotFoundError:
            result["status"] = "warning"
            result["message"] = f"{result['python']} (uv 未安装 — 建议安装)"
        except Exception:
            result["status"] = "warning"
            result["message"] = f"{result['python']} (uv 检测失败)"

        return result

    @staticmethod
    def check_node() -> dict[str, Any]:
        """Check Node.js availability (optional, for Koishi)."""
        result = {"status": "warning", "node": None, "npm": None, "message": ""}

        try:
            out = subprocess.run(
                ["node", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["node"] = out.stdout.strip()
        except (FileNotFoundError, Exception):
            pass

        try:
            out = subprocess.run(
                ["npm", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["npm"] = f"npm {out.stdout.strip()}"
        except (FileNotFoundError, Exception):
            pass

        if result["node"]:
            result["status"] = "ok"
            parts = [f"Node.js {result['node']}"]
            if result["npm"]:
                parts.append(result["npm"])
            result["message"] = " · ".join(parts)
        else:
            result["status"] = "warning"
            result["message"] = "Node.js 未安装 (仅 Discord/Koishi 适配器需要)"

        return result

    @staticmethod
    def check_docker() -> dict[str, Any]:
        """Check Docker availability (optional)."""
        result = {"status": "warning", "docker": None, "compose": None, "message": ""}

        try:
            out = subprocess.run(
                ["docker", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["docker"] = out.stdout.strip()
        except (FileNotFoundError, Exception):
            pass

        try:
            out = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["compose"] = out.stdout.strip()
        except (FileNotFoundError, Exception):
            pass

        if result["docker"]:
            result["status"] = "ok"
            parts = [result["docker"]]
            if result["compose"]:
                parts.append(result["compose"])
            result["message"] = " · ".join(parts)
        else:
            result["status"] = "warning"
            result["message"] = "Docker 未安装 (可选 — 用于容器化部署)"

        return result

    @staticmethod
    def check_ports() -> list[dict[str, Any]]:
        """Check port availability for all known services."""
        # Dynamically retrieve current WebUI port
        try:
            from webui_config import webui_config

            webui_port = webui_config.port
        except Exception:
            try:
                from .webui_config import webui_config

                webui_port = webui_config.port
            except Exception:
                webui_port = 8088

        results = []
        for name, port in KNOWN_PORTS.items():
            if name == "WebUI":
                port = webui_port
            entry = {
                "name": name,
                "port": port,
                "status": "ok",
                "message": "",
                "pid": None,
            }
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    # Port is in use
                    entry["status"] = "warning"
                    entry["message"] = f"端口 {port} 已被占用"
                    # Try to find PID
                    try:
                        import psutil

                        for conn in psutil.net_connections(kind="inet"):
                            if conn.laddr.port == port and conn.status == "LISTEN":
                                entry["pid"] = conn.pid
                                if conn.pid == os.getpid():
                                    entry["status"] = "ok"
                                    entry["message"] = (
                                        f"端口 {port} 由当前 WebUI 占用 (正常)"
                                    )
                                else:
                                    try:
                                        proc = psutil.Process(conn.pid)
                                        entry["message"] = (
                                            f"端口 {port} 被 {proc.name()} (PID:{conn.pid}) 占用"
                                        )
                                    except Exception:
                                        entry["message"] = (
                                            f"端口 {port} 被占用 (PID:{conn.pid})"
                                        )
                                break
                    except Exception:
                        pass

                    # Fallback: if it is the WebUI port and we are running the check,
                    # we are definitely the one listening on it (or it's the current WebUI instance).
                    if name == "WebUI" and (
                        entry["pid"] is None or entry["pid"] == os.getpid()
                    ):
                        entry["status"] = "ok"
                        entry["message"] = f"端口 {port} 由当前 WebUI 占用 (正常)"
                        if entry["pid"] is None:
                            entry["pid"] = os.getpid()

            except (ConnectionRefusedError, OSError, socket.timeout):
                entry["status"] = "ok"
                entry["message"] = f"端口 {port} 可用"
            results.append(entry)
        return results

    @staticmethod
    def check_configs() -> list[dict[str, Any]]:
        """Check which config files exist and which are missing."""
        results = []
        for tmpl, target in TEMPLATE_MAP.items():
            target_path = ROOT_DIR / target
            tmpl_path = ROOT_DIR / tmpl
            results.append(
                {
                    "template": tmpl,
                    "target": target,
                    "target_exists": target_path.exists(),
                    "template_exists": tmpl_path.exists(),
                    "filename": Path(target).name,
                    "component": target.split("/")[0],
                }
            )
        return results

    @staticmethod
    def check_gpu() -> dict[str, Any]:
        """Check GPU availability and VRAM size (in MB)."""
        result = {
            "status": "ok",
            "has_gpu": False,
            "gpu_name": None,
            "vram_mb": 0.0,
            "message": "未检测到可用 NVIDIA 显卡",
        }

        # 1. Try nvidia-smi (reliable for NVIDIA CUDA GPUs)
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                shell=True,
            )
            if out.returncode == 0:
                lines = [
                    line.strip()
                    for line in out.stdout.strip().split("\n")
                    if line.strip()
                ]
                gpus = []
                for line in lines:
                    parts = line.split(",")
                    if len(parts) >= 2:
                        name = parts[0].strip()
                        try:
                            vram = float(parts[1].strip())
                        except ValueError:
                            vram = 0.0
                        gpus.append((name, vram))
                if gpus:
                    gpus.sort(key=lambda x: x[1], reverse=True)
                    best_gpu = gpus[0]
                    result["has_gpu"] = True
                    result["gpu_name"] = best_gpu[0]
                    result["vram_mb"] = best_gpu[1]
                    vram_gb = best_gpu[1] / 1024.0
                    result["message"] = f"{best_gpu[0]} (显存 {vram_gb:.2f} GB)"
                    return result
        except Exception:
            pass

        # 2. Try wmic path win32_VideoController (fallback to check all GPUs)
        try:
            out = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "Name,AdapterRAM"],
                capture_output=True,
                text=True,
                timeout=5,
                shell=True,
            )
            if out.returncode == 0:
                lines = [
                    line.strip()
                    for line in out.stdout.strip().split("\n")
                    if line.strip()
                ]
                if len(lines) > 1:
                    gpus = []
                    header = lines[0].lower()
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 2:
                            try:
                                if header.startswith("adapterram"):
                                    ram_str = parts[0]
                                    name_str = " ".join(parts[1:])
                                else:
                                    ram_str = parts[-1]
                                    name_str = " ".join(parts[:-1])

                                ram_bytes = float(ram_str.strip())
                                vram_mb = ram_bytes / (1024.0 * 1024.0)
                                name = name_str.strip()
                                gpus.append((name, vram_mb))
                            except ValueError:
                                pass
                    if gpus:
                        gpus.sort(key=lambda x: x[1], reverse=True)
                        best_gpu = gpus[0]
                        is_nvidia = "nvidia" in best_gpu[0].lower()
                        result["has_gpu"] = is_nvidia
                        result["gpu_name"] = best_gpu[0]
                        result["vram_mb"] = best_gpu[1]
                        vram_gb = best_gpu[1] / 1024.0
                        if is_nvidia:
                            result["message"] = f"{best_gpu[0]} (显存 {vram_gb:.2f} GB)"
                        else:
                            result["message"] = (
                                f"{best_gpu[0]} (非 NVIDIA 显卡，显存 {vram_gb:.2f} GB)"
                            )
                        return result
        except Exception:
            pass

        return result


# =========================================================================
# Backup Manager
# =========================================================================


class BackupManager:
    """Manages config backups with rotation (max N per file)."""

    @staticmethod
    def backup(file_path: Path) -> str | None:
        """Create a timestamped backup. Rotates old backups."""
        if not file_path.exists():
            return None

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        # Use a flat name: component__filename to avoid directory nesting
        relative = file_path.relative_to(ROOT_DIR)
        safe_name = str(relative).replace("/", "__").replace("\\", "__")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_name = f"{safe_name}.{ts}.bak"
        bak_path = BACKUP_DIR / bak_name

        shutil.copy2(file_path, bak_path)

        # Rotate: keep only MAX_BACKUPS_PER_FILE newest backups for this file
        prefix = f"{safe_name}."
        existing = sorted(
            [
                f
                for f in BACKUP_DIR.iterdir()
                if f.name.startswith(prefix) and f.name.endswith(".bak")
            ],
            key=lambda p: p.stat().st_mtime,
        )
        while len(existing) > MAX_BACKUPS_PER_FILE:
            oldest = existing.pop(0)
            oldest.unlink(missing_ok=True)

        return str(bak_path)


# =========================================================================
# Config Initializer
# =========================================================================


class ConfigInitializer:
    """Generates config files from templates and applies wizard form data."""

    @staticmethod
    def get_status() -> list[dict[str, Any]]:
        """Return the status of each config file (exists/missing)."""
        return EnvironmentChecker.check_configs()

    @staticmethod
    def generate_configs(wizard_data: dict[str, Any]) -> dict[str, Any]:
        """
        Generate config files from templates, applying wizard form data.

        wizard_data keys:
          - components: list[str]  — selected component IDs
          - core: dict             — core settings (nickname, qq_account, etc.)
          - llm: dict              — LLM provider settings (api_provider, api_key, base_url)
          - napcat: dict           — Napcat adapter settings
          - tts: dict              — TTS settings (engine, etc.)
          - env: dict              — .env overrides (HOST, PORT)

        Returns:
          {"generated": [...], "skipped": [...], "backups": [...], "errors": [...]}
        """
        components = set(wizard_data.get("components", []))
        generated = []
        skipped = []
        backups = []
        errors = []

        # Determine TTS enablement for chain routing
        tts_enabled = "tts" in components

        for tmpl_rel, target_rel in TEMPLATE_MAP.items():
            tmpl_path = ROOT_DIR / tmpl_rel
            target_path = ROOT_DIR / target_rel

            # Skip components not selected
            component_id = target_rel.split("/")[0]
            should_generate = ConfigInitializer._should_generate(
                component_id, target_rel, components
            )
            if not should_generate:
                skipped.append(target_rel)
                continue

            if not tmpl_path.exists():
                errors.append(f"模板不存在: {tmpl_rel}")
                continue

            try:
                # Backup existing file
                if target_path.exists():
                    bak = BackupManager.backup(target_path)
                    if bak:
                        backups.append(bak)

                # Ensure target directory exists
                target_path.parent.mkdir(parents=True, exist_ok=True)

                # Copy template to target
                shutil.copy2(tmpl_path, target_path)

                # Apply wizard data overrides
                override_err = ConfigInitializer._apply_overrides(
                    target_path, target_rel, wizard_data, tts_enabled
                )
                if override_err:
                    errors.append(f"覆写失败 {target_rel}: {override_err}")

                generated.append(target_rel)
            except Exception as e:
                errors.append(f"{target_rel}: {e}")

        # Post-generation: patch TTS chain routing in ALL existing adapter configs
        # This covers adapters that were not generated from templates (e.g. Koishi, Bilibili)
        patch_results = ConfigInitializer._patch_tts_chain(tts_enabled, components)
        errors.extend(patch_results.get("errors", []))

        return {
            "generated": generated,
            "skipped": skipped,
            "backups": backups,
            "errors": errors,
            "patched": patch_results.get("patched", []),
        }

    # Adapter configs that may need TTS chain patching.
    # Each entry: (config path, has nachobot_server port, has voice.use_tts)
    _TTS_CHAIN_ADAPTERS: list[tuple[str, str, bool]] = [
        ("NachoBot-Napcat-Adapter/config.toml", "qq", True),
        ("NachoBot-Koishi-Adapter/config.toml", "discord", True),
        # Bilibili connects directly to Core (port 8000), no TTS chain
        # DiscordVC / UniversalVC also connect directly to Core
    ]

    @staticmethod
    def _patch_tts_chain(
        tts_enabled: bool,
        components: set,
    ) -> dict[str, Any]:
        """
        Scan all known adapter configs and adjust TTS chain routing:
        - nachobot_server.port → 8070 (with TTS) or 8000 (without TTS)
        - voice.use_tts → true/false
        Only patches adapters that were selected by the user.
        """
        patched = []
        errors = []

        for rel_path, component_id, has_voice in ConfigInitializer._TTS_CHAIN_ADAPTERS:
            # Only patch adapters the user selected
            if component_id not in components:
                continue

            config_path = ROOT_DIR / rel_path
            if not config_path.exists():
                continue

            try:
                raw = config_path.read_text(encoding="utf-8")
                doc = tomlkit.parse(raw)
                changed = False

                if "nachobot_server" in doc:
                    target_port = 8070 if tts_enabled else 8000
                    if doc["nachobot_server"].get("port") != target_port:
                        doc["nachobot_server"]["port"] = target_port
                        changed = True

                if has_voice and "voice" in doc:
                    if doc["voice"].get("use_tts") != tts_enabled:
                        doc["voice"]["use_tts"] = tts_enabled
                        changed = True

                if changed:
                    # Backup before patching
                    BackupManager.backup(config_path)
                    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
                    patched.append(rel_path)

            except Exception as e:
                errors.append(f"TTS链路修补失败 {rel_path}: {e}")

        return {"patched": patched, "errors": errors}

    @staticmethod
    def _should_generate(component_id: str, target_rel: str, components: set) -> bool:
        """Determine if a config file should be generated based on selected components."""
        # Core configs are always generated
        if component_id == "NachoBot":
            return True

        # Adapter configs only when their component is selected
        mapping = {
            "NachoBot-Napcat-Adapter": "qq",
            "NachoBot-TTS-Adapter": "tts",
            "NachoBot-Bilibili-Adapter": "bilibili",
            "NachoBot-Koishi-Adapter": "discord",
            "NachoBot-DiscordVC-Adapter": "discord",
            "NachoBot-UniversalVC-Adapter": "universalvc",
        }
        required = mapping.get(component_id)
        if required:
            return required in components

        return True  # Unknown → generate

    @staticmethod
    def _apply_overrides(
        target_path: Path,
        target_rel: str,
        wizard_data: dict[str, Any],
        tts_enabled: bool,
    ) -> str | None:
        """
        Apply wizard form data to a generated config file.
        Returns None on success, or an error message string on failure.
        """
        filename = target_path.name

        # ── .env file ──
        if filename == ".env":
            env_data = wizard_data.get("env", {})
            host = env_data.get("host", "127.0.0.1")
            port = env_data.get("port", "8000")
            target_path.write_text(f"HOST={host}\nPORT={port}\n", encoding="utf-8")
            return None

        # ── TOML files ──
        try:
            raw = target_path.read_text(encoding="utf-8")
            doc = tomlkit.parse(raw)
        except Exception as e:
            return f"TOML解析失败: {e}"

        changed = False

        # -- bot_config.toml --
        if "bot_config" in target_rel:
            core_data = wizard_data.get("core", {})
            qq_account = core_data.get("qq_account", "")
            nickname = core_data.get("nickname", "")
            if qq_account and "bot" in doc:
                doc["bot"]["qq_account"] = qq_account
                changed = True
            if nickname and "bot" in doc:
                doc["bot"]["nickname"] = nickname
                changed = True

        # -- model_config.toml --
        if "model_config" in target_rel:
            user_providers = wizard_data.get("providers", [])
            user_models = wizard_data.get("models", [])

            # Replace api_providers with user-provided ones
            if user_providers:
                aot = tomlkit.aot()
                for p in user_providers:
                    t = tomlkit.table()
                    t.add("name", p.get("name", ""))
                    t.add("base_url", p.get("base_url", ""))
                    t.add("api_key", p.get("api_key", ""))
                    t.add("client_type", "openai")
                    t.add("max_retry", 2)
                    t.add("timeout", 30)
                    t.add("retry_interval", 5)
                    aot.append(t)
                doc["api_providers"] = aot
                changed = True

            # Replace models with user-provided ones
            if user_models:
                aot = tomlkit.aot()
                for m in user_models:
                    t = tomlkit.table()
                    t.add("model_identifier", m.get("model_identifier", ""))
                    t.add(
                        "name", m.get("model_name", "") or m.get("model_identifier", "")
                    )
                    t.add("api_provider", m.get("api_provider", ""))
                    t.add("price_in", 0)
                    t.add("price_out", 0)
                    aot.append(t)
                doc["models"] = aot
                changed = True

                # Collect all model names for model group assignment
                model_names = []
                for m in user_models:
                    name = m.get("model_name", "") or m.get("model_identifier", "")
                    if name:
                        model_names.append(name)

                # Set all required model groups to use the user's models
                required_groups = [
                    "replyer0",
                    "planner",
                    "utils",
                    "utils_small",
                    "tool_use",
                ]
                if model_names and "model_task_config" in doc:
                    for group in required_groups:
                        if group in doc["model_task_config"]:
                            doc["model_task_config"][group]["model_list"] = model_names
                            changed = True

        # -- Napcat adapter config.toml — TTS chain only --
        if "NachoBot-Napcat-Adapter" in target_rel and filename == "config.toml":
            # TTS chain routing
            if "nachobot_server" in doc:
                if tts_enabled:
                    doc["nachobot_server"]["port"] = 8070
                else:
                    doc["nachobot_server"]["port"] = 8000
                changed = True
            if "voice" in doc:
                doc["voice"]["use_tts"] = tts_enabled
                changed = True

        # -- Koishi adapter config.toml --
        if "NachoBot-Koishi-Adapter" in target_rel and filename == "config.toml":
            if "nachobot_server" in doc:
                if tts_enabled:
                    doc["nachobot_server"]["port"] = 8070
                else:
                    doc["nachobot_server"]["port"] = 8000
                changed = True
            if "voice" in doc:
                doc["voice"]["use_tts"] = tts_enabled
                changed = True

        # -- TTS base.toml --
        if "NachoBot-TTS-Adapter" in target_rel and "base" in filename:
            tts = wizard_data.get("tts", {})
            engine = tts.get("engine", "GPT_Sovits")
            if "enabled_tts" in doc:
                doc["enabled_tts"]["enabled"] = [engine]
                changed = True

        # -- UniversalVC adapter config.toml --
        if "NachoBot-UniversalVC-Adapter" in target_rel and filename == "config.toml":
            uvc = wizard_data.get("universalvc", {})
            target_process = uvc.get("target_process_name", "")
            output_device = uvc.get("output_device", "")
            denoise_enabled = uvc.get("denoise_enabled", True)
            speaker_enabled = uvc.get("speaker_enabled", True)

            if "capture" in doc:
                if target_process:
                    doc["capture"]["target_process_name"] = target_process
                changed = True
            if "output" in doc and output_device:
                doc["output"]["device_name"] = output_device
                changed = True
            if "denoise" in doc:
                doc["denoise"]["enabled"] = denoise_enabled
                changed = True
            if "speaker" in doc:
                doc["speaker"]["enabled"] = speaker_enabled
                changed = True

        if changed:
            try:
                target_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
            except Exception as e:
                return f"写入失败: {e}"

        return None  # success


# =========================================================================
# Path Verifier — checks external dependency installation
# =========================================================================


class PathVerifier:
    """Verify that external dependencies are installed at the given paths."""

    # Each entry: (check_type, display_name, validation function, download_url)
    CHECKS = {
        "napcat": {
            "name": "NapCat Shell",
            "hint": "NapCat Shell 安装目录（包含 launcher-user.bat）",
            "download_url": "https://github.com/NapNeko/NapCatQQ/releases",
            "default_rel": "NapCat.Shell",
        },
        "sovits": {
            "name": "GPT-SoVITS",
            "hint": "GPT-SoVITS 安装目录（包含 runtime/python.exe）",
            "download_url": "https://www.yuque.com/baicaigongchang1145haoyuangong/ib3g1e/dkxgpiy9zb96hob4",
            "default_rel": None,
        },
        "voxcpm": {
            "name": "VoxCPM",
            "hint": "VoxCPM 安装目录（包含 .venv/Scripts/python.exe）",
            "download_url": "https://github.com/openbmb/VoxCPM/releases",
            "default_rel": None,
        },
        "nodejs": {
            "name": "Node.js",
            "hint": "系统已安装 Node.js（自动检测 PATH）",
            "download_url": "https://nodejs.org/en/download/",
            "default_rel": None,
        },
        "bilibili_dll": {
            "name": "Live2D Cubism Core",
            "hint": "NachoBot-Bilibili-Adapter 目录下的 Live2DCubismCore.dll",
            "download_url": "https://www.live2d.com/sdk/download/native/",
            "default_rel": None,
        },
        "vb_cable": {
            "name": "VB-Audio Virtual Cable",
            "hint": "VB-Audio Virtual Cable 安装目录（包含 VBCABLE_Setup_x64.exe）",
            "download_url": "https://vb-audio.com/Cable/",
            "default_rel": None,
        },
    }

    @staticmethod
    def verify_path(check_type: str, path: str = "") -> dict[str, Any]:
        """
        Verify an external dependency.

        Returns:
            {"valid": bool, "message": str, "download_url": str}
        """
        info = PathVerifier.CHECKS.get(check_type)
        if not info:
            return {
                "valid": False,
                "message": f"未知检查类型: {check_type}",
                "download_url": "",
            }

        download_url = info["download_url"]

        # -- Node.js: check via PATH, no user path needed --
        if check_type == "nodejs":
            return PathVerifier._check_nodejs(download_url)

        # -- Bilibili DLL: fixed path under project root --
        if check_type == "bilibili_dll":
            return PathVerifier._check_bilibili_dll(download_url)

        # -- Path-based checks --
        if not path or not path.strip():
            return {
                "valid": False,
                "message": "请输入路径",
                "download_url": download_url,
            }

        p = Path(path.strip())
        if not p.exists():
            return {
                "valid": False,
                "message": f"路径不存在: {p}",
                "download_url": download_url,
            }
        if not p.is_dir():
            return {
                "valid": False,
                "message": f"路径不是目录: {p}",
                "download_url": download_url,
            }

        if check_type == "napcat":
            return PathVerifier._check_napcat(p, download_url)
        elif check_type == "sovits":
            return PathVerifier._check_sovits(p, download_url)
        elif check_type == "voxcpm":
            return PathVerifier._check_voxcpm(p, download_url)
        elif check_type == "vb_cable":
            return PathVerifier._check_vb_cable(p, download_url)

        return {"valid": False, "message": "未知检查类型", "download_url": download_url}

    @staticmethod
    def _check_napcat(p: Path, download_url: str) -> dict:
        launcher = p / "launcher-user.bat"
        if launcher.exists():
            return {"valid": True, "message": f"✅ NapCat Shell 已找到: {p}"}
        # Also try napcat.bat as fallback
        napcat_bat = p / "napcat.bat"
        if napcat_bat.exists():
            return {"valid": True, "message": f"✅ NapCat Shell 已找到: {p}"}
        return {
            "valid": False,
            "message": f"❌ 未找到 launcher-user.bat: {p}",
            "download_url": download_url,
        }

    @staticmethod
    def _check_sovits(p: Path, download_url: str) -> dict:
        py_exe = p / "runtime" / "python.exe"
        if py_exe.exists():
            return {"valid": True, "message": f"✅ GPT-SoVITS 已找到: {p}"}
        # Alternative: check for api_v2.py
        api_file = p / "api_v2.py"
        if api_file.exists():
            return {"valid": True, "message": f"✅ GPT-SoVITS 已找到: {p}"}
        return {
            "valid": False,
            "message": f"❌ 未找到 runtime/python.exe 或 api_v2.py: {p}",
            "download_url": download_url,
        }

    @staticmethod
    def _check_voxcpm(p: Path, download_url: str) -> dict:
        venv_py = p / ".venv" / "Scripts" / "python.exe"
        if venv_py.exists():
            return {"valid": True, "message": f"✅ VoxCPM 已找到: {p}"}
        # Also accept if models dir exists
        models_dir = p / "models"
        if models_dir.exists():
            return {"valid": True, "message": f"✅ VoxCPM 已找到 (models目录): {p}"}
        return {
            "valid": False,
            "message": f"❌ 未找到 .venv/Scripts/python.exe: {p}",
            "download_url": download_url,
        }

    @staticmethod
    def _check_nodejs(download_url: str) -> dict:
        try:
            result = subprocess.run(
                ["node", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                ver = result.stdout.strip()
                return {"valid": True, "message": f"✅ Node.js 已安装: {ver}"}
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return {
            "valid": False,
            "message": "❌ 未检测到 Node.js，Discord (Koishi) 适配器需要 Node.js",
            "download_url": download_url,
        }

    @staticmethod
    def _check_bilibili_dll(download_url: str) -> dict:
        dll_path = ROOT_DIR / "NachoBot-Bilibili-Adapter" / "Live2DCubismCore.dll"
        if dll_path.exists():
            return {"valid": True, "message": "✅ Live2DCubismCore.dll 已找到"}
        return {
            "valid": False,
            "message": "❌ 未找到 NachoBot-Bilibili-Adapter/Live2DCubismCore.dll",
            "download_url": download_url,
        }

    @staticmethod
    def _check_vb_cable(p: Path, download_url: str) -> dict:
        """Verify VB-Audio Virtual Cable installation directory."""
        # Check for the setup executable (main indicator)
        setup_x64 = p / "VBCABLE_Setup_x64.exe"
        setup_x86 = p / "VBCABLE_Setup.exe"
        # Also accept the driver file directly
        driver_cat = p / "vbaudio_cable64_win10.cat"
        if setup_x64.exists() or setup_x86.exists() or driver_cat.exists():
            return {"valid": True, "message": f"✅ VB-Audio Virtual Cable 已找到: {p}"}
        # Fuzzy check: look for any VB-Audio related exe or sys file
        vb_files = list(p.glob("VBCABLE*")) + list(p.glob("vbaudio*"))
        if vb_files:
            return {"valid": True, "message": f"✅ VB-Audio Virtual Cable 已找到: {p}"}
        return {
            "valid": False,
            "message": f"❌ 未找到 VB-Audio Virtual Cable 安装文件: {p}",
            "download_url": download_url,
        }


# =========================================================================
# NapCat Configurator — auto-configure onebot11 connections
# =========================================================================


class NapCatConfigurator:
    """
    Automatically configure NapCat Shell's onebot11 config files.
    Adds WebSocket client (NachoBot), diary HTTP server, and bilibili video HTTP server.
    """

    # Standard WebSocket client entry for NachoBot
    _WS_CLIENT_ENTRY = {
        "enable": True,
        "name": "NachoBot",
        "url": "ws://localhost:8095",
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": "",
        "debug": False,
        "heartInterval": 30000,
        "reconnectInterval": 30000,
    }

    # Diary plugin HTTP server entry (README L80)
    _DIARY_HTTP_ENTRY = {
        "enable": True,
        "name": "Diary",
        "host": "127.0.0.1",
        "port": 9997,
        "enableCors": True,
        "enableWebsocket": True,
        "messagePostFormat": "array",
        "token": "",
        "debug": False,
    }

    # Bilibili video plugin HTTP server entry (README L81)
    _BILIBILI_HTTP_ENTRY = {
        "enable": True,
        "name": "QZone",
        "host": "127.0.0.1",
        "port": 9999,
        "enableCors": True,
        "enableWebsocket": True,
        "messagePostFormat": "array",
        "token": "",
        "debug": False,
    }

    @staticmethod
    def detect_accounts(napcat_dir: str) -> list[str]:
        """
        Scan NapCat config directory for existing onebot11_<QQ>.json files.
        Returns list of QQ account numbers found.
        """
        config_dir = Path(napcat_dir) / "config"
        if not config_dir.exists():
            return []

        accounts = []
        import re

        pattern = re.compile(r"^onebot11_(\d+)\.json$")
        for f in config_dir.iterdir():
            m = pattern.match(f.name)
            if m:
                accounts.append(m.group(1))
        return sorted(accounts)

    @staticmethod
    def configure(napcat_dir: str, qq_account: str = "") -> dict[str, Any]:
        """
        Auto-configure NapCat onebot11 config files.

        Adds:
          - WebSocket client: ws://localhost:8095 (NachoBot adapter)
          - HTTP server: port 9997 (diary plugin, CORS + WS)
          - HTTP server: port 9999 (bilibili video plugin, CORS + WS)

        Args:
            napcat_dir: Path to NapCat Shell root directory.
            qq_account: QQ account number. If empty, auto-detect from existing files.

        Returns:
            {"configured": [...], "skipped": [...], "errors": [...]}
        """

        config_dir = Path(napcat_dir) / "config"
        configured = []
        skipped = []
        errors = []

        if not config_dir.exists():
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return {
                    "configured": [],
                    "skipped": [],
                    "errors": [f"无法创建配置目录: {e}"],
                }

        # Determine target files
        target_files: list[Path] = []

        if qq_account and qq_account.strip():
            # Specific QQ account
            target = config_dir / f"onebot11_{qq_account.strip()}.json"
            target_files.append(target)
        else:
            # Auto-detect: scan for existing onebot11_*.json files
            import re

            pattern = re.compile(r"^onebot11_\d+\.json$")
            for f in config_dir.iterdir():
                if pattern.match(f.name):
                    target_files.append(f)

            # Fallback: create default onebot11.json if nothing found
            if not target_files:
                target_files.append(config_dir / "onebot11.json")

        for target_path in target_files:
            try:
                result = NapCatConfigurator._configure_file(target_path)
                if result["changed"]:
                    configured.append(str(target_path.name))
                else:
                    skipped.append(str(target_path.name))
            except Exception as e:
                errors.append(f"{target_path.name}: {e}")

        return {"configured": configured, "skipped": skipped, "errors": errors}

    @staticmethod
    def _configure_file(target_path: Path) -> dict[str, bool]:
        """
        Configure a single onebot11 JSON file.
        Creates it from scratch if it doesn't exist.
        Returns {"changed": bool}.
        """
        import json as _json

        if target_path.exists():
            raw = target_path.read_text(encoding="utf-8")
            try:
                doc = _json.loads(raw)
            except _json.JSONDecodeError:
                doc = {}
        else:
            doc = {}

        changed = False

        # Ensure top-level structure
        if "network" not in doc:
            doc["network"] = {}
            changed = True
        network = doc["network"]

        # --- WebSocket Clients ---
        if "websocketClients" not in network:
            network["websocketClients"] = []
        ws_clients = network["websocketClients"]

        # Check if NachoBot WS client already exists
        has_nachobot_ws = any(c.get("url") == "ws://localhost:8095" for c in ws_clients)
        if not has_nachobot_ws:
            ws_clients.append(dict(NapCatConfigurator._WS_CLIENT_ENTRY))
            changed = True

        # --- HTTP Servers ---
        if "httpServers" not in network:
            network["httpServers"] = []
        http_servers = network["httpServers"]

        # Check if diary HTTP server already exists (port 9997)
        has_diary = any(s.get("port") == 9997 for s in http_servers)
        if not has_diary:
            http_servers.append(dict(NapCatConfigurator._DIARY_HTTP_ENTRY))
            changed = True

        # Check if bilibili video HTTP server already exists (port 9999)
        has_bilibili = any(s.get("port") == 9999 for s in http_servers)
        if not has_bilibili:
            http_servers.append(dict(NapCatConfigurator._BILIBILI_HTTP_ENTRY))
            changed = True

        # Ensure other standard arrays exist
        for key in ["httpSseServers", "httpClients", "websocketServers", "plugins"]:
            if key not in network:
                network[key] = []

        # Ensure top-level defaults
        doc.setdefault("musicSignUrl", "")
        doc.setdefault("enableLocalFile2Url", False)
        doc.setdefault("parseMultMsg", False)

        if changed:
            # Backup existing file before writing
            if target_path.exists():
                BackupManager.backup(target_path)
            target_path.write_text(
                _json.dumps(doc, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        return {"changed": changed}


# =========================================================================
# Dependency Installer
# =========================================================================


class DependencyInstaller:
    """Installs project dependencies via uv sync / npm install."""

    # Projects that need uv sync, mapped by component ID
    UV_PROJECTS: dict[str, str] = {
        "core": "NachoBot",
        "qq": "NachoBot-Napcat-Adapter",
        "tts": "NachoBot-TTS-Adapter",
        "bilibili": "NachoBot-Bilibili-Adapter",
        "discord_koishi": "NachoBot-Koishi-Adapter",
        "discord_vc": "NachoBot-DiscordVC-Adapter",
        "universalvc": "NachoBot-UniversalVC-Adapter",
        "webui": "webUI",
    }

    # Projects that need npm install
    NPM_PROJECTS: dict[str, str] = {
        "discord_koishi_npm": "koishi-app",
    }

    @staticmethod
    def get_install_tasks(components: list[str]) -> list[dict[str, str]]:
        """Return the list of install tasks based on selected components."""
        tasks = []

        # Always install core
        tasks.append(
            {
                "id": "core",
                "type": "uv",
                "name": "NachoBot Core",
                "dir": "NachoBot",
            }
        )

        component_set = set(components)

        if "qq" in component_set:
            tasks.append(
                {
                    "id": "qq",
                    "type": "uv",
                    "name": "Napcat Adapter",
                    "dir": "NachoBot-Napcat-Adapter",
                }
            )

        if "tts" in component_set:
            tasks.append(
                {
                    "id": "tts",
                    "type": "uv",
                    "name": "TTS Adapter",
                    "dir": "NachoBot-TTS-Adapter",
                }
            )

        if "bilibili" in component_set:
            tasks.append(
                {
                    "id": "bilibili",
                    "type": "uv",
                    "name": "Bilibili Adapter",
                    "dir": "NachoBot-Bilibili-Adapter",
                }
            )

        if "discord" in component_set:
            tasks.append(
                {
                    "id": "discord_koishi",
                    "type": "uv",
                    "name": "Koishi Adapter",
                    "dir": "NachoBot-Koishi-Adapter",
                }
            )
            tasks.append(
                {
                    "id": "discord_vc",
                    "type": "uv",
                    "name": "DiscordVC Adapter",
                    "dir": "NachoBot-DiscordVC-Adapter",
                }
            )
            tasks.append(
                {
                    "id": "discord_koishi_npm",
                    "type": "npm",
                    "name": "Koishi App (npm)",
                    "dir": "koishi-app",
                }
            )

        if "universalvc" in component_set:
            tasks.append(
                {
                    "id": "universalvc",
                    "type": "uv",
                    "name": "UniversalVC Adapter",
                    "dir": "NachoBot-UniversalVC-Adapter",
                }
            )

        return tasks

    @staticmethod
    async def install(
        task: dict[str, str],
        callback: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """
        Run uv sync or npm install for a single project.
        Returns {"status": "ok"|"error", "message": "..."}.
        """
        project_dir = ROOT_DIR / task["dir"]
        if not project_dir.exists():
            return {"status": "error", "message": f"目录不存在: {task['dir']}"}

        if task["type"] == "uv":
            return await DependencyInstaller._run_uv_sync(project_dir, callback)
        elif task["type"] == "npm":
            return await DependencyInstaller._run_npm_install(project_dir, callback)
        else:
            return {"status": "error", "message": f"未知安装类型: {task['type']}"}

    @staticmethod
    async def _run_uv_sync(
        project_dir: Path,
        callback: Callable[[str], Any] | None,
    ) -> dict[str, Any]:
        """Execute `uv sync` in a project directory."""
        import locale

        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        try:
            proc = await asyncio.create_subprocess_exec(
                "uv",
                "sync",
                "--python",
                ">=3.11,<=3.13",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_dir),
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
                if callback:
                    await callback(text)

            await proc.wait()

            if proc.returncode == 0:
                return {"status": "ok", "message": "依赖安装完成"}
            else:
                return {
                    "status": "error",
                    "message": f"uv sync 退出码: {proc.returncode}",
                }
        except FileNotFoundError:
            return {"status": "error", "message": "uv 未安装，请先安装 uv"}
        except Exception as e:
            return {"status": "error", "message": f"安装出错: {e}"}

    @staticmethod
    async def _run_npm_install(
        project_dir: Path,
        callback: Callable[[str], Any] | None,
    ) -> dict[str, Any]:
        """Execute `npm install` in a project directory."""
        import locale

        env = os.environ.copy()

        try:
            proc = await asyncio.create_subprocess_exec(
                "cmd",
                "/c",
                "npm",
                "install",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_dir),
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
                if callback:
                    await callback(text)

            await proc.wait()

            if proc.returncode == 0:
                return {"status": "ok", "message": "npm install 完成"}
            else:
                return {
                    "status": "error",
                    "message": f"npm install 退出码: {proc.returncode}",
                }
        except FileNotFoundError:
            return {"status": "error", "message": "npm 未安装"}
        except Exception as e:
            return {"status": "error", "message": f"安装出错: {e}"}
