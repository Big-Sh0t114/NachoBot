"""
Universal Voice Adapter — Core adapter logic.

Bridges audio capture (ProcTap) and audio output (virtual cable) with
NachoBot Core via ncnk_message Router/WebSocket.

Features:
  - Real-time denoising (DeepFilterNet)
  - Speaker diarization (WeSpeaker + online clustering)
  - Core-owned multimodal perception
"""

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

import aiohttp

from config import AdapterConfig
from audio_capture import AudioCapture, MicrophoneCapture
from audio_output import AudioOutput
from audio_pipeline import AudioPipeline
from voice_codec import write_wav_base64

# Add NachoBot path for ncnk_message module
_root_dir = Path(__file__).resolve().parents[1]
_nachobot_path = _root_dir / "NachoBot"
if _nachobot_path.exists() and str(_nachobot_path) not in sys.path:
    sys.path.insert(0, str(_nachobot_path))

try:
    from ncnk_message import (
        BaseMessageInfo,
        FormatInfo,
        GroupInfo,
        MessageBase,
        Router,
        RouteConfig,
        Seg,
        TargetConfig,
        TemplateInfo,
        UserInfo,
        get_core_token_from_env,
    )
except ImportError as exc:
    print(
        "Warning: failed to import ncnk_message from the adjacent NachoBot core: "
        f"{exc}"
    )
    BaseMessageInfo = FormatInfo = GroupInfo = MessageBase = Router = RouteConfig = (
        Seg
    ) = TargetConfig = TemplateInfo = UserInfo = None


class CoreAudioStreamClient:
    """Persistent HTTP transport for Core-owned real-time audio ASR."""

    MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(self, host: str, port: int, token: str = ""):
        host = str(host).strip()
        if host.startswith("[") and host.endswith("]"):
            authority = host
        elif ":" in host:
            authority = f"[{host}]"
        else:
            authority = host
        self.base_url = f"http://{authority}:{int(port)}"
        self.token = str(token or "").strip()
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=15.0, connect=3.0, sock_read=10.0
                ),
                headers=headers,
                trust_env=False,
                connector=aiohttp.TCPConnector(limit=4),
            )
        return self._session

    async def _post(self, operation: str, payload: dict, *, expect_json: bool = True):
        session = await self._ensure_session()
        url = f"{self.base_url}/api/multimodal/audio/stream/{operation}"
        async with session.post(url, json=payload, allow_redirects=False) as response:
            if 300 <= response.status < 400 or response.status >= 400:
                raise RuntimeError(f"Core audio stream HTTP status {response.status}")
            if not expect_json:
                return None
            body = await response.content.read(self.MAX_RESPONSE_BYTES + 1)
            if len(body) > self.MAX_RESPONSE_BYTES:
                raise ValueError("Core audio stream response exceeded size limit")
            try:
                result = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Core audio stream response was not valid JSON") from exc
            if not isinstance(result, dict):
                raise ValueError("Core audio stream response was not an object")
            return result

    async def start_stream(self, *, sample_rate: int, channels: int) -> str:
        result = await self._post(
            "start", {"sample_rate": sample_rate, "channels": channels}
        )
        stream_id = result.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id.strip() or len(stream_id) > 256:
            raise ValueError("Core returned an invalid stream ID")
        return stream_id

    async def send_chunk(self, stream_id: str, seq: int, pcm: bytes) -> None:
        result = await self._post(
            "chunk",
            {
                "stream_id": stream_id,
                "seq": seq,
                "pcm_base64": base64.b64encode(pcm).decode("ascii"),
            },
        )
        response_seq = result.get("seq")
        if (
            isinstance(response_seq, bool)
            or not isinstance(response_seq, int)
            or response_seq != seq
        ):
            raise ValueError("Core audio stream returned a mismatched sequence")

    async def finish_stream(self, stream_id: str) -> dict:
        result = await self._post("finish", {"stream_id": stream_id})
        if not isinstance(result.get("text"), str):
            raise ValueError("Core audio stream finish response omitted text")
        result_id = result.get("result_id")
        if result_id is not None and (
            not isinstance(result_id, str) or len(result_id) > 256
        ):
            raise ValueError("Core audio stream returned an invalid result ID")
        return result

    async def abort_stream(self, stream_id: str) -> None:
        if self._session is None or self._session.closed:
            return
        await self._post("abort", {"stream_id": stream_id}, expect_json=False)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

