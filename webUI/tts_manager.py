"""TTS bridge and short-lived audio cache for the WebUI chat."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import socket
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import tomlkit

logger = logging.getLogger("webui.tts")

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "NachoBot-Multimodal-Adapter" / "configs" / "base.toml"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "cache" / "tts"
CORE_HEALTH_TIMEOUT_SECONDS = 4.0
_TTS_STATUS_TRANSPORT_GRACE_SECONDS = 8.0
_NACHOBOT_ROOT = ROOT_DIR / "NachoBot"
_NACHOBOT_ENV_PATH = _NACHOBOT_ROOT / ".env"
_BOT_CONFIG_PATH = _NACHOBOT_ROOT / "config" / "bot_config.toml"
_DEFAULT_CORE_HOST = "127.0.0.1"
_DEFAULT_CORE_PORT = 8000
_MAX_AUDIO_BYTES = 16 * 1024 * 1024
_MAX_AUDIO_BASE64_CHARS = 4 * ((_MAX_AUDIO_BYTES + 2) // 3)
_MAX_JSON_RESPONSE_BYTES = _MAX_AUDIO_BASE64_CHARS + (256 * 1024)
_TTS_ENGINE_CONFIG_FILES = {
    "GPT_Sovits": "gpt-sovits.toml",
    "Vox": "vox.toml",
}


class TTSUnavailableError(RuntimeError):
    """Raised when the configured TTS service cannot accept requests."""


class TTSGenerationError(RuntimeError):
    """Raised when TTS synthesis fails after the service is ready."""


def _get_core_auth_token() -> str:
    """Return the Core bearer token without ever logging its value."""

    environment_token = os.getenv("NACHOBOT_CORE_TOKEN", "").strip()
    if environment_token:
        return environment_token
    if not _BOT_CONFIG_PATH.exists():
        return ""
    try:
        document = tomlkit.parse(_BOT_CONFIG_PATH.read_text(encoding="utf-8"))
        tokens = document.get("ncnk_message", {}).get("auth_token", []) or []
        if isinstance(tokens, str):
            tokens = [tokens]
        for token in tokens:
            value = str(token).strip()
            if value:
                return value
    except Exception:
        # A malformed live config must not expose a credential-bearing parse
        # error through WebUI logs or status responses.
        return ""
    return ""


def _get_core_base_url() -> str:
    """Resolve the canonical NachoBot Core URL.

    The live Core ``.env`` wins, then the WebUI service definition, with the
    documented 8000 port as the safe fallback.  The multimodal adapter's
    historical ``[server]`` section is intentionally not consulted.
    """

    host: str | None = None
    port: int | None = None
    if _NACHOBOT_ENV_PATH.exists():
        try:
            for line in _NACHOBOT_ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key == "HOST" and value:
                    host = value
                elif key == "PORT" and value:
                    try:
                        candidate = int(value)
                    except ValueError:
                        continue
                    if 1 <= candidate <= 65535:
                        port = candidate
        except OSError:
            pass

    if not host or port is None:
        try:
            from process_manager import SERVICE_DEFS

            core_service = SERVICE_DEFS.get("nachobot")
            if core_service is not None:
                host = host or str(core_service.env_extra.get("HOST") or "")
                candidate = core_service.port or core_service.env_extra.get("PORT")
                if port is None and candidate:
                    candidate_int = int(candidate)
                    if 1 <= candidate_int <= 65535:
                        port = candidate_int
        except (ImportError, TypeError, ValueError, AttributeError):
            pass

    host = host or _DEFAULT_CORE_HOST
    port = port or _DEFAULT_CORE_PORT
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


class TTSManager:
    """Check TTS readiness, synthesize speech, and cache it for a fixed TTL."""

    def __init__(
        self,
        *,
        config_path: Path = DEFAULT_CONFIG_PATH,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        ttl_seconds: int = 24 * 60 * 60,
        cleanup_interval_seconds: int = 60 * 60,
    ) -> None:
        self.config_path = Path(config_path)
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self._cleanup_task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_ready_status: dict[str, Any] | None = None
        self._last_ready_status_identity: tuple[str, bytes, str] | None = None
        self._last_ready_status_at: float | None = None

    async def start(self) -> None:
        """Create the cache and start periodic expiry cleanup."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.cleanup_expired)
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(),
                name="webui-tts-cache-cleanup",
            )

    async def close(self) -> None:
        """Stop the periodic cache cleanup task."""
        if self._cleanup_task is None:
            return
        self._cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._cleanup_task
        self._cleanup_task = None

    async def status(self) -> dict[str, Any]:
        """Return readiness observed by Core for the public 9880 TTS Runtime."""
        try:
            core_url = _get_core_base_url()
            core_token = _get_core_auth_token()
        except (OSError, ValueError, TypeError) as exc:
            self._clear_ready_status()
            return {
                "ready": False,
                "adapter_ready": False,
                "core_ready": False,
                "engine_ready": False,
                "text_only": False,
                "error": str(exc),
            }

        try:
            core_health = await asyncio.to_thread(
                self._request_json,
                f"{core_url}/api/multimodal/health",
                CORE_HEALTH_TIMEOUT_SECONDS,
                core_token,
            )
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            if self._is_transport_error(exc):
                cached = self._recent_ready_status_after_transport_failure(
                    core_url,
                    core_token,
                )
                if cached is not None:
                    return cached
            self._clear_ready_status()
            result = {
                "ready": False,
                "adapter_ready": False,
                "core_ready": False,
                "engine_ready": False,
                "text_only": False,
                "error": self._format_request_error(exc),
            }
            if self._is_transport_error(exc):
                result["transient"] = True
            return result

        if not isinstance(core_health, dict):
            self._clear_ready_status()
            return {
                "ready": False,
                "adapter_ready": False,
                "core_ready": False,
                "engine_ready": False,
                "text_only": False,
                "error": "Core 多模态 API 返回了无效状态",
            }

        desired_profile = core_health.get("desired_profile")
        capabilities = core_health.get("capabilities")
        core_status = core_health.get("status")
        observed_local = core_health.get("observed_local")
        observed_perception = (
            observed_local.get("perception") if isinstance(observed_local, dict) else None
        )
        observed_tts = observed_local.get("tts") if isinstance(observed_local, dict) else None
        required = ({
            "full": (True, True),
            "lite": (False, True),
            "potato": (False, False),
        }.get(desired_profile) if isinstance(desired_profile, str) else None)
        valid_health = (
            isinstance(core_status, str)
            and core_status in {"ok", "degraded"}
            and required is not None
            and isinstance(capabilities, dict)
            and type(capabilities.get("tts")) is bool
            and capabilities["tts"] is (desired_profile != "potato")
            and isinstance(observed_local, dict)
            and observed_local.get("profile") == desired_profile
            and type(observed_local.get("ready")) is bool
            and isinstance(observed_perception, dict)
            and isinstance(observed_tts, dict)
        )
        if valid_health:
            flags = (
                observed_perception.get("required"),
                observed_perception.get("ready"),
                observed_tts.get("required"),
                observed_tts.get("ready"),
            )
            valid_health = (
                all(type(value) is bool for value in flags)
                and (flags[0], flags[2]) == required
                and observed_local["ready"]
                is ((not flags[0] or flags[1]) and (not flags[2] or flags[3]))
                and core_status == ("ok" if observed_local["ready"] else "degraded")
            )
        if not valid_health:
            self._clear_ready_status()
            return {
                "ready": False,
                "adapter_ready": False,
                "core_ready": False,
                "engine_ready": False,
                "text_only": False,
                "error": "Core 多模态 API 返回了无效状态",
            }

        core_ready = True
        text_only = desired_profile == "potato"
        # FULL may still expose a healthy public TTS Runtime when local
        # perception is degraded and the Core falls back to a remote provider.
        tts_service_ready = observed_tts["ready"]

        if text_only:
            self._clear_ready_status()
            return {
                "ready": False,
                "adapter_ready": False,
                "core_ready": core_ready,
                "engine_ready": False,
                "text_only": True,
                "engine": "",
                "error": "Core 当前为 potato/text-only 模式，TTS 不可用",
            }

        try:
            tts_settings = self._load_tts_settings()
        except (OSError, ValueError, TypeError) as exc:
            self._clear_ready_status()
            return {
                "ready": False,
                "adapter_ready": False,
                "core_ready": core_ready,
                "engine_ready": False,
                "text_only": False,
                "error": str(exc),
            }

        # ``observed_local.tts`` is Core's probe of the configured public
        # endpoint (9880). Do not probe a backend-specific/private port here.
        runtime_ready = tts_service_ready is True
        adapter_ready = (
            core_ready
            and runtime_ready
        )
        ready = adapter_ready

        error = ""
        if not adapter_ready:
            error = "Core 9880 TTS Runtime 未就绪"

        result = {
            "ready": ready,
            "adapter_ready": adapter_ready,
            "core_ready": core_ready,
            "engine_ready": runtime_ready,
            "runtime_ready": runtime_ready,
            "text_only": False,
            "engine": tts_settings["engine"],
            "error": error,
        }
        if ready:
            self._remember_ready_status(result, core_url, core_token, tts_settings["engine"])
        else:
            self._clear_ready_status()
        return result

    @staticmethod
    def _is_transport_error(error: BaseException) -> bool:
        if isinstance(error, HTTPError):
            return False
        if isinstance(error, URLError):
            return isinstance(error.reason, (OSError, TimeoutError))
        return isinstance(error, (OSError, TimeoutError))

    def _status_identity(self, core_url: str, core_token: str, engine: str) -> tuple[str, bytes, str]:
        # Keep only a digest of the token in memory, never the token itself in
        # status cache state. Include config mtimes so a config edit expires it.
        token_fingerprint = hashlib.sha256(core_token.encode("utf-8")).digest()
        config_fingerprint = self._cache_key("", engine)
        return core_url.rstrip("/"), token_fingerprint, config_fingerprint

    def _remember_ready_status(
        self,
        result: dict[str, Any],
        core_url: str,
        core_token: str,
        engine: str,
    ) -> None:
        self._last_ready_status = dict(result)
        self._last_ready_status_identity = self._status_identity(core_url, core_token, engine)
        self._last_ready_status_at = time.monotonic()

    def _clear_ready_status(self) -> None:
        self._last_ready_status = None
        self._last_ready_status_identity = None
        self._last_ready_status_at = None

    def _recent_ready_status_after_transport_failure(
        self,
        core_url: str,
        core_token: str,
    ) -> dict[str, Any] | None:
        if self._last_ready_status is None or self._last_ready_status_at is None:
            return None
        try:
            settings = self._load_tts_settings()
        except (OSError, ValueError, TypeError):
            self._clear_ready_status()
            return None
        identity = self._status_identity(core_url, core_token, settings["engine"])
        if identity != self._last_ready_status_identity:
            self._clear_ready_status()
            return None
        if time.monotonic() - self._last_ready_status_at > _TTS_STATUS_TRANSPORT_GRACE_SECONDS:
            self._clear_ready_status()
            return None
        cached = dict(self._last_ready_status)
        cached["transient"] = True
        cached["error"] = "Core 健康检查暂时不可用"
        return cached

    async def generate(self, text: str) -> tuple[Path, bool]:
        """Return a cached/generated WAV path and whether it was a cache hit."""
        normalized_text = str(text or "").strip()
        if not normalized_text:
            raise ValueError("TTS 文本不能为空")
        if len(normalized_text) > 10_000:
            raise ValueError("TTS 文本不能超过 10000 个字符")

        endpoints = self._load_endpoints()
        cache_key = self._cache_key(normalized_text, endpoints["engine"])
        cache_path = self.cache_dir / f"{cache_key}.wav"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        try:
            async with lock:
                service_status = await self.status()
                if not service_status["ready"]:
                    raise TTSUnavailableError(
                        service_status.get("error") or "TTS 服务未就绪"
                    )

                if self._is_fresh(cache_path):
                    return cache_path, True

                try:
                    audio = await asyncio.to_thread(
                        self._request_audio,
                        f"{endpoints['core_url']}/api/multimodal/tts",
                        normalized_text,
                        endpoints["core_token"],
                    )
                except (HTTPError, URLError, OSError, TimeoutError) as exc:
                    raise TTSGenerationError(self._format_request_error(exc)) from exc

                if not audio:
                    raise TTSGenerationError("TTS 服务返回了空音频")
                if len(audio) > _MAX_AUDIO_BYTES:
                    raise TTSGenerationError("TTS 音频超过 16 MB 限制")

                temp_path = cache_path.with_suffix(f".{os.getpid()}.tmp")
                temp_path.write_bytes(audio)
                temp_path.replace(cache_path)
                return cache_path, False
        finally:
            if not lock.locked():
                self._locks.pop(cache_key, None)

    def cleanup_expired(self, *, now: float | None = None) -> int:
        """Delete cache files older than the fixed 24-hour TTL."""
        if not self.cache_dir.exists():
            return 0

        cutoff = (time.time() if now is None else now) - self.ttl_seconds
        deleted = 0
        for path in self.cache_dir.glob("*.wav"):
            try:
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
                    deleted += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("Unable to remove expired TTS cache %s: %s", path, exc)
        return deleted

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cleanup_interval_seconds)
            deleted = await asyncio.to_thread(self.cleanup_expired)
            if deleted:
                logger.info("Removed %s expired WebUI TTS cache files", deleted)

    def _load_endpoints(self) -> dict[str, Any]:
        settings = self._load_tts_settings()
        return {
            "core_url": _get_core_base_url(),
            "core_token": _get_core_auth_token(),
            **settings,
        }

    def _load_tts_settings(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise ValueError("找不到 TTS 基础配置")

        document = tomlkit.parse(self.config_path.read_text(encoding="utf-8"))
        enabled = document.get("enabled_tts", {}).get("enabled", [])
        if not isinstance(enabled, list) or not enabled:
            raise ValueError("没有启用 TTS 引擎")
        engine = str(enabled[0])

        engine_config_name = _TTS_ENGINE_CONFIG_FILES.get(engine)
        if not engine_config_name:
            raise ValueError(f"不支持的 TTS 引擎：{engine}")
        engine_config_path = self.config_path.parent / engine_config_name
        if not engine_config_path.exists():
            raise ValueError(f"找不到 TTS 引擎配置：{engine_config_path.name}")
        try:
            tomlkit.parse(engine_config_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(
                f"TTS 引擎配置无效：{engine_config_path.name}"
            ) from exc

        return {
            "engine": engine,
            "runtime_port": 9880,
        }

    def _cache_key(self, text: str, engine: str) -> str:
        config_fingerprint = [engine]
        config_dir = self.config_path.parent
        for path in (
            self.config_path,
            config_dir / "gpt-sovits.toml",
            config_dir / "vox.toml",
        ):
            try:
                config_fingerprint.append(f"{path.name}:{path.stat().st_mtime_ns}")
            except OSError:
                continue
        payload = "\0".join([text, *config_fingerprint]).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _is_fresh(self, path: Path) -> bool:
        try:
            return time.time() - path.stat().st_mtime < self.ttl_seconds
        except OSError:
            return False

    @staticmethod
    def _connectable_host(host: str) -> str:
        return "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host

    @staticmethod
    def _port_is_open(host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _request_json(url: str, timeout: float, token: str = "") -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, headers=headers)
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Core 多模态 API 返回了无效 JSON")
        return payload

    @staticmethod
    def _request_audio(url: str, text: str, token: str = "") -> bytes:
        body = json.dumps(
            {"text": text, "platform": "webui"},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urlopen(request, timeout=180) as response:
            raw = response.read(_MAX_JSON_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_JSON_RESPONSE_BYTES:
            raise TTSGenerationError("TTS 服务响应超过大小限制")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TTSGenerationError("TTS 服务返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise TTSGenerationError("TTS 服务返回了无效 JSON")
        encoded = payload.get("audio_base64")
        if not isinstance(encoded, str) or not encoded.strip():
            raise TTSGenerationError("TTS 服务返回了空音频")
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, base64.binascii.Error) as exc:
            raise TTSGenerationError("TTS 服务返回了无效 base64 音频") from exc
        if not audio:
            raise TTSGenerationError("TTS 服务返回了空音频")
        if len(audio) > _MAX_AUDIO_BYTES:
            raise TTSGenerationError("TTS 音频超过 16 MB 限制")
        return audio

    @staticmethod
    def _format_request_error(error: Exception) -> str:
        if isinstance(error, HTTPError):
            try:
                payload = json.loads(error.read().decode("utf-8"))
                detail = payload.get("detail")
                if detail:
                    return str(detail)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            return f"TTS 服务请求失败（HTTP {error.code}）"
        return f"TTS 服务请求失败：{error}"
