"""
NachoBot WebUI — Configuration Manager
Handles TOML config file reading, writing, and backup with comment preservation.
"""

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import tomlkit

try:
    from .secure_paths import ensure_within, resolve_relative_to_root
except ImportError:
    from secure_paths import ensure_within, resolve_relative_to_root

# Root of the Nacho-with-u project (parent of webui/)
ROOT_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Config file registry
# ---------------------------------------------------------------------------

CONFIG_REGISTRY: list[dict[str, str]] = [
    # NachoBot 核心
    {"id": "bot_config",    "group": "NachoBot 核心",  "path": "NachoBot/config/bot_config.toml",           "label": "机器人主配置"},
    {"id": "model_config",  "group": "NachoBot 核心",  "path": "NachoBot/config/model_config.toml",         "label": "模型与 API 配置"},
    {"id": "topics_config", "group": "NachoBot 核心",  "path": "NachoBot/config/topics_config.toml",        "label": "话题系统配置"},
    {"id": "env",           "group": "NachoBot 核心",  "path": "NachoBot/.env",                             "label": "环境变量"},
    # 多模态适配器
    {"id": "tts_base",      "group": "多模态适配器", "path": "NachoBot-Multimodal-Adapter/configs/base.toml",       "label": "TTS 基础配置"},
    {"id": "tts_vox",       "group": "多模态适配器", "path": "NachoBot-Multimodal-Adapter/configs/vox.toml",        "label": "VoxCPM 配置"},
    {"id": "tts_sovits",    "group": "多模态适配器", "path": "NachoBot-Multimodal-Adapter/configs/gpt-sovits.toml", "label": "GPT-SoVITS 配置"},
    {"id": "tts_perception","group": "多模态适配器", "path": "NachoBot-Multimodal-Adapter/configs/perception.toml", "label": "VLM / ASR 配置"},
    # 各平台适配器
    {"id": "napcat_config",       "group": "Napcat 适配器",    "path": "NachoBot-Napcat-Adapter/config.toml",       "label": "Napcat 适配器配置"},
    {"id": "bilibili_config",     "group": "Bilibili 适配器",  "path": "NachoBot-Bilibili-Adapter/config.toml",     "label": "Bilibili 适配器配置"},
    {"id": "discord_config",      "group": "Discord 适配器",   "path": "NachoBot-DiscordVC-Adapter/config.toml",    "label": "Discord VC 适配器配置"},
    {"id": "koishi_config",       "group": "Discord 适配器",   "path": "NachoBot-Koishi-Adapter/config.toml",       "label": "Koishi 适配器配置"},
    {"id": "universalvc_config",  "group": "全局语音适配器",    "path": "NachoBot-UniversalVC-Adapter/config.toml",  "label": "UniversalVC 配置"},
    # WebUI 配置
    {"id": "webui_config",        "group": "WebUI 配置",       "path": "webUI/webui_config.toml",                   "label": "WebUI 系统配置"},
]

# Field names that should be masked in the UI
SENSITIVE_FIELDS = {
    "api_key", "token", "sessdata", "bili_jct", "buvid3", "buvid4",
    "auth_token", "cert_file", "key_file",
}


def _is_sensitive(key: str) -> bool:
    """Check whether a key name is considered sensitive."""
    k = key.lower()
    return k in SENSITIVE_FIELDS or "key" in k or "secret" in k


def _mask_value(value: str) -> str:
    """Mask a sensitive string, showing only the first 4 and last 4 chars."""
    s = str(value)
    if len(s) <= 12:
        return s[:3] + "****"
    return s[:4] + "****" + s[-4:]


