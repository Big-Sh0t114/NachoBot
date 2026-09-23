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
import logging
import re
import sys
import time
import uuid
from pathlib import Path

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

class UniversalVCAdapter:
    """
    Core adapter that connects:
    - AudioCapture (ProcTap) → AudioPipeline → voice segment → Core
    - NachoBot Core → Core-produced voice → AudioOutput (virtual cable)
    """

    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger

        # Session identifier for this adapter instance
        self._session_id = f"uvc_{int(time.time())}"

        # Initialize Audio Pipeline (Denoise → VAD → Speaker → Core voice)
        self.pipeline = AudioPipeline(
            config=config,
            logger=logger,
            on_result=self._on_speech_result,
            on_speech_start=self._on_speech_start,
            on_mic_speech_start=self._on_mic_speech_start,
            on_mic_speech_end=self._on_mic_speech_end,
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
        tasks = []

        # Initialize audio output device
        self.audio_output.initialize()

        # Start audio capture + pipeline
        loop = asyncio.get_running_loop()
        self.pipeline.set_loop(loop)
        await self.audio_capture.start(loop)
        
        if self.mic_capture:
            await self.mic_capture.start(loop)

        # Start Router (WebSocket to NachoBot Core)
        if self.router:
            tasks.append(asyncio.create_task(self.router.run()))

        self.logger.info("Universal Voice Adapter is running!")
        self.logger.info(f"Session ID: {self._session_id}")
        self.logger.info(f"Platform: universal_vc")

        try:
            if tasks:
                await asyncio.gather(*tasks)
            else:
                while True:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            self.logger.info("Adapter cancelled, cleaning up...")
        except Exception as e:
            self.logger.exception(f"Adapter error: {e}")

    async def stop(self):
        """Stop all components gracefully."""
        self.logger.info("Stopping Universal Voice Adapter...")
        await self.audio_capture.stop()
        if self.mic_capture:
            await self.mic_capture.stop()
        if self.audio_output:
            await self.audio_output.stop()
        await asyncio.sleep(0.5)

    def _inject_variables(self, template: str, variables: dict) -> str:
        """Inject variables into template, preserving undefined placeholders."""
        if not template or not variables:
            return template

        def replace(match):
            key = match.group(1)
            return variables.get(key, match.group(0))

        return re.sub(r"\{(\w+)\}", replace, template)

    async def _on_speech_result(self, speaker_id: str, speaker_name: str, voice_data: str):
        """Send one finalized voice segment to Core for perception."""
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
            },
            "voice_format": {
                "mime_type": "audio/wav",
                "sample_rate": self.pipeline.TARGET_SR,
                "channels": 1,
            },
        }

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

            for voice_segment in self._voice_segments(segment):
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

    @staticmethod
    def _voice_segments(segment):
        """Yield only Core-produced audio, including nested seglists."""
        if isinstance(segment, dict):
            seg_type = segment.get("type")
            data = segment.get("data")
            if seg_type == "seglist" and isinstance(data, list):
                for child in data:
                    yield from UniversalVCAdapter._voice_segments(child)
            elif seg_type in {"voice", "voice_stream"} and data:
                if isinstance(data, dict):
                    data = data.get("audio_base64") or data.get("audio")
                if data:
                    yield data
            return
        if isinstance(segment, list):
            for child in segment:
                yield from UniversalVCAdapter._voice_segments(child)
            return
        if hasattr(segment, "type") and hasattr(segment, "data"):
            if segment.type == "seglist" and isinstance(segment.data, list):
                for child in segment.data:
                    yield from UniversalVCAdapter._voice_segments(child)
            elif segment.type in {"voice", "voice_stream"} and segment.data:
                data = segment.data
                if isinstance(data, dict):
                    data = data.get("audio_base64") or data.get("audio")
                if data:
                    yield data
