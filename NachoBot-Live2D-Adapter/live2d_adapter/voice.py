"""Cancellable desktop speech, independent of Local Host and WebUI."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import aiohttp
import edge_tts

from .config import DesktopChatConfig


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
    """Validate the upstream PCM contract, including legacy adapter headers."""
    modern = headers.get("X-TTS-Stream-Version") is not None
    if modern:
        if headers.get("X-TTS-Stream-Version") != "1":
            raise ValueError("不支持的 TTS 流版本")
        if headers.get("X-Audio-Codec") != "pcm_s16le":
            raise ValueError("TTS 必须返回 pcm_s16le")
        keys = ("X-Audio-Sample-Rate", "X-Audio-Channels", "X-Audio-Sample-Width")
    else:
        keys = ("X-Sample-Rate", "X-Channels", "X-Sample-Width")
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
            if self._loop is None:
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
                ready.wait(2)
            assert self._loop is not None
            self._future = asyncio.run_coroutine_threadsafe(
                self._speak(text, language, epoch), self._loop
            )
            return True

    def _send(self, epoch: int, command: str, value: Any) -> bool:
        with self._lock:
            if self._closed or not self._enabled or epoch != self._epoch:
                return False
            self.emit(command, value)
            return True

    async def _speak(self, text: str, language: str, epoch: int) -> None:
        started = time.monotonic()
        try:
            if self.config.tts_provider == "neural":
                await self._neural(text, language, epoch, started)
            else:
                await self._multimodal(text, language, epoch, started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._send(epoch, "stop_audio", None):
                self.logger.warning(
                    "[DesktopVoice] {} synthesis failed: {}", self.config.tts_provider, exc
                )
                self.on_error(f"语音生成失败：{exc}。文字回答已保留。")

    async def _neural(self, text: str, language: str, epoch: int, started: float) -> None:
        voice = {"ja": "ja-JP-NanamiNeural", "en": "en-US-JennyNeural"}.get(
            language, self.config.tts_voice
        )
        parts = speech_segments(text)

        async def synthesize(part: str) -> bytes:
            speaker = edge_tts.Communicate(
                part, voice, rate=self.config.tts_rate, connect_timeout=5, receive_timeout=10
            )
            audio = bytearray()
            async for chunk in speaker.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                    if len(audio) > 4 * 1024 * 1024:
                        raise ValueError("语音片段过大")
            if not audio:
                raise ValueError("语音服务返回了空音频")
            return bytes(audio)

        # Only one request ahead: keep memory and cloud concurrency bounded.
        pending = asyncio.create_task(synthesize(parts[0])) if parts else None
        try:
            for index, _part in enumerate(parts):
                assert pending is not None
                audio = await pending
                pending = (
                    asyncio.create_task(synthesize(parts[index + 1]))
                    if index + 1 < len(parts)
                    else None
                )
                if not self._send(epoch, "queue_audio", {"audio": audio, "reset": index == 0}):
                    return
                if index == 0:
                    self.logger.info(
                        "[DesktopVoice] neural first playable segment: {:.3f}s",
                        time.monotonic() - started,
                    )
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _multimodal(self, text: str, language: str, epoch: int, started: float) -> None:
        timeout = aiohttp.ClientTimeout(total=120, sock_connect=5, sock_read=20)
        url = self.config.tts_url.rstrip("/") + "/api/tts/stream"
        payload = {"text": text, "text_lang": language, "platform": "local.live2d"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            response = await session.post(url, json=payload)
            # Legacy compatibility only on an absent route, never on a synthesis error.
            if response.status == 404:
                response.release()
                response = await session.post(
                    self.config.tts_url.rstrip("/") + "/api/tts-stream", json=payload
                )
            async with response:
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
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2)
