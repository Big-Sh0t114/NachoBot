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
                # The setup wizard regenerates selected configs from templates. For
                # the NapCat adapter, keep the user's existing inbound WS contract:
                # NapCat's websocketClient must use the same host/port/token.
                preserved_napcat_server: dict[str, Any] | None = None
                if (
                    target_rel == "NachoBot-Napcat-Adapter/config.toml"
                    and target_path.exists()
                ):
                    try:
                        existing_doc = tomlkit.parse(target_path.read_text(encoding="utf-8"))
                        existing_server = existing_doc.get("napcat_server")
                        if existing_server is not None:
                            preserved_napcat_server = {
                                key: existing_server.get(key)
                                for key in ("host", "port", "token", "heartbeat_interval")
                                if key in existing_server
                            }
                    except Exception as e:
                        raise ValueError(
                            f"现有 NapCat Adapter 配置无法解析，已拒绝用模板覆盖: {e}"
                        ) from e

                # Backup existing file
                if target_path.exists():
                    bak = BackupManager.backup(target_path)
                    if bak:
                        backups.append(bak)

                # Ensure target directory exists
                target_path.parent.mkdir(parents=True, exist_ok=True)

                # Copy template to target
                shutil.copy2(tmpl_path, target_path)

                # Restore the existing NapCat connection/authentication contract.
                # The template intentionally contains generic defaults (including
                # an empty token), which must not erase a working local setup.
                if preserved_napcat_server:
                    generated_doc = tomlkit.parse(target_path.read_text(encoding="utf-8"))
                    generated_server = generated_doc.get("napcat_server")
                    if generated_server is None:
                        raise ValueError("NapCat Adapter 模板缺少 [napcat_server]")
                    for key, value in preserved_napcat_server.items():
                        generated_server[key] = value
                    target_path.write_text(tomlkit.dumps(generated_doc), encoding="utf-8")

                # Apply wizard data overrides
                override_err = ConfigInitializer._apply_overrides(
                    target_path, target_rel, wizard_data, tts_enabled
                )
                if override_err:
                    errors.append(f"覆写失败 {target_rel}: {override_err}")

                generated.append(target_rel)
            except Exception as e:
                errors.append(f"{target_rel}: {e}")

        # Post-generation: patch TTS chain routing in ALL existing adapter configs.
        # This covers adapters that were not generated from templates (e.g. Koishi, Bilibili).
        # When TTS is not selected, keep the user's existing core port untouched.
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
        - nachobot_server.port → 8070 only when TTS is enabled
        - voice.use_tts → true/false
        When TTS is disabled, do not rewrite nachobot_server.port. The adapter
        should keep the core endpoint already configured by the user.
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

                if tts_enabled and "nachobot_server" in doc:
                    if doc["nachobot_server"].get("port") != 8070:
                        doc["nachobot_server"]["port"] = 8070
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
            if tts_enabled and "nachobot_server" in doc:
                if doc["nachobot_server"].get("port") != 8070:
                    doc["nachobot_server"]["port"] = 8070
                    changed = True
            if "voice" in doc:
                if doc["voice"].get("use_tts") != tts_enabled:
                    doc["voice"]["use_tts"] = tts_enabled
                    changed = True

        # -- Koishi adapter config.toml --
        if "NachoBot-Koishi-Adapter" in target_rel and filename == "config.toml":
            if tts_enabled and "nachobot_server" in doc:
                if doc["nachobot_server"].get("port") != 8070:
                    doc["nachobot_server"]["port"] = 8070
                    changed = True
            if "voice" in doc:
                if doc["voice"].get("use_tts") != tts_enabled:
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
# Setup deployment helpers


