"""
NachoBot WebUI — Configuration Manager
Handles TOML config file reading, writing, and backup with comment preservation.
"""

import base64
import hashlib
import hmac
import json
import secrets
import shutil
from copy import deepcopy
from collections.abc import Mapping, MutableMapping, MutableSequence
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tomlkit

try:
    from .secure_paths import ensure_within, resolve_named_file, resolve_relative_to_root
except ImportError:
    from secure_paths import ensure_within, resolve_named_file, resolve_relative_to_root

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
    # Temporarily hidden from WebUI; restore when VRChat config exposure is wanted.
    # {"id": "vrchat_config",       "group": "VRChat 适配器",     "path": "NachoBot-VRChat-Adapter/config.toml",       "label": "VRChat 适配器配置"},
    # WebUI 配置
    {"id": "webui_config",        "group": "WebUI 配置",       "path": "webUI/webui_config.toml",                   "label": "WebUI 系统配置"},
]

# Field names that should be masked in the UI
SENSITIVE_FIELDS = {
    "api_key", "token", "sessdata", "bili_jct", "buvid3", "buvid4",
    "auth_token", "access_token", "refresh_token", "client_secret",
    "password", "passwd", "cookie", "cookies", "authorization",
    "credential", "credentials", "cert_file", "key_file",
}
SECRET_PLACEHOLDER = "__NACHOBOT_KEEP_EXISTING_SECRET__:"
_SECRET_PLACEHOLDER_KEY = secrets.token_bytes(32)


def _is_sensitive(key: str) -> bool:
    """Check whether a key name is considered sensitive."""
    k = key.lower()
    return (
        k in SENSITIVE_FIELDS
        or k.endswith("_token")
        or "key" in k
        or "secret" in k
        or "password" in k
        or "credential" in k
    )


def _mask_secret_value(value: Any) -> Any:
    """Preserve a secret value's container shape while hiding scalar values."""
    if isinstance(value, Mapping):
        return {key: _mask_secret_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, MutableSequence)):
        return [_mask_secret_value(item) for item in value]
    if value in (None, ""):
        return value
    return SECRET_PLACEHOLDER


