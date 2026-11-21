"""Storage and validation for WebUI-managed external HTTP TTS profiles.

The module intentionally imports the multimodal adapter only when a profile is
validated or previewed. Importing WebUI itself must stay independent of GPU
packages installed in the adapter runtime.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import tomlkit


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = ROOT_DIR / "NachoBot-Multimodal-Adapter" / "configs"
MAX_PROFILE_BYTES = 64 * 1024
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_PREVIEW_TEXT_CHARS = 512
MAX_PREVIEW_AUDIO_BYTES = 16 * 1024 * 1024
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SECRET_NAME_PARTS = {
    "auth", "authorization", "credential", "credentials", "key", "password",
    "passwd", "secret", "token",
}


class TTSProfileError(ValueError):
    """A safe, user-facing profile operation error."""


def validate_profile_id(profile_id: str) -> str:
    value = str(profile_id or "").strip()
    if not PROFILE_ID_RE.fullmatch(value):
        raise TTSProfileError(
            "配置 ID 只能包含小写字母、数字、连字符和下划线，且长度为 1–64"
        )
    return value


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise TTSProfileError("配置内容必须是有效的 JSON 数据") from exc


def _validate_json_tree(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise TTSProfileError("请求体嵌套层级不能超过 16 层")
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not (float("-inf") < value < float("inf")):
            raise TTSProfileError("请求体不能包含 NaN 或无穷大")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_tree(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TTSProfileError("请求体对象的字段名必须是文本")
            _validate_json_tree(item, depth=depth + 1)
        return
    raise TTSProfileError("请求体必须由 JSON 支持的数据组成")


def normalize_profile(profile: Any) -> dict[str, Any]:
    """Validate the public profile schema and apply documented defaults."""
    if not isinstance(profile, dict):
        raise TTSProfileError("HTTP TTS 配置必须是 JSON 对象")
    if _json_size(profile) > MAX_PROFILE_BYTES:
        raise TTSProfileError("HTTP TTS 配置不能超过 64 KB")

    allowed = {
        "name", "url", "method", "headers", "body", "response_type",
        "response_path", "timeout_seconds", "audio_format", "sample_rate", "channels",
    }
    unknown = set(profile) - allowed
    if unknown:
        raise TTSProfileError("HTTP TTS 配置包含不支持的字段")

    name = profile.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
        raise TTSProfileError("请填写不超过 128 个字符的配置名称")

    url = profile.get("url")
    if not isinstance(url, str) or not url.strip() or len(url.strip()) > 2048:
        raise TTSProfileError("请填写有效的 HTTP TTS 地址（最多 2048 个字符）")
    try:
        parsed = urlsplit(url.strip())
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        parsed = None
        valid_port = False
    if (
        parsed is None
        or parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or not valid_port
    ):
        raise TTSProfileError("地址必须是有效的 http:// 或 https:// URL")

    method = str(profile.get("method", "POST")).upper()
    if method not in {"GET", "POST"}:
        raise TTSProfileError("请求方式只能选择 GET 或 POST")

    headers = profile.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 64:
        raise TTSProfileError("请求头必须是最多 64 项的 JSON 对象")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()):
        raise TTSProfileError("请求头名称和值都必须是文本")
    if any("\r" in key or "\n" in key or "\r" in value or "\n" in value for key, value in headers.items()):
        raise TTSProfileError("请求头不能包含换行符")

    body = profile.get("body", {"text": "{{text}}"})
    if not isinstance(body, dict):
        raise TTSProfileError("请求体必须是 JSON 对象")
    _validate_json_tree(body)

    response_type = str(profile.get("response_type", "audio")).lower()
    if response_type not in {"audio", "base64", "url"}:
        raise TTSProfileError("响应类型只能选择 audio、base64 或 url")
    response_path = profile.get("response_path", "")
    if not isinstance(response_path, str) or len(response_path) > 256:
        raise TTSProfileError("响应字段路径必须是最多 256 个字符的文本")
    if response_type in {"base64", "url"} and not response_path.strip():
        raise TTSProfileError("base64 或 URL 响应需要填写响应字段路径")

    try:
        timeout = int(profile.get("timeout_seconds", 60))
    except (ValueError, TypeError, OverflowError) as exc:
        raise TTSProfileError("超时时间必须是 1–120 秒的整数") from exc
    if timeout < 1 or timeout > 120 or isinstance(profile.get("timeout_seconds"), bool):
        raise TTSProfileError("超时时间必须是 1–120 秒的整数")

    audio_format = str(profile.get("audio_format", "wav")).lower()
    if audio_format not in {"wav", "pcm"}:
        raise TTSProfileError("音频格式只能选择 WAV 或 PCM16LE")

    normalized: dict[str, Any] = {
        "name": name.strip(),
        "url": url.strip(),
        "method": method,
        "headers": dict(headers),
        "body": body,
        "response_type": response_type,
        "response_path": response_path.strip(),
        "timeout_seconds": timeout,
        "audio_format": audio_format,
    }
    if audio_format == "pcm":
        for field in ("sample_rate", "channels"):
            try:
                value = int(profile.get(field))
            except (TypeError, ValueError, OverflowError) as exc:
                raise TTSProfileError(f"PCM16LE 音频需要填写有效的 {field}") from exc
            if value <= 0 or isinstance(profile.get(field), bool):
                raise TTSProfileError(f"PCM16LE 音频需要填写有效的 {field}")
            normalized[field] = value
    return normalized


def _load_generic_http_backend(adapter_dir: Path | None = None):
    """Load the stdlib-only GenericHTTP module from the adapter source tree."""
    adapter_root = Path(adapter_dir or (ROOT_DIR / "NachoBot-Multimodal-Adapter"))
    adapter_path = str(adapter_root)
    if adapter_path not in sys.path:
        sys.path.insert(0, adapter_path)
    try:
        return importlib.import_module("nachobot_multimodal.tts.backends.GenericHTTP")
    except Exception as exc:
        # Avoid returning importer exception text because it can include paths or
        # values from a custom deployment.
        raise TTSProfileError("找不到 HTTP TTS 后端，请先更新多模态适配器文件") from exc


def validate_with_backend(
    profile: Any,
    *,
    backend_loader: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_profile(profile)
    backend = (backend_loader or _load_generic_http_backend)()
    try:
        validated = backend.validate_profile(normalized)
    except TTSProfileError:
        raise
    except Exception as exc:
        # Backend exceptions are deliberately not reflected into API responses.
        raise TTSProfileError("HTTP TTS 配置无法通过后端验证，请检查字段设置") from exc
    if not isinstance(validated, dict):
        raise TTSProfileError("HTTP TTS 后端返回了无效的验证结果")
    return normalize_profile(validated)


class TTSProfileStore:
    """Persist HTTP TTS profiles in TOML with atomic file replacement."""

    def __init__(
        self,
        *,
        config_dir: Path | None = None,
        base_config_path: Path | None = None,
        backend_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.config_dir = Path(config_dir or DEFAULT_CONFIG_DIR)
        self.profile_path = self.config_dir / "generic-http.toml"
        self.base_config_path = Path(base_config_path or (self.config_dir / "base.toml"))
        self.backend_loader = backend_loader or _load_generic_http_backend
        self._lock = RLock()

    @staticmethod
    def _read_document(path: Path, *, missing_ok: bool = False):
        if not path.exists():
            if missing_ok:
                return tomlkit.document()
            raise TTSProfileError("找不到 TTS 基础配置")
        try:
            if path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise TTSProfileError("TTS 配置文件不能超过 1 MB")
            return tomlkit.parse(path.read_text(encoding="utf-8"))
        except TTSProfileError:
            raise
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise TTSProfileError("TTS 配置文件无法读取或包含无效 TOML") from exc

    @staticmethod
    def _plain(value: Any) -> Any:
        if isinstance(value, dict) or hasattr(value, "items"):
            return {str(key): TTSProfileStore._plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)) or hasattr(value, "__iter__") and not isinstance(value, str):
            try:
                return [TTSProfileStore._plain(item) for item in value]
            except TypeError:
                pass
        return value

    @staticmethod
    def _to_toml(value: Any):
        if isinstance(value, dict):
            table = tomlkit.table()
            for key, item in value.items():
                table[key] = TTSProfileStore._to_toml(item)
            return table
        if isinstance(value, list):
            return [TTSProfileStore._to_toml(item) for item in value]
        return value

    @staticmethod
    def _decode_profile(value: Any) -> dict[str, Any]:
        profile = TTSProfileStore._plain(value)
        if not isinstance(profile, dict):
            raise TTSProfileError("HTTP TTS 配置必须是 TOML 表")
        body_json = profile.pop("body_json", None)
        if body_json is not None:
            if "body" in profile or not isinstance(body_json, str):
                raise TTSProfileError("HTTP TTS 请求体配置无效")
            try:
                profile["body"] = json.loads(body_json)
            except json.JSONDecodeError as exc:
                raise TTSProfileError("HTTP TTS 请求体 JSON 无效") from exc
        return normalize_profile(profile)

    @staticmethod
    def _encode_profile(profile: dict[str, Any]) -> dict[str, Any]:
        """Store body as compact JSON because TOML has no representation for null."""
        persisted = dict(profile)
        body = persisted.pop("body", {})
        persisted["body_json"] = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        return persisted

    @staticmethod
    def _atomic_write(path: Path, raw: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _profile_table(self, document):
        profiles = document.get("profiles")
        if profiles is None:
            profiles = tomlkit.table()
            document["profiles"] = profiles
        elif not hasattr(profiles, "items"):
            raise TTSProfileError("HTTP TTS 配置中的 profiles 必须是 TOML 表")
        return profiles

    def list_profiles(self) -> dict[str, Any]:
        with self._lock:
            document = self._read_document(self.profile_path, missing_ok=True)
            profiles = self._profile_table(document)
            active = document.get("active_profile", "")
            if not isinstance(active, str):
                active = ""
            base = self._read_document(self.base_config_path, missing_ok=True)
            enabled_table = base.get("enabled_tts", {})
            enabled = (
                enabled_table.get("enabled", [])
                if hasattr(enabled_table, "get")
                else []
            )
            if not isinstance(enabled, list):
                enabled = []
            items = []
            for raw_id, raw_profile in profiles.items():
                try:
                    profile_id = validate_profile_id(str(raw_id))
                    profile = self._decode_profile(raw_profile)
                except TTSProfileError:
                    # Keep malformed hand-edited entries visible enough to let a
                    # user replace or delete them; never echo their raw values.
                    profile_id = str(raw_id)
                    name = raw_profile.get("name", "Invalid profile") if hasattr(raw_profile, "get") else "Invalid profile"
                    profile = {"name": str(name)[:128]}
                items.append({"id": profile_id, "profile": profile})
            items.sort(key=lambda item: (item["profile"].get("name", "").casefold(), item["id"]))
            return {
                "profiles": items,
                "active_profile": active,
                "backend_enabled": bool(enabled and str(enabled[0]).casefold() == "generichttp"),
            }

    def save_profile(self, profile_id: str, profile: Any) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        normalized = validate_with_backend(profile, backend_loader=self.backend_loader)
        with self._lock:
            document = self._read_document(self.profile_path, missing_ok=True)
            profiles = self._profile_table(document)
            profiles[profile_id] = self._to_toml(self._encode_profile(normalized))
            self._atomic_write(self.profile_path, tomlkit.dumps(document))
        return {"id": profile_id, "profile": normalized}

    def delete_profile(self, profile_id: str) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        with self._lock:
            document = self._read_document(self.profile_path, missing_ok=True)
            profiles = self._profile_table(document)
            if profile_id not in profiles:
                raise KeyError("找不到指定的 HTTP TTS 配置")
            del profiles[profile_id]
            cleared_default = document.get("active_profile") == profile_id
            if cleared_default:
                document["active_profile"] = ""
            self._atomic_write(self.profile_path, tomlkit.dumps(document))
            return {"deleted": profile_id, "default_cleared": cleared_default}

    def set_default(self, profile_id: str) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        with self._lock:
            profile_document = self._read_document(self.profile_path, missing_ok=True)
            profiles = self._profile_table(profile_document)
            if profile_id not in profiles:
                raise KeyError("找不到指定的 HTTP TTS 配置")
            # Revalidate at activation time in case TOML was edited elsewhere.
            validate_with_backend(
                self._decode_profile(profiles[profile_id]), backend_loader=self.backend_loader
            )
            base_document = self._read_document(self.base_config_path)
            enabled_table = base_document.get("enabled_tts")
            if enabled_table is None:
                enabled_table = tomlkit.table()
                base_document["enabled_tts"] = enabled_table
            elif not hasattr(enabled_table, "get"):
                raise TTSProfileError("base.toml 中的 enabled_tts 必须是 TOML 表")
            prior_enabled = enabled_table.get("enabled", [])
            was_generic_http = bool(
                isinstance(prior_enabled, list)
                and prior_enabled
                and str(prior_enabled[0]).casefold() == "generichttp"
            )
            profile_document["active_profile"] = profile_id
            enabled_table["enabled"] = ["GenericHTTP"]
            profile_raw = tomlkit.dumps(profile_document)
            base_raw = tomlkit.dumps(base_document)
            old_profile_raw = self.profile_path.read_text(encoding="utf-8") if self.profile_path.exists() else None
            old_base_raw = self.base_config_path.read_text(encoding="utf-8")
            try:
                self._atomic_write(self.profile_path, profile_raw)
                self._atomic_write(self.base_config_path, base_raw)
            except Exception as exc:
                # Restore both files if the second atomic replace failed.
                try:
                    if old_profile_raw is None:
                        self.profile_path.unlink(missing_ok=True)
                    else:
                        self._atomic_write(self.profile_path, old_profile_raw)
                    self._atomic_write(self.base_config_path, old_base_raw)
                except OSError:
                    pass
                raise TTSProfileError("默认 HTTP TTS 配置保存失败，原配置已尝试恢复") from exc
            return {
                "active_profile": profile_id,
                "restart_required": not was_generic_http,
            }

    def export_profile(self, profile_id: str) -> dict[str, Any]:
        profile_id = validate_profile_id(profile_id)
        with self._lock:
            document = self._read_document(self.profile_path, missing_ok=True)
            profiles = self._profile_table(document)
            if profile_id not in profiles:
                raise KeyError("找不到指定的 HTTP TTS 配置")
            profile = self._decode_profile(profiles[profile_id])
            return {"id": profile_id, "profile": sanitize_export_profile(profile)}


def _secret_field_name(name: str) -> bool:
    # Split snake/kebab/space and camel case before matching. This catches
    # accessToken / apiKey without accidentally removing a field such as monkey.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    parts = re.split(r"[^A-Za-z0-9]+", spaced)
    lowered = [part.lower() for part in parts if part]
    if any(part in _SECRET_NAME_PARTS for part in lowered):
        return True
    joined = "".join(lowered)
    return any(
        joined.endswith(suffix)
        for suffix in ("token", "secret", "password", "passwd", "credential", "credentials", "authorization", "apikey")
    )


def _remove_named_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_named_secrets(item)
            for key, item in value.items()
            if not _secret_field_name(str(key))
        }
    if isinstance(value, list):
        return [_remove_named_secrets(item) for item in value]
    return value


def _public_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        netloc = host + (f":{port}" if port is not None else "")
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return ""


def sanitize_export_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Remove request headers, URL credentials/query, and named body secrets."""
    sanitized = dict(profile)
    sanitized.pop("headers", None)
    sanitized["url"] = _public_url(str(profile.get("url", "")))
    sanitized["body"] = _remove_named_secrets(profile.get("body", {}))
    return sanitized


