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

from config import Live2DConfig, NeuralTTSConfig, OutputConfig, TTSConfig


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
    def __init__(
        self,
        output: OutputConfig,
        tts: TTSConfig,
        neural_tts: NeuralTTSConfig,
        live2d: Live2DConfig,
    ):
        self.output = output
        self.tts = tts
        self.neural_tts = neural_tts
        self.live2d = Live2DBridge(live2d)
        self._lock = asyncio.Lock()
        self._audio_version = 0
        self._audio_ready = False
        self._audio_media_type = "audio/mpeg"
        self._audio_source = "none"
        self._voice_profile = "cute"
        self._neural_voice = neural_tts.voice
        self._neural_rate = neural_tts.rate
        self._neural_pitch = neural_tts.pitch
        self._browser_preferred_voice = "Microsoft Yaoyao"

    async def deliver(
        self, text: str, *, emotion: str | None = None, action: str | None = None
    ) -> str:
        text = re.sub(r"</?(?:ZH|JP|EN)>", "", text, flags=re.IGNORECASE).strip()
        if not text:
            return ""
        async with self._lock:
            if self.output.console:
                logger.info("AI 主播口播：{}", text)
            await asyncio.to_thread(self._write_subtitle, text)
            await self.live2d.send("state", {"state": "start_replying"})
            if emotion:
                await self.live2d.send("emotion", {"emotion": emotion})
            if action:
                await self.live2d.send("action", {"action_id": action.strip().upper()})
            audio = await self._synthesize(text) if self.tts.enabled else b""
            self._audio_ready = False
            if audio:
                self._audio_ready = await asyncio.to_thread(self._save_local_audio, audio)
            elif self.neural_tts.enabled:
                # Keep the legacy online engine as an explicit fallback only.
                self._audio_ready = await self._synthesize_neural(text)
            if audio and self.tts.play_local:
                await self.live2d.send("speaking", {"speaking": True})
                await asyncio.to_thread(self._play_wav, audio)
                await self.live2d.send("speaking", {"speaking": False})
            await self.live2d.send("state", {"state": "finish_reply"})
        return text

    def clear_subtitle(self) -> None:
        self._write_subtitle("")

    def set_voice_profile(self, profile: str) -> str:
        profiles = {
            "cute": ("zh-CN-XiaoxiaoNeural", "+0%", "+2Hz", "Microsoft Yaoyao"),
            "mature": ("zh-CN-XiaoyiNeural", "-4%", "-2Hz", "Microsoft Huihui"),
        }
        if profile not in profiles:
            raise ValueError("不支持的声音类型")
        self._voice_profile = profile
        (
            self._neural_voice,
            self._neural_rate,
            self._neural_pitch,
            self._browser_preferred_voice,
        ) = profiles[profile]
        return self._voice_profile

    @property
    def audio_version(self) -> int:
        return self._audio_version

    @property
    def audio_ready(self) -> bool:
        return self._audio_ready and self.output.speech_file.exists()

    @property
    def voice_profile(self) -> str:
        return self._voice_profile

    @property
    def neural_voice(self) -> str:
        return self._neural_voice

    @property
    def browser_preferred_voice(self) -> str:
        return self._browser_preferred_voice

    @property
    def audio_media_type(self) -> str:
        return self._audio_media_type

    @property
    def audio_source(self) -> str:
        return self._audio_source

    def _write_subtitle(self, text: str) -> None:
        self.output.subtitle_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output.subtitle_file.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.output.subtitle_file)

    async def _synthesize(self, text: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=self.tts.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.tts.url,
                    # The local profile is mapped to a Vox voice-design preset
                    # by the multimodal adapter.  No text is sent outside the PC.
                    json={"text": text, "platform": f"local.host.{self._voice_profile}"},
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

    def _save_local_audio(self, audio: bytes) -> bool:
        """Atomically expose a locally generated WAV to the captured browser."""
        if not audio:
            return False
        try:
            self.output.speech_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.output.speech_file.with_name(
                f".{self.output.speech_file.stem}.tmp{self.output.speech_file.suffix}"
            )
            temporary.write_bytes(audio)
            temporary.replace(self.output.speech_file)
            self._audio_media_type = "audio/wav"
            self._audio_source = "local_voxcpm"
            self._audio_version += 1
            return True
        except OSError as exc:
            logger.warning("Could not save local TTS audio: {}", exc)
            return False

    async def _synthesize_neural(self, text: str) -> bool:
        """Save Microsoft neural speech for the captured browser to play."""
        try:
            import edge_tts

            self.output.speech_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.output.speech_file.with_name(
                f".{self.output.speech_file.stem}.tmp{self.output.speech_file.suffix}"
            )
            speaker = edge_tts.Communicate(
                text,
                voice=self._neural_voice,
                rate=self._neural_rate,
                pitch=self._neural_pitch,
                volume=self.neural_tts.volume,
            )
            await speaker.save(str(temporary))
            temporary.replace(self.output.speech_file)
            self._audio_media_type = "audio/mpeg"
            self._audio_source = "edge_tts"
            self._audio_version += 1
            return True
        except Exception as exc:
            logger.warning("Neural TTS unavailable; falling back to browser speech: {}", exc)
            return False

    @staticmethod
    def _play_wav(audio: bytes) -> None:
        if os.name != "nt":
            logger.warning("Local WAV playback is currently implemented for Windows only")
            return
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                handle.write(audio)
                temp_path = Path(handle.name)
            import winsound

            winsound.PlaySound(str(temp_path), winsound.SND_FILENAME)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