class NapCatConfigurator:
    """
    Automatically configure NapCat Shell's onebot11 config files.
    Adds WebSocket client (NachoBot), diary HTTP server, and bilibili video HTTP server.
    """

    # Standard WebSocket client defaults for NachoBot. The actual host/port/token
    # are synchronized from NachoBot-Napcat-Adapter/config.toml at deploy time.
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

    @staticmethod
    def _load_adapter_ws_entry() -> dict[str, Any]:
        """Build the desired NapCat WS client entry from adapter config.toml."""
        entry = dict(NapCatConfigurator._WS_CLIENT_ENTRY)
        adapter_config = resolve_relative_to_root(
            ROOT_DIR, "NachoBot-Napcat-Adapter/config.toml"
        )
        if not adapter_config.exists():
            return entry

        try:
            doc = tomlkit.parse(adapter_config.read_text(encoding="utf-8"))
            server = doc.get("napcat_server", {})
            host = str(server.get("host", "localhost") or "localhost").strip()
            port = int(server.get("port", 8095))
            token = str(server.get("token", "") or "")
        except Exception as e:
            raise ValueError(f"读取 NapCat Adapter 配置失败: {e}") from e

        # NapCat and the adapter normally run on the same machine. 0.0.0.0 is a
        # listen address, not a valid client destination, so connect via localhost.
        client_host = "localhost" if host in {"0.0.0.0", "::", "[::]"} else host
        if ":" in client_host and not client_host.startswith("["):
            client_host = f"[{client_host}]"
        entry["url"] = f"ws://{client_host}:{port}"
        entry["token"] = token
        return entry

    @staticmethod
    def _reconcile_entry(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
        """Update an existing NapCat network entry to the desired values."""
        changed = False
        for key, value in desired.items():
            if existing.get(key) != value:
                existing[key] = value
                changed = True
        return changed

    # HTTP server defaults. Actual ports/tokens are synchronized from the
    # corresponding Core plugin configs so WebUI cannot drift from runtime config.
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

    _BILIBILI_HTTP_ENTRY = {
        "enable": True,
        "name": "BiliBili",
        "host": "127.0.0.1",
        "port": 5700,
        "enableCors": False,
        "enableWebsocket": False,
        "messagePostFormat": "array",
        "token": "",
        "debug": False,
    }

    @staticmethod
    def _load_diary_http_entry() -> dict[str, Any]:
        """Build the NapCat HTTP server required by diary_plugin."""
        entry = dict(NapCatConfigurator._DIARY_HTTP_ENTRY)
        config_path = resolve_relative_to_root(
            ROOT_DIR, "NachoBot/plugins/diary_plugin/config.toml"
        )
        if not config_path.exists():
            return entry

        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
            publishing = doc.get("qzone_publishing", {})
            entry["port"] = int(publishing.get("napcat_port", 9997))
            entry["token"] = str(publishing.get("napcat_token", "") or "")
        except Exception as e:
            raise ValueError(f"读取 Diary 插件 NapCat 配置失败: {e}") from e

        # qzone_publishing.napcat_host is the client's destination host, not the
        # address NapCat itself should bind to, so the server bind stays local.
        return entry

    @staticmethod
    def _load_bilibili_http_entry() -> dict[str, Any]:
        """Build the NapCat HTTP server required by bilibili_video_sender_plugin."""
        entry = dict(NapCatConfigurator._BILIBILI_HTTP_ENTRY)
        config_path = resolve_relative_to_root(
            ROOT_DIR, "NachoBot/plugins/bilibili_video_sender_plugin/config.toml"
        )
        if not config_path.exists():
            return entry

        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
            api = doc.get("api", {})
            entry["port"] = int(api.get("port", 5700))
        except Exception as e:
            raise ValueError(f"读取 Bilibili 插件 NapCat 配置失败: {e}") from e

        # The plugin posts directly to http://localhost:<api.port> without an
        # Authorization header, therefore this managed NapCat endpoint must not
        # require a token.
        entry["token"] = ""
        return entry

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

        Adds/reconciles:
          - WebSocket client from NachoBot-Napcat-Adapter/config.toml
          - Diary HTTP server from diary_plugin/config.toml
          - Bilibili HTTP server from bilibili_video_sender_plugin/config.toml

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
        account_pattern = re.compile(r"^onebot11_(\d+)\.json$")
        existing_accounts: dict[str, Path] = {}
        for f in config_dir.iterdir():
            match = account_pattern.match(f.name)
            if match:
                existing_accounts[match.group(1)] = ensure_within(config_dir, f)

        if qq_account and qq_account.strip():
            # Specific QQ account. If NapCat already has account-specific configs,
            # do not silently create a different account file: that usually means
            # the wizard QQ number and the currently logged-in NapCat account differ.
            account = qq_account.strip()
            if not re.fullmatch(r"\d{5,20}", account):
                return {"configured": [], "skipped": [], "errors": ["QQ 账号格式无效"]}
            target = ensure_within(config_dir, config_dir / f"onebot11_{account}.json")
            if existing_accounts and account not in existing_accounts and not target.exists():
                detected = ", ".join(sorted(existing_accounts))
                return {
                    "configured": [],
                    "skipped": [],
                    "errors": [
                        f"NapCat 当前已有账号配置 {detected}，与向导 QQ {account} 不匹配；"
                        "请确认 NapCat 当前登录账号后重试"
                    ],
                }
            target_files.append(target)
        else:
            # Auto-detect is safe only when exactly one account-specific config
            # exists. Each OneBot account owns its own HTTP listeners, so writing
            # the same Diary/Bilibili ports into multiple account configs would
            # create bind conflicts inside the same NapCat process.
            if len(existing_accounts) == 1:
                target_files.extend(existing_accounts.values())
            elif len(existing_accounts) > 1:
                detected = ", ".join(sorted(existing_accounts))
                return {
                    "configured": [],
                    "skipped": [],
                    "errors": [
                        f"检测到多个 NapCat 账号配置 {detected}；"
                        "请在向导中明确填写要配置的 QQ 账号"
                    ],
                }
            else:
                # Fallback: create default onebot11.json if nothing found.
                target_files.append(ensure_within(config_dir, config_dir / "onebot11.json"))

        try:
            desired_ws_entry = NapCatConfigurator._load_adapter_ws_entry()
            desired_diary_http_entry = NapCatConfigurator._load_diary_http_entry()
            desired_bilibili_http_entry = NapCatConfigurator._load_bilibili_http_entry()
        except ValueError as e:
            return {"configured": [], "skipped": [], "errors": [str(e)]}

        for target_path in target_files:
            try:
                result = NapCatConfigurator._configure_file(
                    target_path,
                    desired_ws_entry,
                    desired_diary_http_entry,
                    desired_bilibili_http_entry,
                )
                if result["changed"]:
                    configured.append(str(target_path.name))
                else:
                    skipped.append(str(target_path.name))
            except Exception as e:
                errors.append(f"{target_path.name}: {e}")

        return {"configured": configured, "skipped": skipped, "errors": errors}

    @staticmethod
    def _configure_file(
        target_path: Path,
        desired_ws_entry: dict[str, Any],
        desired_diary_http_entry: dict[str, Any],
        desired_bilibili_http_entry: dict[str, Any],
    ) -> dict[str, bool]:
        """
        Configure a single onebot11 JSON file.
        Creates it from scratch if it doesn't exist.
        Existing managed entries are reconciled instead of merely detected.
        Returns {"changed": bool}.
        """
        import json as _json

        target_path = ensure_within(target_path.parent, target_path)
        if not re.fullmatch(r"onebot11(?:_\d{5,20})?\.json", target_path.name):
            raise ValueError(f"非法 NapCat 配置文件名: {target_path.name}")
        # codeql[py/path-injection]
        if target_path.exists():
            # codeql[py/path-injection]
            raw = target_path.read_text(encoding="utf-8")
            try:
                doc = _json.loads(raw)
            except _json.JSONDecodeError as e:
                raise ValueError(
                    f"现有配置 JSON 损坏，已拒绝覆盖: line {e.lineno}, column {e.colno}: {e.msg}"
                ) from e
            if not isinstance(doc, dict):
                raise ValueError("现有 NapCat 配置顶层必须是 JSON 对象，已拒绝覆盖")
        else:
            doc = {}

        changed = False

        # Ensure top-level structure, but reject incompatible existing types.
        if "network" not in doc:
            doc["network"] = {}
            changed = True
        elif not isinstance(doc["network"], dict):
            raise ValueError("network 字段必须是 JSON 对象，已拒绝覆盖")
        network = doc["network"]

        # --- WebSocket Clients ---
        if "websocketClients" not in network:
            network["websocketClients"] = []
            changed = True
        elif not isinstance(network["websocketClients"], list):
            raise ValueError("network.websocketClients 必须是数组，已拒绝覆盖")
        ws_clients = network["websocketClients"]
        if any(not isinstance(c, dict) for c in ws_clients):
            raise ValueError("network.websocketClients 包含非对象条目，已拒绝覆盖")

        # Prefer the entry explicitly named NachoBot; for backward compatibility,
        # also recognize the old fixed localhost:8095 entry.
        nachobot_ws = next(
            (
                c
                for c in ws_clients
                if c.get("name") == "NachoBot"
                or c.get("url") == "ws://localhost:8095"
            ),
            None,
        )
        if nachobot_ws is None:
            ws_clients.append(dict(desired_ws_entry))
            changed = True
        elif NapCatConfigurator._reconcile_entry(nachobot_ws, desired_ws_entry):
            changed = True

        # --- HTTP Servers ---
        if "httpServers" not in network:
            network["httpServers"] = []
            changed = True
        elif not isinstance(network["httpServers"], list):
            raise ValueError("network.httpServers 必须是数组，已拒绝覆盖")
        http_servers = network["httpServers"]
        if any(not isinstance(s, dict) for s in http_servers):
            raise ValueError("network.httpServers 包含非对象条目，已拒绝覆盖")

        # Manage HTTP endpoints by their configured target port only. Do not use
        # names as a fallback: another bot/account may legitimately have its own
        # QZone/Diary/BiliBili entry on a different port.
        diary_port = desired_diary_http_entry["port"]
        diary = next((s for s in http_servers if s.get("port") == diary_port), None)
        if diary is None:
            http_servers.append(dict(desired_diary_http_entry))
            changed = True
        elif str(diary.get("name", "")).lower() != "diary":
            raise ValueError(
                f"NapCat HTTP 端口 {diary_port} 已被条目 {diary.get('name', '<unnamed>')} 占用"
            )
        elif NapCatConfigurator._reconcile_entry(diary, desired_diary_http_entry):
            changed = True

        bilibili_port = desired_bilibili_http_entry["port"]
        bilibili = next((s for s in http_servers if s.get("port") == bilibili_port), None)
        if bilibili is None:
            http_servers.append(dict(desired_bilibili_http_entry))
            changed = True
        elif str(bilibili.get("name", "")).lower() not in {"bilibili", "bili bili"}:
            raise ValueError(
                f"NapCat HTTP 端口 {bilibili_port} 已被条目 {bilibili.get('name', '<unnamed>')} 占用"
            )
        elif NapCatConfigurator._reconcile_entry(bilibili, desired_bilibili_http_entry):
            changed = True

        # Ensure other standard arrays exist.
        for key in ["httpSseServers", "httpClients", "websocketServers", "plugins"]:
            if key not in network:
                network[key] = []
                changed = True
            elif not isinstance(network[key], list):
                raise ValueError(f"network.{key} 必须是数组，已拒绝覆盖")

        # Ensure top-level defaults and make sure additions are persisted.
        for key, value in {
            "musicSignUrl": "",
            "enableLocalFile2Url": False,
            "parseMultMsg": False,
        }.items():
            if key not in doc:
                doc[key] = value
                changed = True

        if changed:
            # Backup existing file before writing
            # codeql[py/path-injection]
            if target_path.exists():
                BackupManager.backup(target_path)
            # codeql[py/path-injection]
            target_path.write_text(
                _json.dumps(doc, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        return {"changed": changed}


# =========================================================================
# Dependency Installer


class DependencyInstaller:
    """Installs locked project dependencies and Core browser assets."""

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

    # Projects that use the repository-pinned Yarn release.
    YARN_PROJECTS: dict[str, str] = {
        "discord_koishi_yarn": "koishi-app",
    }

    PLAYWRIGHT_PROJECTS: dict[str, str] = {
        "core_playwright": "NachoBot",
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
        tasks.append(
            {
                "id": "core_playwright",
                "type": "playwright",
                "name": "Playwright Chromium",
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
                    "id": "discord_koishi_yarn",
                    "type": "yarn",
                    "name": "Koishi App (Yarn)",
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
        Run a validated uv, Yarn, or Playwright installation task.
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
        elif task["type"] == "yarn":
            return await DependencyInstaller._run_yarn_install(project_dir, callback)
        elif task["type"] == "playwright":
            return await DependencyInstaller._run_playwright_install(project_dir, callback)
        else:
            return {"status": "error", "message": f"未知安装类型: {task['type']}"}

    @staticmethod
    def _resolve_task_project(task: dict[str, str]) -> Path:
        task_id = str(task.get("id", "")).strip()
        task_type = str(task.get("type", "")).strip()
        requested_dir = str(task.get("dir", "")).strip()

        if task_type == "uv":
            expected_dir = DependencyInstaller.UV_PROJECTS.get(task_id)
        elif task_type == "yarn":
            expected_dir = DependencyInstaller.YARN_PROJECTS.get(task_id)
        elif task_type == "playwright":
            expected_dir = DependencyInstaller.PLAYWRIGHT_PROJECTS.get(task_id)
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
    async def _run_playwright_install(
        project_dir: Path,
        callback: Callable[[str], Any] | None,
    ) -> dict[str, Any]:
        """Install and launch-check the Chromium revision required by Playwright."""
        import locale

        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        try:
            proc = await asyncio.create_subprocess_exec(
                "uv",
                "run",
                "python",
                "scripts/ensure_playwright.py",
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
                return {"status": "ok", "message": "Playwright Chromium 已就绪"}
            return {
                "status": "error",
                "message": f"Playwright Chromium 准备失败，退出码: {proc.returncode}",
            }
        except FileNotFoundError:
            return {"status": "error", "message": "uv 未安装，请先安装 uv"}
        except Exception as e:
            return {"status": "error", "message": f"Playwright Chromium 准备出错: {e}"}

    @staticmethod
    async def _run_yarn_install(
        project_dir: Path,
        callback: Callable[[str], Any] | None,
    ) -> dict[str, Any]:
        """Execute the repository-pinned immutable Yarn install."""
        import locale

        env = os.environ.copy()

        try:
            command = (
                ["cmd", "/c", "corepack", "yarn", "install", "--immutable"]
                if os.name == "nt"
                else ["corepack", "yarn", "install", "--immutable"]
            )
            proc = await asyncio.create_subprocess_exec(
                *command,
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
                return {"status": "ok", "message": "yarn install --immutable 完成"}
            else:
                return {
                    "status": "error",
                    "message": f"yarn install --immutable 退出码: {proc.returncode}",
                }
        except FileNotFoundError:
            return {"status": "error", "message": "Corepack/Yarn 未安装"}
        except Exception as e:
            return {"status": "error", "message": f"安装出错: {e}"}