async def synthesize_preview(
    profile: Any,
    text: str,
    *,
    language: str = "",
    voice: str = "",
    backend_loader: Callable[[], Any] | None = None,
) -> bytes:
    """Validate and synthesize an unsaved profile without touching defaults."""
    normalized = validate_with_backend(profile, backend_loader=backend_loader)
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_PREVIEW_TEXT_CHARS:
        raise TTSProfileError("试听文本必须是 1–512 个字符")
    for field, value in (("language", language), ("voice", voice)):
        if not isinstance(value, str) or len(value) > 64:
            raise TTSProfileError(f"{field} 最多可填写 64 个字符")
    backend = (backend_loader or _load_generic_http_backend)()
    try:
        audio = await backend.synthesize_profile(
            normalized, text, language=language, voice=voice
        )
    except Exception as exc:
        status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(status_code, int) and 100 <= status_code <= 599:
            raise TTSProfileError(f"HTTP TTS 服务返回状态码 {status_code}") from exc
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            raise TTSProfileError("HTTP TTS 请求超时，请检查服务地址和超时时间") from exc
        raise TTSProfileError("HTTP TTS 试听失败，请检查地址、响应格式和服务状态") from exc
    if not isinstance(audio, bytes) or not audio:
        raise TTSProfileError("HTTP TTS 服务没有返回音频")
    if len(audio) > MAX_PREVIEW_AUDIO_BYTES:
        raise TTSProfileError("试听音频超过 16 MB 限制")
    return audio
