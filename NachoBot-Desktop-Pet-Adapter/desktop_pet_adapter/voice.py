"""Cancellable desktop speech, independent of Local Host and WebUI."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .config import DesktopChatConfig


_CORE_TTS_ERROR_BODY_MAX_BYTES = 4_096
_CORE_TTS_ERROR_MESSAGES = {
    "invalid_request": "语音请求无效",
    "speech_policy_unavailable": "语音服务暂不可用",
    "speech_preparation_timeout": "日语语音文本转换超时",
    "speech_preparation_unavailable": "日语语音文本转换失败",
    "speech_preparation_invalid_response": "日语语音文本转换结果无效",
    "speech_preparation_wrong_language": "日语语音文本未通过语言校验",
    "speech_preparation_invalid_request": "语音文本请求无效",
}


def _core_tts_error_message(status: int, body: bytes) -> str:
    """Expose only allowlisted Core detail codes in desktop diagnostics."""

    description = None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        code = payload.get("detail")
        if isinstance(code, str):
            description = _CORE_TTS_ERROR_MESSAGES.get(code)

    prefix = f"Core TTS 请求失败 (HTTP {status})"
    return f"{prefix}: {description}" if description else prefix


async def _read_core_error_body(content: Any) -> bytes:
    body = bytearray()
    while len(body) < _CORE_TTS_ERROR_BODY_MAX_BYTES:
        remaining = _CORE_TTS_ERROR_BODY_MAX_BYTES - len(body)
        chunk = await content.read(min(1_024, remaining))
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body)


def speech_segments(text: str) -> list[str]:
    """Keep sentence prosody; a small first sentence reduces the initial wait."""
    parts = re.findall(r"[^。！？!?\n]+[。！？!?]?", text.strip())
    result: list[str] = []
    for part in parts:
        # Bound each synthesis request without removing any of the reply.
        while len(part) > 80:
            cut = max(part.rfind(mark, 20, 80) for mark in "，,；;、 ")
            cut = cut + 1 if cut >= 20 else 80
            result.append(part[:cut])
            part = part[cut:]
        if part.strip():
            result.append(part.strip())
    return result


def pcm_format(headers: Any) -> dict[str, Any]:
    """Validate the Core multimodal PCM stream contract."""
    if headers.get("X-TTS-Stream-Version") != "1":
        raise ValueError("不支持的 TTS 流版本")
    if headers.get("X-Audio-Codec") != "pcm_s16le":
        raise ValueError("TTS 必须返回 pcm_s16le")
    keys = ("X-Audio-Sample-Rate", "X-Audio-Channels", "X-Audio-Sample-Width")
    try:
        rate, channels, width = (int(headers[key]) for key in keys)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("TTS 流缺少有效的音频格式信息") from exc
    if not 8000 <= rate <= 96000 or channels not in (1, 2) or width != 2:
        raise ValueError("TTS 流的采样率、声道或位宽不受支持")
    return dict(sample_rate=rate, channels=channels, sample_width=width, codec="pcm_s16le")


class DesktopVoice:
    """One event-loop worker; mute invalidates even replies still being generated."""

    def __init__(
        self,
        config: DesktopChatConfig,
        emit: Callable,
        logger: Any,
        on_error: Callable[[str], None],
    ) -> None:
        self.config, self.emit, self.logger, self.on_error = config, emit, logger, on_error
        self._lock = threading.RLock()
        self._epoch = 0
        self._enabled = config.play_audio
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._future: Future | None = None
        self._policy_future: Future | None = None
        self._tts_policy = {
            "backend": None,
            "ready": False,
            "fixed_language": None,
            "language_switch_allowed": False,
        }
        self._last_fixed_language: str | None = None

    def get_tts_policy(self) -> dict[str, Any]:
        """Return a copy of the last Core policy snapshot without doing I/O."""
        with self._lock:
            return dict(self._tts_policy)

    def refresh_tts_policy(self, callback: Callable[[dict[str, Any]], None]) -> bool:
        """Fetch Core policy on the voice worker and deliver it to a private callback."""
        with self._lock:
            if self._closed or self._policy_future is not None and not self._policy_future.done():
                return False
            self._ensure_loop_locked()
            assert self._loop is not None
            self._policy_future = asyncio.run_coroutine_threadsafe(
                self._refresh_tts_policy(callback), self._loop
            )
            return True

    def token(self) -> int:
        with self._lock:
            return self._epoch

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            self._cancel_locked()

    def _cancel_locked(self) -> None:
        self._epoch += 1
        if self._future is not None:
            self._future.cancel()
            self._future = None
        self.emit("stop_audio", None)

    def speak(self, text: str, language: str, token: int) -> bool:
        with self._lock:
            if self._closed or not self._enabled or token != self._epoch:
                return False
            self._cancel_locked()
            epoch = self._epoch
            self._ensure_loop_locked()
            assert self._loop is not None
            self._future = asyncio.run_coroutine_threadsafe(
                self._speak(text, language, epoch), self._loop
            )
            return True

    def _ensure_loop_locked(self) -> None:
        if self._loop is not None:
            return
        ready = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self._thread = threading.Thread(target=run, name="live2d-voice", daemon=True)
        self._thread.start()
        if not ready.wait(2):
            raise RuntimeError("desktop voice worker did not start")

    @staticmethod
    def _authorization_header() -> str | None:
        token = os.environ.get("NACHOBOT_CORE_TOKEN", "").strip()
        if not token:
            return None
        if token[:7].casefold() == "bearer ":
            token = token[7:].strip()
        return f"Bearer {token}" if token else None

    def _core_http_origin(self) -> str:
        parts = urlsplit(self.config.core_url)
        schemes = {"ws": "http", "wss": "https"}
        scheme = schemes.get(parts.scheme.casefold())
        host = parts.hostname
        if scheme is None or not host:
            raise ValueError("core_url must contain a ws:// or wss:// origin")
        # Build authority from hostname/port so userinfo in a URL can never be
        # forwarded in an HTTP request or accidentally included in diagnostics.
        authority = f"[{host}]" if ":" in host else host
        if parts.port is not None:
            authority = f"{authority}:{parts.port}"
        return f"{scheme}://{authority}"

    def _core_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        authorization = self._authorization_header()
        if authorization:
            headers["Authorization"] = authorization
        return headers

    async def _fetch_tts_policy(self) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=4, sock_connect=1, sock_read=3)
        url = self._core_http_origin() + "/api/multimodal/tts/policy"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=self._core_headers()) as response:
                response.raise_for_status()
                policy = await response.json(content_type=None)
        if not isinstance(policy, dict):
            raise ValueError("Core TTS policy response must be an object")
        backend = policy.get("backend")
        fixed_language = policy.get("fixed_language")
        ready = policy.get("ready")
        switch_allowed = policy.get("language_switch_allowed")
        if (
            (backend is not None and not isinstance(backend, str))
            or type(ready) is not bool
            or (fixed_language is not None and not isinstance(fixed_language, str))
            or type(switch_allowed) is not bool
        ):
            raise ValueError("Core TTS policy response has invalid fields")
        return {
            "backend": backend,
            "ready": ready,
            "fixed_language": fixed_language,
            "language_switch_allowed": switch_allowed,
        }

    async def _refresh_tts_policy(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        try:
            reported = await self._fetch_tts_policy()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            self.logger.debug("[DesktopVoice] Core TTS policy refresh unavailable: {}", type(exc).__name__)
            reported = None

        with self._lock:
            if self._closed:
                return
            if reported is None:
                # A transient outage must never erase a fixed-language lock
                # learned from Core before it became unavailable.
                self._tts_policy = {
                    "backend": self._tts_policy.get("backend"),
                    "ready": False,
                    "fixed_language": self._last_fixed_language,
                    "language_switch_allowed": False,
                }
            elif reported["ready"]:
                fixed = reported["fixed_language"]
                self._last_fixed_language = (
                    fixed if fixed and not reported["language_switch_allowed"] else None
                )
                self._tts_policy = dict(reported)
            else:
                fixed = reported["fixed_language"]
                if fixed and not reported["language_switch_allowed"]:
                    self._last_fixed_language = fixed
                self._tts_policy = {
                    "backend": reported["backend"],
                    "ready": False,
                    "fixed_language": self._last_fixed_language,
                    "language_switch_allowed": False,
                }
            snapshot = dict(self._tts_policy)
            callback(snapshot)

    def _send(self, epoch: int, command: str, value: Any) -> bool:
        with self._lock:
            if self._closed or not self._enabled or epoch != self._epoch:
                return False
            self.emit(command, value)
            return True

    async def _speak(self, text: str, language: str, epoch: int) -> None:
        started = time.monotonic()
        try:
            await self._multimodal(text, language, epoch, started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._send(epoch, "stop_audio", None):
                self.logger.warning(
                    "[DesktopVoice] {} synthesis failed: {}", self.config.tts_provider, exc
                )
                self.on_error(f"语音生成失败：{exc}。文字回答已保留。")

    async def _multimodal(self, text: str, language: str, epoch: int, started: float) -> None:
        timeout = aiohttp.ClientTimeout(total=120, sock_connect=5, sock_read=90)
        url = self._core_http_origin() + "/api/multimodal/tts/stream"
        payload = {
            "text": text,
            "text_lang": language,
            "platform": "local.live2d",
            "prepare_text": True,
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            response = await session.post(url, json=payload, headers=self._core_headers())
            async with response:
                if response.status >= 400:
                    try:
                        error_body = await _read_core_error_body(response.content)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        error_body = b""
                    raise ValueError(
                        _core_tts_error_message(
                            response.status,
                            error_body,
                        )
                    )
                response.raise_for_status()
                fmt = pcm_format(response.headers)
                identity = dict(
                    fmt, stream_id=f"desktop-{epoch}", parent_message_id=f"desktop-{epoch}"
                )
                self._send(epoch, "voice_stream", dict(identity, event="start"))
                remainder = b""
                seq = total = 0
                frame_bytes = fmt["channels"] * fmt["sample_width"]
                async for block in response.content.iter_chunked(32768):
                    block = remainder + block
                    aligned = len(block) - len(block) % frame_bytes
                    remainder = block[aligned:]
                    block = block[:aligned]
                    if not block:
                        continue
                    total += len(block)
                    if total > 32 * 1024 * 1024:
                        raise ValueError("TTS 流超过大小限制")
                    if not self._send(
                        epoch, "voice_stream", dict(identity, event="chunk", seq=seq, pcm=block)
                    ):
                        return
                    if seq == 0:
                        self.logger.info(
                            "[DesktopVoice] multimodal first PCM: {:.3f}s",
                            time.monotonic() - started,
                        )
                    seq += 1
                if remainder or not total:
                    raise ValueError("TTS 流为空或结束于不完整音频帧")
                self._send(epoch, "voice_stream", dict(identity, event="end"))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_locked()
            if self._policy_future is not None:
                self._policy_future.cancel()
                self._policy_future = None
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2)
