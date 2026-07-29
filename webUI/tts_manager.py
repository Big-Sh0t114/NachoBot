"""TTS bridge and short-lived audio cache for the WebUI chat."""

from __future__ import annotations

import asyncio
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
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import tomlkit

logger = logging.getLogger("webui.tts")

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "NachoBot-Multimodal-Adapter" / "configs" / "base.toml"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "cache" / "tts"


class TTSUnavailableError(RuntimeError):
    """Raised when the configured TTS service cannot accept requests."""


class TTSGenerationError(RuntimeError):
    """Raised when TTS synthesis fails after the service is ready."""


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
        """Return whether both the adapter and its configured engine are ready."""
        try:
            endpoints = self._load_endpoints()
        except (OSError, ValueError, TypeError) as exc:
            return {
                "ready": False,
                "adapter_ready": False,
                "engine_ready": False,
                "error": str(exc),
            }

        adapter_health, engine_ready = await asyncio.gather(
            asyncio.to_thread(
                self._request_json,
                f"{endpoints['adapter_url']}/api/health",
                1.5,
            ),
            asyncio.to_thread(
                self._port_is_open,
                endpoints["engine_host"],
                endpoints["engine_port"],
                0.6,
            ),
            return_exceptions=True,
        )

        adapter_ready = (
            isinstance(adapter_health, dict)
            and adapter_health.get("status") == "ok"
            and bool(adapter_health.get("tts_backends"))
        )
        engine_ready = engine_ready is True
        ready = adapter_ready and engine_ready

        error = ""
        if not adapter_ready:
            error = "TTS 适配器未就绪"
        elif not engine_ready:
            error = "TTS 引擎未就绪"

        return {
            "ready": ready,
            "adapter_ready": adapter_ready,
            "engine_ready": engine_ready,
            "engine": endpoints["engine"],
            "error": error,
        }

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

        if self._is_fresh(cache_path):
            return cache_path, True

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        try:
            async with lock:
                if self._is_fresh(cache_path):
                    return cache_path, True

                service_status = await self.status()
                if not service_status["ready"]:
                    raise TTSUnavailableError(
                        service_status.get("error") or "TTS 服务未就绪"
                    )

                try:
                    audio = await asyncio.to_thread(
                        self._request_audio,
                        f"{endpoints['adapter_url']}/api/tts",
                        normalized_text,
                    )
                except (HTTPError, URLError, OSError, TimeoutError) as exc:
                    raise TTSGenerationError(self._format_request_error(exc)) from exc

                if not audio:
                    raise TTSGenerationError("TTS 服务返回了空音频")
                if len(audio) > 50 * 1024 * 1024:
                    raise TTSGenerationError("TTS 音频超过 50 MB 限制")

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
        if not self.config_path.exists():
            raise ValueError("找不到 TTS 基础配置")

        document = tomlkit.parse(self.config_path.read_text(encoding="utf-8"))
        server = document.get("server", {})
        adapter_host = self._connectable_host(str(server.get("host", "127.0.0.1")))
        adapter_port = int(server.get("port", 8070))

        enabled = document.get("enabled_tts", {}).get("enabled", [])
        if not isinstance(enabled, list) or not enabled:
            raise ValueError("没有启用 TTS 引擎")
        engine = str(enabled[0])

        engine_config = document.get("plugins", {}).get(engine, {})
        engine_url = str(engine_config.get("api_base", "")).rstrip("/")
        parsed_engine = urlparse(engine_url)
        if not parsed_engine.hostname or parsed_engine.port is None:
            raise ValueError(f"TTS 引擎 {engine} 的 api_base 配置无效")

        return {
            "adapter_url": f"http://{adapter_host}:{adapter_port}",
            "engine": engine,
            "engine_host": self._connectable_host(parsed_engine.hostname),
            "engine_port": parsed_engine.port,
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
    def _request_json(url: str, timeout: float) -> dict[str, Any]:
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _request_audio(url: str, text: str) -> bytes:
        body = json.dumps(
            {"text": text, "platform": "webui"},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "audio/wav",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urlopen(request, timeout=180) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith("audio/"):
                raise TTSGenerationError(
                    f"TTS 服务返回了不支持的内容类型：{content_type}"
                )
            return response.read(50 * 1024 * 1024 + 1)

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
