"""Configuration generation and dependency deployment for the WebUI wizard."""

import asyncio
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tomlkit

try:
    from .setup_checks import EnvironmentChecker, ROOT_DIR, TEMPLATE_MAP
    from .secure_paths import ensure_within, resolve_external_path, resolve_relative_to_root
except ImportError:
    from setup_checks import EnvironmentChecker, ROOT_DIR, TEMPLATE_MAP
    from secure_paths import ensure_within, resolve_external_path, resolve_relative_to_root

BACKUP_DIR = ROOT_DIR / "config-save" / "setup_backups"
MAX_BACKUPS_PER_FILE = 5

class BackupManager:
    """Manages config backups with rotation (max N per file)."""

    @staticmethod
    def backup(file_path: Path) -> str | None:
        """Create a timestamped backup. Rotates old backups."""
        file_path = resolve_external_path(file_path, base_dir=ROOT_DIR, must_exist=True, must_be_file=True)
        if not file_path.exists():
            return None

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        # Use a flat name: component__filename to avoid directory nesting
        try:
            relative = file_path.relative_to(ROOT_DIR)
            raw_name = str(relative)
        except ValueError:
            raw_name = str(file_path)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "__", raw_name).strip("._")
        if not safe_name:
            safe_name = "config"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_name = f"{safe_name}.{ts}.bak"
        bak_path = ensure_within(BACKUP_DIR, BACKUP_DIR / bak_name)

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


class ConfigInitializer:
    """Generates config files from templates and applies wizard form data."""

    @staticmethod
    def get_status() -> list[dict[str, Any]]:
        """Return the status of each config file (exists/missing)."""
        return EnvironmentChecker.check_configs()

    @staticmethod
    def get_defaults() -> dict[str, Any]:
        """Read template config files and return default values for the wizard form."""
        result: dict[str, Any] = {
            "core": {"qq_account": "", "nickname": "NachoBot"},
            "providers": [],
            "models": [],
            "model_groups": {},
            "tts": {"engine": "Vox"},
            "universalvc": {
                "target_process_name": "VRChat.exe",
                "output_device": "",
                "denoise_enabled": False,
                "speaker_enabled": True,
            },
            "env": {"host": "127.0.0.1", "port": "8000"},
        }

        # ── bot_config template ──
        bot_tmpl = ROOT_DIR / "NachoBot/template/bot_config_template.toml"
        if bot_tmpl.exists():
            try:
                doc = tomlkit.parse(bot_tmpl.read_text(encoding="utf-8"))
                bot = doc.get("bot", {})
                result["core"]["qq_account"] = str(bot.get("qq_account", ""))
                result["core"]["nickname"] = str(bot.get("nickname", "NachoBot"))
            except Exception:
                pass

        # ── model_config template — providers & models ──
        model_tmpl = ROOT_DIR / "NachoBot/template/model_config_template.toml"
        if model_tmpl.exists():
            try:
                doc = tomlkit.parse(model_tmpl.read_text(encoding="utf-8"))
                for p in doc.get("api_providers", []):
                    result["providers"].append({
                        "name": str(p.get("name", "")),
                        "base_url": str(p.get("base_url", "")),
                        "api_key": str(p.get("api_key", "")),
                    })
                for m in doc.get("models", []):
                    result["models"].append({
                        "model_identifier": str(m.get("model_identifier", "")),
                        "model_name": str(m.get("name", "")),
                        "api_provider": str(m.get("api_provider", "")),
                    })
                # Extract per-group model assignments from model_task_config
                mtc = doc.get("model_task_config", {})
                for group_name in ("replyer0", "planner", "utils", "utils_small", "tool_use"):
                    if group_name in mtc:
                        ml = mtc[group_name].get("model_list", [])
                        result["model_groups"][group_name] = ", ".join(str(x) for x in ml)
            except Exception:
                pass

        # ── .env template ──
        env_tmpl = ROOT_DIR / "NachoBot/template/template.env"
        if env_tmpl.exists():
            try:
                for line in env_tmpl.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("HOST="):
                        result["env"]["host"] = line.split("=", 1)[1]
                    elif line.startswith("PORT="):
                        result["env"]["port"] = line.split("=", 1)[1]
            except Exception:
                pass

        # ── TTS base template ──
        tts_tmpl = ROOT_DIR / "NachoBot-Multimodal-Adapter/template_configs/base_template.toml"
        if tts_tmpl.exists():
            try:
                doc = tomlkit.parse(tts_tmpl.read_text(encoding="utf-8"))
                enabled = doc.get("enabled_tts", {}).get("enabled", [])
                if enabled:
                    result["tts"]["engine"] = str(enabled[0])
            except Exception:
                pass

        # ── UniversalVC template ──
        uvc_tmpl = ROOT_DIR / "NachoBot-UniversalVC-Adapter/template/config_template.toml"
        if uvc_tmpl.exists():
            try:
                doc = tomlkit.parse(uvc_tmpl.read_text(encoding="utf-8"))
                result["universalvc"]["target_process_name"] = str(
                    doc.get("capture", {}).get("target_process_name", "")
                )
                result["universalvc"]["output_device"] = str(
                    doc.get("output", {}).get("device_name", "")
                )
                result["universalvc"]["denoise_enabled"] = bool(
                    doc.get("denoise", {}).get("enabled", False)
                )
                result["universalvc"]["speaker_enabled"] = bool(
                    doc.get("speaker", {}).get("enabled", True)
                )
            except Exception:
                pass

        return result

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
            tmpl_path = resolve_relative_to_root(ROOT_DIR, tmpl_rel)
            target_path = resolve_relative_to_root(ROOT_DIR, target_rel)

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

            config_path = resolve_relative_to_root(ROOT_DIR, rel_path)
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
            "NachoBot-Multimodal-Adapter": "tts",
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
        if "NachoBot-Multimodal-Adapter" in target_rel and "base" in filename:
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
        try:
            napcat_root = resolve_external_path(napcat_dir, base_dir=ROOT_DIR, must_exist=True, must_be_dir=True)
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return []
        config_dir = ensure_within(napcat_root, napcat_root / "config")
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

        try:
            napcat_root = resolve_external_path(napcat_dir, base_dir=ROOT_DIR, must_exist=True, must_be_dir=True)
        except (FileNotFoundError, NotADirectoryError, ValueError) as e:
            return {"configured": [], "skipped": [], "errors": [f"NapCat 目录无效: {e}"]}

        config_dir = ensure_within(napcat_root, napcat_root / "config")
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
            account = qq_account.strip()
            if not re.fullmatch(r"\d{5,20}", account):
                return {"configured": [], "skipped": [], "errors": ["QQ 账号格式无效"]}
            target = ensure_within(config_dir, config_dir / f"onebot11_{account}.json")
            target_files.append(target)
        else:
            # Auto-detect: scan for existing onebot11_*.json files
            import re

            pattern = re.compile(r"^onebot11_\d+\.json$")
            for f in config_dir.iterdir():
                if pattern.match(f.name):
                    target_files.append(ensure_within(config_dir, f))

            # Fallback: create default onebot11.json if nothing found
            if not target_files:
                target_files.append(ensure_within(config_dir, config_dir / "onebot11.json"))

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

        target_path = ensure_within(target_path.parent, target_path)
        if not re.fullmatch(r"onebot11(?:_\d{5,20})?\.json", target_path.name):
            raise ValueError(f"非法 NapCat 配置文件名: {target_path.name}")
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