def _make_secret_placeholder(path: tuple[str | int, ...], value: Any) -> str:
    path_payload = json.dumps(path, ensure_ascii=False, separators=(",", ":"))
    payload = f"{path_payload}\0{type(value).__name__}\0{value}".encode()
    digest = hmac.new(_SECRET_PLACEHOLDER_KEY, payload, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return SECRET_PLACEHOLDER + token


def _tokenize_secret_value(value: Any, path: tuple[str | int, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _tokenize_secret_value(item, path + (str(key),))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, MutableSequence)):
        return [
            _tokenize_secret_value(item, path + (index,))
            for index, item in enumerate(value)
        ]
    if value in (None, ""):
        return value
    return _make_secret_placeholder(path, value)


def _redact_toml_node(node: Any, path: tuple[str | int, ...] = ()) -> None:
    if isinstance(node, MutableMapping):
        for key in list(node.keys()):
            value = node[key]
            child_path = path + (str(key),)
            if _is_sensitive(str(key)):
                node[key] = _tokenize_secret_value(value, child_path)
            else:
                _redact_toml_node(value, child_path)
    elif isinstance(node, MutableSequence):
        for index, item in enumerate(node):
            _redact_toml_node(item, path + (index,))


def sanitize_toml_for_edit(raw: str) -> str:
    """Return editable TOML with every recognized secret replaced by a sentinel."""
    doc = tomlkit.parse(raw)
    _redact_toml_node(doc)
    return tomlkit.dumps(doc)


def _collect_secret_tokens(
    node: Any,
    tokens: dict[str, Any],
    path: tuple[str | int, ...] = (),
) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            child_path = path + (str(key),)
            if _is_sensitive(str(key)):
                _collect_secret_value_tokens(value, tokens, child_path)
            else:
                _collect_secret_tokens(value, tokens, child_path)
    elif isinstance(node, (list, tuple, MutableSequence)):
        for index, value in enumerate(node):
            _collect_secret_tokens(value, tokens, path + (index,))


def _collect_secret_value_tokens(
    value: Any,
    tokens: dict[str, Any],
    path: tuple[str | int, ...],
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _collect_secret_value_tokens(item, tokens, path + (str(key),))
    elif isinstance(value, (list, tuple, MutableSequence)):
        for index, item in enumerate(value):
            _collect_secret_value_tokens(item, tokens, path + (index,))
    elif value not in (None, ""):
        tokens[_make_secret_placeholder(path, value)] = deepcopy(value)


def _restore_secret_placeholders(
    node: Any,
    tokens: Mapping[str, Any],
    *,
    in_sensitive_field: bool = False,
) -> Any:
    if isinstance(node, MutableMapping):
        for key in list(node.keys()):
            node[key] = _restore_secret_placeholders(
                node[key],
                tokens,
                in_sensitive_field=in_sensitive_field or _is_sensitive(str(key)),
            )
        return node
    if isinstance(node, MutableSequence):
        for index, value in enumerate(list(node)):
            node[index] = _restore_secret_placeholders(
                value,
                tokens,
                in_sensitive_field=in_sensitive_field,
            )
        return node
    if isinstance(node, str) and node.startswith(SECRET_PLACEHOLDER):
        if not in_sensitive_field:
            raise ValueError("秘密占位符只能用于秘密字段")
        if node not in tokens:
            raise ValueError("秘密占位符已失效；请重新加载配置后再保存")
        return deepcopy(tokens[node])
    return node


def merge_toml_secrets(raw: str, existing_raw: str) -> str:
    """Resolve secret sentinels against the current file before persisting."""
    incoming = tomlkit.parse(raw)
    existing = tomlkit.parse(existing_raw) if existing_raw else tomlkit.document()
    tokens: dict[str, Any] = {}
    _collect_secret_tokens(existing, tokens)
    _restore_secret_placeholders(incoming, tokens)
    return tomlkit.dumps(incoming)


def _sanitize_env_for_edit(raw: str) -> str:
    lines: list[str] = []
    for line in raw.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        prefix, separator, value = content.partition("=")
        key = prefix.strip()
        if separator and _is_sensitive(key) and value.strip():
            content = f"{prefix}={_make_secret_placeholder(('env', key), value)}"
        lines.append(content + ending)
    return "".join(lines)


def _merge_env_secrets(raw: str, existing_raw: str) -> str:
    existing_tokens: dict[str, str] = {}
    for line in existing_raw.splitlines():
        prefix, separator, value = line.partition("=")
        key = prefix.strip()
        if separator and _is_sensitive(key) and value.strip():
            existing_tokens[_make_secret_placeholder(("env", key), value)] = value

    lines: list[str] = []
    for line in raw.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        prefix, separator, value = content.partition("=")
        key = prefix.strip()
        candidate = value.strip()
        if separator and candidate.startswith(SECRET_PLACEHOLDER):
            if not _is_sensitive(key):
                raise ValueError("秘密占位符只能用于秘密字段")
            if candidate not in existing_tokens:
                raise ValueError(f"秘密占位符 {key} 已失效；请重新加载配置后再保存")
            content = f"{prefix}={existing_tokens[candidate]}"
        lines.append(content + ending)
    return "".join(lines)


class ConfigManager:
    """Read / write TOML configuration files for NachoBot."""

    def __init__(self, root_dir: Path | None = None):
        self.root = root_dir or ROOT_DIR

    @property
    def secret_placeholder(self) -> str:
        return SECRET_PLACEHOLDER

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
            data = self._read_env(full)
            if mask_sensitive:
                self._mask_dict(data)
            return data

        raw = full.read_text(encoding="utf-8")
        doc = tomlkit.parse(raw)
        data = self._tomlkit_to_dict(doc)

        if mask_sensitive:
            self._mask_dict(data)

        return data

    def read_config_raw(self, file_id: str) -> str:
        """Read editable config text without disclosing existing secrets."""
        entry = self._find(file_id)
        full = self._entry_path(entry)
        raw = full.read_text(encoding="utf-8")
        if full.name == ".env":
            return _sanitize_env_for_edit(raw)
        try:
            return sanitize_toml_for_edit(raw)
        except Exception as exc:
            raise ValueError(
                "配置语法错误，无法在不泄露秘密的前提下显示原始内容；"
                "请恢复有效备份或在本机编辑文件"
            ) from exc

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

    def write_config_raw(
        self,
        file_id: str,
        raw: str,
        *,
        validator: Callable[[str], None] | None = None,
    ) -> None:
        """Write editable text, resolving secret sentinels against current data."""
        entry = self._find(file_id)
        full = self._entry_path(entry)
        existing_raw = full.read_text(encoding="utf-8") if full.exists() else ""
        if full.name == ".env":
            raw = _merge_env_secrets(raw, existing_raw)
        else:
            raw = merge_toml_secrets(raw, existing_raw)
        if validator is not None:
            validator(raw)
        self._backup(full, backup_type="auto")
        full.write_text(raw, encoding="utf-8")

    # ---- backup ----

    def backup_config(self, file_id: str) -> str:
        """Create a timestamped backup. Returns backup path."""
        entry = self._find(file_id)
        full = self._entry_path(entry)
        return self._backup(full, backup_type="manual")

    def restore_backup(
        self,
        file_id: str,
        backup_filename: str,
        *,
        validator: Callable[[str], None] | None = None,
    ) -> str:
        """Restore a specific backup file."""
        entry = self._find(file_id)
        full = self._entry_path(entry)
        
        directory = ensure_within(self.root, full.parent, must_exist=True)
        target_backup = resolve_named_file(
            directory,
            backup_filename,
            suffix=".bak",
            must_exist=True,
        )
        expected_prefix = f"{full.stem}."
        if not target_backup.name.startswith(expected_prefix) or not target_backup.is_file():
            raise ValueError("备份文件不属于当前配置")

        backup_raw = target_backup.read_text(encoding="utf-8")
        if full.name != ".env":
            try:
                tomlkit.parse(backup_raw)
            except Exception as exc:
                raise ValueError("备份配置包含无效 TOML，恢复已拒绝") from exc
        if validator is not None:
            validator(backup_raw)
            
        self._backup(full, backup_type="auto")
        
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
            try:
                b = ensure_within(directory, b, must_exist=True)
            except (ValueError, FileNotFoundError):
                continue
            if not b.is_file():
                continue
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
            if _is_sensitive(k):
                d[k] = _mask_secret_value(v)
            elif isinstance(v, dict):
                cls._mask_dict(v, k)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        cls._mask_dict(item, k)