class ConfigManager:
    """Read / write TOML configuration files for NachoBot."""

    def __init__(self, root_dir: Path | None = None):
        self.root = root_dir or ROOT_DIR

    # ---- listing ----

    def list_configs(self) -> list[dict[str, Any]]:
        """Return the registry with existence info."""
        result = []
        for entry in CONFIG_REGISTRY:
            full = self._entry_path(entry)
            result.append({
                **entry,
                "exists": full.exists(),
                "abs_path": str(full),
            })
        return result

    # ---- reading ----

    def read_config(self, file_id: str, mask_sensitive: bool = True) -> dict[str, Any]:
        """Read a config file and return as a JSON-friendly dict.

        For `.env` files, returns a simple key=value dict.
        For `.toml` files, uses tomlkit to parse.
        """
        entry = self._find(file_id)
        full = self._entry_path(entry)

        if not full.exists():
            raise FileNotFoundError(f"Config file not found: {full}")

        if full.name == ".env":
            return self._read_env(full)

        raw = full.read_text(encoding="utf-8")
        doc = tomlkit.parse(raw)
        data = self._tomlkit_to_dict(doc)

        if mask_sensitive:
            self._mask_dict(data)

        return data

    def read_config_raw(self, file_id: str) -> str:
        """Read raw text of a config file."""
        entry = self._find(file_id)
        full = self._entry_path(entry)
        return full.read_text(encoding="utf-8")

    # ---- writing ----

    def write_config(self, file_id: str, data: dict[str, Any]) -> None:
        """Write data back to a config file.

        Creates a .bak backup before writing.
        """
        entry = self._find(file_id)
        full = self._entry_path(entry)

        # Backup
        self._backup(full, backup_type="auto")

        if full.name == ".env":
            self._write_env(full, data)
            return

        # Read existing to preserve formatting, then update values
        if full.exists():
            raw = full.read_text(encoding="utf-8")
            doc = tomlkit.parse(raw)
        else:
            doc = tomlkit.document()

        self._update_tomlkit_doc(doc, data)
        full.write_text(tomlkit.dumps(doc), encoding="utf-8")

    def write_config_raw(self, file_id: str, raw: str) -> None:
        """Write raw config text to a registry-owned file."""
        entry = self._find(file_id)
        full = self._entry_path(entry)
        self._backup(full, backup_type="auto")
        full.write_text(raw, encoding="utf-8")

    # ---- backup ----

    def backup_config(self, file_id: str) -> str:
        """Create a timestamped backup. Returns backup path."""
        entry = self._find(file_id)
        full = self._entry_path(entry)
        return self._backup(full, backup_type="manual")

    def restore_backup(self, file_id: str, backup_filename: str) -> str:
        """Restore a specific backup file."""
        entry = self._find(file_id)
        full = self._entry_path(entry)
        
        directory = full.parent
        target_backup = directory / backup_filename
        
        if not target_backup.exists() or not target_backup.name.endswith(".bak"):
            raise FileNotFoundError(f"Backup file not found: {backup_filename}")
            
        self._backup(full, backup_type="auto")
        
        import shutil
        shutil.copy2(target_backup, full)
        return target_backup.name

    # ---- internals ----

    def _find(self, file_id: str) -> dict[str, str]:
        for entry in CONFIG_REGISTRY:
            if entry["id"] == file_id:
                return entry
        raise ValueError(f"Unknown config id: {file_id}")

    def _entry_path(self, entry: dict[str, str]) -> Path:
        return resolve_relative_to_root(self.root, entry["path"])

    def _backup(self, path: Path, backup_type: str = "auto") -> str:
        if not path.exists():
            return ""
            
        if path.name != ".env":
            import tomlkit
            try:
                tomlkit.parse(path.read_text(encoding="utf-8"))
            except Exception:
                # Do not backup invalid TOML files to prevent polluting the backup history
                return ""
                
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            from .secure_paths import ensure_within
        except ImportError:
            from secure_paths import ensure_within
            
        bak = ensure_within(self.root, path.with_suffix(f".{backup_type}.{ts}.bak"))
        import shutil
        shutil.copy2(path, bak)
        
        if backup_type == "auto":
            # Keep only the newest 2 auto backups
            directory = path.parent
            prefix = path.stem
            auto_backups = list(directory.glob(f"{prefix}.auto.*.bak"))
            auto_backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old_bak in auto_backups[2:]:
                try:
                    old_bak.unlink()
                except OSError:
                    pass
                    
        return str(bak)

    def list_backups(self, file_id: str) -> list[dict]:
        entry = self._find(file_id)
        full = self._entry_path(entry)
        directory = full.parent
        prefix = full.stem
        
        # Match old format (*.bak) and new format (*.auto.*.bak, *.manual.*.bak)
        backups = list(directory.glob(f"{prefix}.*.bak"))
        
        result = []
        for b in backups:
            name_parts = b.name.split('.')
            if len(name_parts) >= 4 and name_parts[-2].isdigit() and name_parts[-3] in ("auto", "manual"):
                btype = "手动备份" if name_parts[-3] == "manual" else "自动备份"
            else:
                btype = "备份"
                
            result.append({
                "filename": b.name,
                "type": btype,
                "mtime": b.stat().st_mtime
            })
            
        result.sort(key=lambda x: x["mtime"], reverse=True)
        return result

    # ---- env helpers ----

    @staticmethod
    def _read_env(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
        return result

    @staticmethod
    def _write_env(path: Path, data: dict[str, str]) -> None:
        lines = [f"{k}={v}" for k, v in data.items()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- toml helpers ----

    @staticmethod
    def _tomlkit_to_dict(obj: Any) -> Any:
        """Recursively convert tomlkit objects to plain Python dicts/lists."""
        if isinstance(obj, dict):
            return {k: ConfigManager._tomlkit_to_dict(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [ConfigManager._tomlkit_to_dict(v) for v in obj]
        if isinstance(obj, tomlkit.items.Integer):
            return int(obj)
        if isinstance(obj, tomlkit.items.Float):
            return float(obj)
        if isinstance(obj, tomlkit.items.Bool):
            return bool(obj)
        if isinstance(obj, tomlkit.items.String):
            return str(obj)
        return obj

    @staticmethod
    def _update_tomlkit_doc(doc: Any, data: dict[str, Any]) -> None:
        """Recursively update a tomlkit document with new values."""
        for key, value in data.items():
            if isinstance(value, dict) and key in doc and isinstance(doc[key], dict):
                ConfigManager._update_tomlkit_doc(doc[key], value)
            else:
                doc[key] = value

    @classmethod
    def _mask_dict(cls, d: dict[str, Any], _parent_key: str = "") -> None:
        """In-place mask sensitive values."""
        for k, v in d.items():
            if isinstance(v, dict):
                cls._mask_dict(v, k)
            elif isinstance(v, str) and _is_sensitive(k):
                d[k] = _mask_value(v)
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    if isinstance(item, dict):
                        cls._mask_dict(item, k)
                    elif isinstance(item, str) and _is_sensitive(k):
                        v[i] = _mask_value(item)
