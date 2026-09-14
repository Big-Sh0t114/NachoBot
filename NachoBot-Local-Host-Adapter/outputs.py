from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import wave
from array import array
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
        self._streamed_audio_version = 0
        self._last_stream_first_block_seconds: float | None = None
        self._last_stream_total_seconds: float | None = None
        self._last_stream_segment_count = 0
        self._last_stream_filler_count = 0
        self._audio_ready = False
        self._audio_media_type = "audio/mpeg"
        self._audio_source = "none"
        self._voice_profile = "cute"
        self._neural_voice = neural_tts.voice
        self._neural_rate = neural_tts.rate
        self._neural_pitch = neural_tts.pitch
        self._browser_preferred_voice = "Microsoft Yaoyao"
        self._filler_warmup_lock = asyncio.Lock()
        self._filler_audio: tuple[bytes, int] | None = self._load_filler_cache()

    async def deliver(
        self,
        text: str,
        *,
        emotion: str | None = None,
        action: str | None = None,
        tts_language: str = "auto",
        synthesize_audio: bool = True,
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
            used_sapi = False
            streamed_to_live2d = False
            audio = b""
            self._streamed_audio_version = 0
            if self.tts.enabled and synthesize_audio:
                voxcpm_timeout = max(float(self.tts.timeout_seconds), 1.0)
                try:
                    audio, streamed_to_live2d = await asyncio.wait_for(
                        self._synthesize_preferred(text, tts_language=tts_language),
                        timeout=voxcpm_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Local VoxCPM TTS exceeded {} seconds; using offline SAPI",
                        voxcpm_timeout,
                    )
            if not audio and self.tts.enabled and synthesize_audio:
                audio = await asyncio.to_thread(self._synthesize_sapi, text)
                used_sapi = bool(audio)
            self._audio_ready = False
            self._audio_source = "none"
            if audio:
                self._audio_ready = await asyncio.to_thread(self._save_local_audio, audio)
                if streamed_to_live2d and self._audio_ready:
                    self._streamed_audio_version = self._audio_version
                if used_sapi and self._audio_ready:
                    self._audio_source = "local_sapi"
            elif self.neural_tts.enabled and synthesize_audio:
                # Keep the legacy online engine as an explicit fallback only.
                self._audio_ready = await self._synthesize_neural(text)
            if audio and self.tts.play_local and not streamed_to_live2d:
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
        self._filler_audio = self._load_filler_cache()
        return self._voice_profile

    @property
    def audio_version(self) -> int:
        return self._audio_version

    @property
    def streamed_audio_version(self) -> int:
        return self._streamed_audio_version

    @property
    def last_stream_first_block_seconds(self) -> float | None:
        return self._last_stream_first_block_seconds

    @property
    def last_stream_total_seconds(self) -> float | None:
        return self._last_stream_total_seconds

    @property
    def last_stream_segment_count(self) -> int:
        return self._last_stream_segment_count

    @property
    def last_stream_filler_count(self) -> int:
        return self._last_stream_filler_count

    @property
    def segment_filler_ready(self) -> bool:
        return self._filler_audio is not None

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

    async def _synthesize(self, text: str, *, tts_language: str = "auto") -> bytes:
        timeout = aiohttp.ClientTimeout(total=self.tts.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.tts.url,
                    # The local profile is mapped to a Vox voice-design preset
                    # by the multimodal adapter.  No text is sent outside the PC.
                    json={
                        "text": text,
                        "platform": f"local.host.{self._voice_profile}",
                        "text_lang": tts_language,
                    },
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

    async def _synthesize_preferred(
        self,
        text: str,
        *,
        tts_language: str = "auto",
    ) -> tuple[bytes, bool]:
        if self.live2d.config.enabled:
            streamed = await self._synthesize_streaming(text, tts_language=tts_language)
            if streamed is not None:
                return streamed
        return await self._synthesize(text, tts_language=tts_language), False

    def _streaming_tts_url(self) -> str:
        parts = urlsplit(self.tts.url)
        path = parts.path
        if path.endswith("/api/tts"):
            path = f"{path}-stream"
        else:
            path = "/api/tts-stream"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

    async def _synthesize_streaming(
        self,
        text: str,
        *,
        tts_language: str = "auto",
    ) -> tuple[bytes, bool] | None:
        """Generate punctuation-delimited phrases ahead of the Live2D playback queue.

        VoxCPM is faster and more stable when each phrase is allowed to finish before
        it is decoded by PyGame.  While one phrase is playing, the next phrase is
        synthesized.  If the producer falls behind, a short silent hesitation and at
        most one cached, same-voice filler keep the queue from underrunning.
        """
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        self._last_stream_first_block_seconds = None
        self._last_stream_total_seconds = None
        self._last_stream_filler_count = 0
        segments = (
            self._split_speech_segments(
                text,
                self.tts.segment_min_chars,
                self.tts.segment_target_chars,
            )
            if self.tts.segmented_playback
            else [text]
        )
        self._last_stream_segment_count = len(segments)
        if not segments:
            return None

        try:
            first_result = await self._request_stream_pcm(
                segments[0],
                tts_language=tts_language,
            )
            if first_result is None:
                return None

            first_pcm, sample_rate = first_result
            timeline = bytearray(first_pcm)
            reset = True
            sent_any = False
            stream_delivery_ok = True

            async def queue_piece(piece: bytes) -> bool:
                nonlocal reset, sent_any, stream_delivery_ok
                if not piece or not stream_delivery_ok:
                    return False
                sent = await self._send_stream_block(
                    piece,
                    sample_rate=sample_rate,
                    reset=reset,
                )
                if sent:
                    sent_any = True
                    reset = False
                    if self._last_stream_first_block_seconds is None:
                        self._last_stream_first_block_seconds = round(
                            loop.time() - started_at,
                            3,
                        )
                    return True
                stream_delivery_ok = False
                if sent_any:
                    await self.live2d.send("stop_audio", {})
                return False

            await queue_piece(first_pcm)
            playback_deadline = loop.time() + self._pcm_duration(first_pcm, sample_rate)
            filler_used = False
            queue_lead_seconds = 0.12

            for index, segment in enumerate(segments[1:], start=2):
                next_task = asyncio.create_task(
                    self._request_stream_pcm(segment, tts_language=tts_language),
                    name=f"voxcpm-segment-{index}",
                )

                if stream_delivery_ok:
                    ready = await self._wait_for_task_until(
                        next_task,
                        playback_deadline - queue_lead_seconds,
                    )
                    paused_seconds = 0.0
                    while not ready and paused_seconds < self.tts.segment_wait_seconds:
                        pause_seconds = min(
                            self.tts.segment_pause_step_seconds,
                            self.tts.segment_wait_seconds - paused_seconds,
                        )
                        pause_pcm = self._silence_pcm(
                            sample_rate,
                            pause_seconds,
                        )
                        timeline.extend(pause_pcm)
                        await queue_piece(pause_pcm)
                        playback_deadline += self._pcm_duration(pause_pcm, sample_rate)
                        paused_seconds += pause_seconds
                        ready = await self._wait_for_task_until(
                            next_task,
                            playback_deadline - queue_lead_seconds,
                        )
                    if paused_seconds:
                        logger.info(
                            "VoxCPM segment {}/{} adaptive wait={:.2f}s, ready={}",
                            index,
                            len(segments),
                            paused_seconds,
                            ready,
                        )

                    filler = self._filler_audio
                    if (
                        not ready
                        and not filler_used
                        and self.tts.segment_filler_enabled
                        and filler is not None
                        and filler[1] == sample_rate
                    ):
                        filler_pcm = filler[0]
                        timeline.extend(filler_pcm)
                        await queue_piece(filler_pcm)
                        playback_deadline += self._pcm_duration(filler_pcm, sample_rate)
                        filler_used = True
                        self._last_stream_filler_count = 1
                        logger.info(
                            "VoxCPM segment {}/{} is not ready after the pause; "
                            "queued same-voice filler",
                            index,
                            len(segments),
                        )

                    # A slow segment can outlast the adaptive pause plus filler.
                    # Extend the queue with short silence only; never repeat filler.
                    guard_seconds = max(
                        0.2,
                        min(0.4, self.tts.segment_pause_step_seconds),
                    )
                    while not await self._wait_for_task_until(
                        next_task,
                        playback_deadline - queue_lead_seconds,
                    ):
                        guard_pcm = self._silence_pcm(sample_rate, guard_seconds)
                        timeline.extend(guard_pcm)
                        await queue_piece(guard_pcm)
                        playback_deadline += self._pcm_duration(guard_pcm, sample_rate)

                next_result = await next_task
                if next_result is None:
                    logger.warning(
                        "VoxCPM segment {}/{} failed; retrying the complete WAV path",
                        index,
                        len(segments),
                    )
                    return None
                next_pcm, next_sample_rate = next_result
                if next_sample_rate != sample_rate:
                    logger.warning(
                        "VoxCPM sample rate changed from {} to {}; aborting segmented playback",
                        sample_rate,
                        next_sample_rate,
                    )
                    return None
                timeline.extend(next_pcm)
                await queue_piece(next_pcm)
                playback_deadline = max(playback_deadline, loop.time()) + self._pcm_duration(
                    next_pcm,
                    sample_rate,
                )

            self._last_stream_total_seconds = round(loop.time() - started_at, 3)
            logger.info(
                "VoxCPM segmented stream: segments={}, filler={}, first={}s, "
                "generation={}s, live2d={}",
                len(segments),
                self._last_stream_filler_count,
                self._last_stream_first_block_seconds,
                self._last_stream_total_seconds,
                sent_any and stream_delivery_ok,
            )
            return (
                self._pcm_to_wav(bytes(timeline), sample_rate),
                sent_any and stream_delivery_ok,
            )
        except Exception as exc:
            logger.warning("Streaming TTS failed; retrying complete WAV path: {}", exc)
            return None

    async def _request_stream_pcm(
        self,
        text: str,
        *,
        tts_language: str = "auto",
    ) -> tuple[bytes, int] | None:
        timeout = aiohttp.ClientTimeout(total=self.tts.timeout_seconds)
        payload = {
            "text": text,
            "platform": f"local.host.{self._voice_profile}",
            "text_lang": tts_language,
            # Punctuation was already split by this adapter.  Prevent VoxCPM
            # from starting a second, hidden segmentation pass.
            "split_method": "cut0",
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self._streaming_tts_url(),
                json=payload,
                headers={"Accept": "application/octet-stream"},
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:500]
                    logger.warning(
                        "Streaming TTS unavailable: HTTP {} {}",
                        response.status,
                        detail,
                    )
                    return None
                if response.headers.get("X-Sample-Width", "2") != "2":
                    logger.warning("Streaming TTS returned an unsupported sample width")
                    return None
                if response.headers.get("X-Channels", "1") != "1":
                    logger.warning("Streaming TTS returned unsupported channel count")
                    return None
                sample_rate = int(response.headers.get("X-Sample-Rate", "48000"))
                pcm = bytearray()
                async for chunk in response.content.iter_any():
                    if chunk:
                        pcm.extend(chunk)
                if not pcm:
                    return None
                return self._trim_pcm_silence(bytes(pcm), sample_rate), sample_rate

    async def warm_segment_filler(self) -> bool:
        """Prepare one same-profile filler clip without playing it."""
        if (
            not self.tts.enabled
            or not self.live2d.config.enabled
            or not self.tts.segmented_playback
            or not self.tts.segment_filler_enabled
        ):
            return False
        async with self._filler_warmup_lock:
            if self._filler_audio is not None:
                return True
            temporary: Path | None = None
            try:
                result = await self._request_stream_pcm(self.tts.segment_filler_text)
                if result is None:
                    return False
                pcm, sample_rate = result
                cache_path = self._filler_cache_path()
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_name(f".{cache_path.stem}.tmp.wav")
                temporary.write_bytes(self._pcm_to_wav(pcm, sample_rate))
                temporary.replace(cache_path)
                self._filler_audio = (pcm, sample_rate)
                logger.info(
                    "Prepared same-voice TTS filler: {} ({:.2f}s)",
                    cache_path,
                    self._pcm_duration(pcm, sample_rate),
                )
                return True
            except Exception as exc:
                logger.warning("Could not prepare the same-voice TTS filler: {}", exc)
                return False
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    @staticmethod
    async def _wait_for_task_until(task: asyncio.Task, deadline: float) -> bool:
        if task.done():
            return True
        delay = deadline - asyncio.get_running_loop().time()
        if delay <= 0:
            return task.done()
        done, _ = await asyncio.wait({task}, timeout=delay)
        return bool(done)

    @staticmethod
    def _split_speech_segments(
        text: str,
        min_chars: int = 4,
        target_chars: int = 16,
    ) -> list[str]:
        text = text.strip()
        if not text:
            return []
        raw_parts = re.findall(r"[^，,。！？!?；;：:\n]+(?:[，,。！？!?；;：:\n]+|$)", text)
        raw_parts = [part.strip() for part in raw_parts if part.strip()]
        if len(raw_parts) <= 1:
            return [text]

        def spoken_length(value: str) -> int:
            return len(re.sub(r"[\s，,。！？!?；;：:]", "", value))

        clauses: list[str] = []
        pending = ""
        for part in raw_parts:
            pending += part
            if spoken_length(pending) < min_chars and not re.search(r"[。！？!?]$", pending):
                continue
            clauses.append(pending)
            pending = ""
        if pending:
            if clauses:
                clauses[-1] += pending
            else:
                clauses.append(pending)
        if len(clauses) <= 1:
            return clauses or [text]

        # Keep the first clause short for fast first audio.  Merge later comma
        # clauses into medium semantic chunks to avoid paying model startup
        # overhead for every comma.
        segments = [clauses[0]]
        grouped = ""
        for clause in clauses[1:]:
            grouped += clause
            if spoken_length(grouped) >= target_chars or re.search(r"[。！？!?]$", grouped):
                segments.append(grouped)
                grouped = ""
        if grouped:
            if spoken_length(grouped) < min_chars:
                segments[-1] += grouped
            else:
                segments.append(grouped)
        return segments or [text]

    @staticmethod
    def _pcm_duration(pcm: bytes, sample_rate: int) -> float:
        if sample_rate <= 0:
            return 0.0
        return len(pcm) / float(sample_rate * 2)

    @staticmethod
    def _silence_pcm(sample_rate: int, seconds: float) -> bytes:
        frame_count = max(1, int(max(0.0, seconds) * sample_rate))
        return b"\x00\x00" * frame_count

    @staticmethod
    def _trim_pcm_silence(
        pcm: bytes,
        sample_rate: int,
        *,
        threshold: int = 350,
        leading_padding_seconds: float = 0.08,
        trailing_padding_seconds: float = 0.14,
    ) -> bytes:
        """Remove model-added dead air without changing speech speed or pitch."""
        if len(pcm) < 4 or sample_rate <= 0:
            return pcm
        if len(pcm) % 2:
            pcm = pcm[:-1]
        samples = array("h")
        samples.frombytes(pcm)
        window = max(1, int(sample_rate * 0.02))
        threshold_energy = threshold * threshold
        first_active: int | None = None
        last_active = 0
        for start in range(0, len(samples), window):
            stop = min(len(samples), start + window)
            count = stop - start
            energy = sum(int(value) * int(value) for value in samples[start:stop])
            if energy >= threshold_energy * count:
                if first_active is None:
                    first_active = start
                last_active = stop
        if first_active is None:
            return pcm
        keep_start = max(
            0,
            first_active - int(sample_rate * leading_padding_seconds),
        )
        keep_stop = min(
            len(samples),
            last_active + int(sample_rate * trailing_padding_seconds),
        )
        if keep_start == 0 and keep_stop == len(samples):
            return pcm
        return samples[keep_start:keep_stop].tobytes()

    def _filler_cache_path(self) -> Path:
        profile = re.sub(r"[^a-z0-9_-]+", "_", self._voice_profile.casefold())
        text_key = hashlib.sha1(
            f"trim-v1:{self.tts.segment_filler_text}".encode("utf-8")
        ).hexdigest()[:8]
        return self.output.speech_file.parent / f"tts_segment_filler_{profile}_{text_key}.wav"

    def _load_filler_cache(self) -> tuple[bytes, int] | None:
        cache_path = self._filler_cache_path()
        if not cache_path.is_file():
            return None
        try:
            with wave.open(str(cache_path), "rb") as wav_file:
                if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                    return None
                sample_rate = wav_file.getframerate()
                pcm = wav_file.readframes(wav_file.getnframes())
            pcm = self._trim_pcm_silence(pcm, sample_rate)
            if pcm and sample_rate > 0:
                return pcm, sample_rate
        except (OSError, EOFError, wave.Error) as exc:
            logger.warning("Could not load cached TTS filler {}: {}", cache_path, exc)
        return None

    async def _send_stream_block(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        reset: bool,
    ) -> bool:
        wav_data = self._pcm_to_wav(pcm, sample_rate)
        return await self.live2d.send(
            "queue_audio",
            {
                "format": "wav",
                "audio_base64": base64.b64encode(wav_data).decode("ascii"),
                "reset": reset,
            },
        )

    @staticmethod
    def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
        return output.getvalue()

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
    def _synthesize_sapi(text: str) -> bytes:
        """Use the installed Windows voice as a local, offline last resort."""
        if os.name != "nt":
            return b""

        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                path = Path(handle.name)

            def quote(value: str) -> str:
                return value.replace("'", "''")

            script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{ $synth.SelectVoice('Microsoft Huihui Desktop') }} catch {{}}
$synth.SetOutputToWaveFile('{quote(str(path))}')
try {{ $synth.Speak('{quote(text)}') }} finally {{ $synth.Dispose() }}
"""
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            powershell = (
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            command = str(powershell) if powershell.is_file() else "powershell.exe"
            subprocess.run(
                [
                    command,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                check=True,
                capture_output=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            audio = path.read_bytes()
            if audio.startswith(b"RIFF") and b"WAVE" in audio[:16]:
                logger.info("Local VoxCPM TTS unavailable; used offline Windows SAPI voice")
                return audio
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Offline Windows SAPI TTS unavailable: {}", exc)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        return b""

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