class DependencyInstaller:
    """Installs project dependencies via uv sync / npm install."""

    # Projects that need uv sync, mapped by component ID
    UV_PROJECTS: dict[str, str] = {
        "core": "NachoBot",
        "qq": "NachoBot-Napcat-Adapter",
        "tts": "NachoBot-Multimodal-Adapter",
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
                    "name": "Multimodal Adapter",
                    "dir": "NachoBot-Multimodal-Adapter",
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
        try:
            project_dir = DependencyInstaller._resolve_task_project(task)
        except (KeyError, ValueError) as e:
            return {"status": "error", "message": str(e)}
        if not project_dir.exists():
            return {"status": "error", "message": f"目录不存在: {project_dir}"}

        if task["type"] == "uv":
            return await DependencyInstaller._run_uv_sync(project_dir, callback)
        elif task["type"] == "npm":
            return await DependencyInstaller._run_npm_install(project_dir, callback)
        else:
            return {"status": "error", "message": f"未知安装类型: {task['type']}"}

    @staticmethod
    def _resolve_task_project(task: dict[str, str]) -> Path:
        task_id = str(task.get("id", "")).strip()
        task_type = str(task.get("type", "")).strip()
        requested_dir = str(task.get("dir", "")).strip()

        if task_type == "uv":
            expected_dir = DependencyInstaller.UV_PROJECTS.get(task_id)
        elif task_type == "npm":
            expected_dir = DependencyInstaller.NPM_PROJECTS.get(task_id)
        else:
            raise ValueError(f"未知安装类型: {task_type}")

        if not expected_dir or requested_dir != expected_dir:
            raise ValueError(f"安装任务无效: {task_id}")

        return resolve_relative_to_root(ROOT_DIR, expected_dir)

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