class UniversalVCAdapter:
    """
    Core adapter that connects:
    - AudioCapture (ProcTap) → AudioPipeline → voice segment → Core
    - NachoBot Core → Core-produced voice → AudioOutput (virtual cable)
    """

    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self._stop_lock = asyncio.Lock()
        self._stopped = False
        self._stop_event = asyncio.Event()
        self._router_task = None

        # Session identifier for this adapter instance
        self._session_id = f"uvc_{int(time.time())}"

        self.core_stream_client = None
        if Router:
            self.core_stream_client = CoreAudioStreamClient(
                host=self.config.nachobot.host,
                port=self.config.nachobot.port,
                token=os.environ.get("NACHOBOT_CORE_TOKEN", ""),
            )

        # Initialize Audio Pipeline (Denoise → VAD → Speaker → Core voice)
        self.pipeline = AudioPipeline(
            config=config,
            logger=logger,
            on_result=self._on_speech_result,
            on_speech_start=self._on_speech_start,
            on_mic_speech_start=self._on_mic_speech_start,
            on_mic_speech_end=self._on_mic_speech_end,
            stream_client=self.core_stream_client,
        )

        # Initialize Audio Capture (feeds raw frames to pipeline)
        self.audio_capture = AudioCapture(
            capture_config=config.capture,
            logger=logger,
            on_frame=self.pipeline.process_frame,
        )

        # Initialize Microphone Capture (feeds raw frames to mic pipeline)
        self.mic_capture = None
        if config.microphone.enabled:
            self.mic_capture = MicrophoneCapture(
                config=config.microphone,
                logger=logger,
                on_frame=self.pipeline.process_mic_frame,
            )

        # Initialize Audio Output
        self.audio_output = AudioOutput(
            config=config.output,
            logger=logger,
        )

        # Initialize Router (Connection to NachoBot Core)
        self.router = None
        if Router:
            route_config = RouteConfig(
                route_config={
                    "universal_vc": TargetConfig(
                        url=f"ws://{self.config.nachobot.host}:{self.config.nachobot.port}/ws",
                        token=get_core_token_from_env(),
                    )
                }
            )
            self.router = Router(route_config, custom_logger=logger)
            self.router.register_class_handler(self._handle_from_nachobot)
        else:
            self.logger.error("Router not initialized due to missing dependencies.")

    async def run(self):
        """Start all components and run the adapter."""
        try:
            if self._stopped:
                return
            self.audio_output.initialize()

            # Start capture only after the loop-owned stream sender is ready.
            loop = asyncio.get_running_loop()
            self.pipeline.set_loop(loop)
            await self.audio_capture.start(loop)
            if self.mic_capture:
                await self.mic_capture.start(loop)

            if self.router:
                self._router_task = asyncio.create_task(self.router.run())

            self.logger.info("Universal Voice Adapter is running!")
            self.logger.info(f"Session ID: {self._session_id}")
            self.logger.info("Platform: universal_vc")

            if self._router_task:
                await self._router_task
            else:
                await self._stop_event.wait()
        except asyncio.CancelledError:
            self.logger.info("Adapter cancelled, cleaning up...")
        except Exception as e:
            self.logger.exception(f"Adapter error: {e}")
        finally:
            await self.stop()

    async def stop(self):
        """Stop all components gracefully."""
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop_event.set()
            self.logger.info("Stopping Universal Voice Adapter...")

            for component in (self.audio_capture, self.mic_capture):
                if component is None:
                    continue
                try:
                    await component.stop()
                except Exception as exc:
                    self.logger.warning(
                        "Audio capture shutdown failed (%s)", type(exc).__name__
                    )

            if self._router_task is not None and not self._router_task.done():
                self._router_task.cancel()
                try:
                    await self._router_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self.logger.debug(
                        "Router shutdown completed with (%s)", type(exc).__name__
                    )

            try:
                await self.pipeline.stop_streaming()
            except Exception as exc:
                self.logger.warning(
                    "Core stream shutdown failed (%s)", type(exc).__name__
                )

            if self.audio_output:
                try:
                    await self.audio_output.stop()
                except Exception as exc:
                    self.logger.warning(
                        "Audio output shutdown failed (%s)", type(exc).__name__
                    )

            if self.core_stream_client is not None:
                try:
                    await self.core_stream_client.close()
                except Exception as exc:
                    self.logger.warning(
                        "Core HTTP client shutdown failed (%s)", type(exc).__name__
                    )

    def _inject_variables(self, template: str, variables: dict) -> str:
        """Inject variables into template, preserving undefined placeholders."""
        if not template or not variables:
            return template

        def replace(match):
            key = match.group(1)
            return variables.get(key, match.group(0))

        return re.sub(r"\{(\w+)\}", replace, template)

    async def _on_speech_result(
        self,
        speaker_id: str,
        speaker_name: str,
        voice_data: str,
        precomputed_asr_result_id: str | None = None,
        precomputed_asr_text: str | None = None,
    ):
        """Send one finalized voice segment to Core for perception.

        ``precomputed_asr_text`` accompanies the Core-issued receipt for
        adapters that can consume the confirmed transcript locally. UniversalVC
        still sends the original voice payload and receipt to Core.
        """
        self.logger.info(
            "[%s] (%s): finalized voice segment (%d base64 chars)",
            speaker_name,
            speaker_id,
            len(voice_data or ""),
        )

        if not self.router or not voice_data:
            return

        additional_config = {
            "disable_tools": True,
            "runtime_capabilities": {
                "schema_version": 1,
                "planner_bypass": True,
                "relation_inference": False,
                "expression_selection": False,
                "memory_retrieval": False,
                "knowledge_retrieval": False,
                "tool_mode": "disabled",
                "web_search_mode": "disabled",
                # VC replies stay one structured transport message so Core can
                # project ``reply`` for display and synthesize only the
                # explicitly requested ``tts_text`` field.
                "reply_delivery": "json_envelope",
                "tts_language": "zh",
                "voice_stream": True,
            },
            "voice_format": {
                "mime_type": "audio/wav",
                "sample_rate": self.pipeline.TARGET_SR,
                "channels": 1,
            },
        }
        if (
            isinstance(precomputed_asr_result_id, str)
            and precomputed_asr_result_id.strip()
            and len(precomputed_asr_result_id) <= 256
        ):
            additional_config["precomputed_asr_result_id"] = (
                precomputed_asr_result_id
            )

        # Custom Prompts
        template_info = None
        if self.config.prompts.planner_prompt or self.config.prompts.replyer_prompt:
            if TemplateInfo:
                template_items = {}
                variables = self.config.prompts.variables.copy()
                if "application_name" not in variables:
                    variables["application_name"] = self.audio_capture.get_application_name()

                if self.config.prompts.planner_prompt:
                    p_prompt = self.config.prompts.planner_prompt
                    template_items["planner_prompt"] = self._inject_variables(
                        p_prompt, variables
                    )

                if self.config.prompts.replyer_prompt:
                    r_prompt = self.config.prompts.replyer_prompt
                    template_items["replyer_prompt"] = self._inject_variables(
                        r_prompt, variables
                    )

                template_info = TemplateInfo(
                    template_items=template_items,
                    template_name=f"universal_vc_{self._session_id}",
                    template_default=False,
                )

        message_info = BaseMessageInfo(
            platform="universal_vc",
            message_id=str(uuid.uuid4()),
            time=time.time(),
            user_info=UserInfo(
                platform="universal_vc",
                user_id=speaker_id,
                user_nickname=speaker_name,
            ),
            group_info=GroupInfo(
                platform="universal_vc",
                group_id=self._session_id,
                group_name=f"Universal VC Session",
            ),
            format_info=FormatInfo(
                content_format=["voice"],
                accept_format=["text", "voice", "tts_text"],
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="voice", data=voice_data),
        )

        await self.router.send_message(message)

    async def _on_speech_start(self):
        """Interrupt current Core-produced audio when a user speaks."""
        self.logger.debug("User speech start detected, interrupting playback")
        await self.audio_output.stop_current()

    async def _on_mic_speech_start(self):
        """Pause Core-produced audio while the owner microphone speaks."""
        self.logger.debug("Mic user speech start detected, pausing playback")
        await self.audio_output.stop_and_pause()

    async def _on_mic_speech_end(self):
        """Resume queued Core-produced audio after microphone speech."""
        self.logger.debug("Mic user speech end detected, resuming playback")
        self.audio_output.resume()

    async def _handle_from_nachobot(self, message: MessageBase) -> None:
        """Play only Core-produced voice segments on the virtual cable."""
        try:
            # Extract segment
            segment = None
            if isinstance(message, dict):
                segment = message.get("message_segment")
            else:
                segment = message.message_segment

            for segment_type, segment_data in self._audio_segments(segment):
                if segment_type == "voice_stream":
                    await self._handle_voice_stream_event(segment_data)
                    continue

                voice_segment = segment_data
                if isinstance(voice_segment, dict):
                    voice_segment = voice_segment.get("audio_base64") or voice_segment.get("audio")
                if not voice_segment:
                    continue
                try:
                    audio_path = write_wav_base64(voice_segment)
                except (ValueError, TypeError) as exc:
                    self.logger.warning(
                        "Dropping invalid Core voice segment: %s", type(exc).__name__
                    )
                    continue
                await self.audio_output.play(audio_path)

        except Exception as e:
            self.logger.exception(f"Error handling message from NachoBot: {e}")

    async def _handle_voice_stream_event(self, data) -> None:
        """Route Core PCM stream events to the persistent output stream."""
        if not isinstance(data, dict):
            self.logger.warning("Dropping invalid Core voice stream event data")
            return

        event = data.get("event")
        handler = {
            "start": self.audio_output.start_voice_stream,
            "chunk": self.audio_output.write_voice_stream_chunk,
            "end": self.audio_output.end_voice_stream,
            "abort": self.audio_output.abort_voice_stream,
        }.get(event)
        if handler is None:
            self.logger.warning("Dropping unknown Core voice stream event")
            return
        try:
            await handler(data)
        except (ValueError, TypeError, BufferError) as exc:
            self.logger.warning(
                "Dropping invalid Core voice stream event %s (%s)",
                event,
                type(exc).__name__,
            )

    @staticmethod
    def _voice_segments(segment):
        """Yield buffered Core voice payloads, including nested seglists."""
        for segment_type, data in UniversalVCAdapter._audio_segments(segment):
            if segment_type == "voice":
                yield data

    @staticmethod
    def _audio_segments(segment):
        """Yield buffered voice payloads and explicit stream events in order."""
        if isinstance(segment, dict):
            seg_type = segment.get("type")
            data = segment.get("data")
            if seg_type == "seglist" and isinstance(data, list):
                for child in data:
                    yield from UniversalVCAdapter._audio_segments(child)
            elif seg_type in {"voice", "voice_stream"}:
                yield seg_type, data
            return
        if isinstance(segment, list):
            for child in segment:
                yield from UniversalVCAdapter._audio_segments(child)
            return
        if hasattr(segment, "type") and hasattr(segment, "data"):
            if segment.type == "seglist" and isinstance(segment.data, list):
                for child in segment.data:
                    yield from UniversalVCAdapter._audio_segments(child)
            elif segment.type in {"voice", "voice_stream"}:
                yield segment.type, segment.data
