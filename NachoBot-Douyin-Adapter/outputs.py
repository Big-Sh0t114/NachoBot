from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from loguru import logger
import websockets

from config import Live2DConfig, OutputConfig, TTSConfig


class Live2DBridge:
    def __init__(self, config: Live2DConfig):
        self.config = config

    def _url(self) -> str:
        if not self.config.token:
            return self.config.url
        parts = urlsplit(self.config.url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["token"] = self.config.token
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    async def send(self, event: str, payload: dict) -> bool:
        if not self.config.enabled:
            return False
        envelope = {
            "type": "avatar.command",
            "version": "1.0",
            "event": event,
            "payload": payload,
        }
        try:
            async with websockets.connect(self._url(), open_timeout=3, close_timeout=1) as ws:
                await ws.send(json.dumps(envelope, ensure_ascii=False))
            return True
        except Exception as exc:
            logger.warning("Live2D command failed: {}", exc)
            return False


class ReplyOutput:
    def __init__(self, output: OutputConfig, tts: TTSConfig, live2d: Live2DConfig):
        self.output = output
        self.tts = tts
        self.live2d = Live2DBridge(live2d)
        self._lock = asyncio.Lock()

    async def deliver(
        self, text: str, *, emotion: str | None = None, action: str | None = None
    ) -> None:
        text = re.sub(r"</?(?:ZH|JP|EN)>", "", text, flags=re.IGNORECASE).strip()
        if not text:
            return
        async with self._lock:
            if self.output.console:
                logger.info("AI主播回复：{}", text)
            await asyncio.to_thread(self._write_subtitle, text)
            await self.live2d.send("state", {"state": "start_replying"})
            if emotion:
                await self.live2d.send("emotion", {"emotion": emotion})
            if action:
                await self.live2d.send("action", {"action_id": action.strip().upper()})
            audio = await self._synthesize(text) if self.tts.enabled else b""
            if audio and self.tts.play_local:
                await self.live2d.send("speaking", {"speaking": True})
                await asyncio.to_thread(self._play_wav, audio)
                await self.live2d.send("speaking", {"speaking": False})
            await self.live2d.send("state", {"state": "finish_reply"})

    def _write_subtitle(self, text: str) -> None:
        self.output.subtitle_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.output.subtitle_file.with_suffix(".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(self.output.subtitle_file)

    async def _synthesize(self, text: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=self.tts.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.tts.url,
                    json={"text": text, "platform": "douyin.live"},
                    headers={"Accept": "audio/wav"},
                ) as response:
                    if response.status != 200:
                        detail = (await response.text())[:500]
                        logger.warning("TTS request failed: HTTP {} {}", response.status, detail)
                        return b""
                    return await response.read()
        except Exception as exc:
            logger.warning("TTS unavailable; subtitle output remains active: {}", exc)
            return b""

    @staticmethod
    def _play_wav(audio: bytes) -> None:
        if os.name != "nt":
            logger.warning("Local WAV playback is currently implemented for Windows only")
            return
        import winsound

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                handle.write(audio)
                temp_path = Path(handle.name)
            winsound.PlaySound(str(temp_path), winsound.SND_FILENAME)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
