"""
Universal Voice Adapter — Core adapter logic.

Bridges audio capture (ProcTap) and audio output (virtual cable) with
NachoBot Core via ncnk_message Router/WebSocket.

Features:
  - Real-time denoising (DeepFilterNet)
  - Speaker diarization (WeSpeaker + online clustering)
  - Streaming ASR (sherpa-onnx)
"""

import asyncio
import logging
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from config import AdapterConfig
from audio_capture import AudioCapture, MicrophoneCapture
from audio_output import AudioOutput
from audio_pipeline import AudioPipeline
from tts_handler import TTSHandler

_multimodal_adapter_path = Path(__file__).resolve().parents[1] / "NachoBot-Multimodal-Adapter"
if _multimodal_adapter_path.exists() and str(_multimodal_adapter_path) not in sys.path:
    sys.path.insert(0, str(_multimodal_adapter_path))

# 独立的情感预设解析器（不依赖 TTS 模型实例）
try:
    from nachobot_multimodal.utils.emotion_resolver import resolve_emotion_preset_remote
except ImportError:
    resolve_emotion_preset_remote = None

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
    )
except ImportError as exc:
    print(
        "Warning: failed to import ncnk_message from the adjacent NachoBot core: "
        f"{exc}"
    )
    BaseMessageInfo = FormatInfo = GroupInfo = MessageBase = Router = RouteConfig = (
        Seg
    ) = TargetConfig = TemplateInfo = UserInfo = None

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def _mask_urls(text: str) -> str:
    if not text:
        return ""
    return _URL_RE.sub("[link]", text)


# Regex to match kaomoji and special emoticons
_KAOMOJI_RE = re.compile(
    r"[\(\（]"
    r"[^\(\)\（\）]{1,15}"
    r"[\)\）]"
    r"|"
    r"[｡ﾟ✧♪♡☆★●○◎◇◆□■△▲▽▼※→←↑↓]+"
)


def _clean_text_for_tts(text: str) -> str:
    """Clean text for TTS: remove kaomoji, emoticons, and special characters."""
    if not text:
        return ""
    cleaned = _KAOMOJI_RE.sub("", text)
    cleaned = re.sub(r"[～〜♪♡☆★]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[。、！？]{2,}", "。", cleaned)
    return cleaned.strip()


class UniversalVCAdapter:
    """
    Core adapter that connects:
    - AudioCapture (ProcTap) → AudioPipeline → speaker text → NachoBot Core
    - NachoBot Core → reply text → TTS → AudioOutput (virtual cable)
    """

    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger

        # Session identifier for this adapter instance
        self._session_id = f"uvc_{int(time.time())}"

        # Initialize Audio Pipeline (Denoise → VAD → Speaker → ASR)
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

        # Initialize TTS
        self.tts_handler = TTSHandler(logger)

        # Initialize Router (Connection to NachoBot Core)
        self.router = None
        if Router:
            route_config = RouteConfig(
                route_config={
                    "universal_vc": TargetConfig(
                        url=f"ws://{self.config.nachobot.host}:{self.config.nachobot.port}/ws",
                        token=None,
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

    async def _on_speech_result(self, speaker_id: str, speaker_name: str, text: str):
        """Called when pipeline produces a recognized speech result with speaker info."""
        self.logger.info(f"[{speaker_name}] ({speaker_id}): {text}")

        if not self.router:
            return

        processed_text = text
        if self.config.disable_network_search:
            processed_text = _mask_urls(processed_text)

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
                content_format=["text"],
                accept_format=["text", "voice"],
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="text", data=processed_text),
        )

        await self.router.send_message(message)

    async def _on_speech_start(self):
        """Called when user starts speaking - interrupt current TTS playback."""
        self.logger.debug("User speech start detected, interrupting playback")
        await self.audio_output.stop_current()

    async def _on_mic_speech_start(self):
        """Called when mic user starts speaking - stop and pause TTS playback."""
        self.logger.debug("Mic user speech start detected, pausing playback")
        await self.audio_output.stop_and_pause()

    async def _on_mic_speech_end(self):
        """Called when mic user stops speaking - resume TTS playback."""
        self.logger.debug("Mic user speech end detected, resuming playback")
        self.audio_output.resume()

    async def _handle_from_nachobot(self, message: MessageBase) -> None:
        """Handle outgoing messages from NachoBot Core → TTS → Virtual Cable."""
        try:
            text_to_speak = ""

            # Extract segment
            segment = None
            if isinstance(message, dict):
                segment = message.get("message_segment")
            else:
                segment = message.message_segment

            # Flatten segments to text
            if segment:
                if isinstance(segment, dict):
                    if segment.get("type") == "text":
                        text_to_speak = segment.get("data", "")
                elif isinstance(segment, list):
                    for seg in segment:
                        if isinstance(seg, dict):
                            if seg.get("type") == "text":
                                text_to_speak += seg.get("data", "")
                        elif hasattr(seg, "type") and hasattr(seg, "data"):
                            if seg.type == "text":
                                text_to_speak += seg.data
                elif hasattr(segment, "type") and hasattr(segment, "data"):
                    if segment.type == "text":
                        text_to_speak = segment.data

            if not text_to_speak:
                return

            # Strip invisible characters
            text_to_speak = (
                text_to_speak.replace("\u200b", "")
                .replace("\\u200b", "")
                .replace("\\u200B", "")
                .replace("\ufeff", "")
                .strip()
            )

            if not text_to_speak:
                return

            self.logger.info(f"Received from Core: {text_to_speak}")

            # Filter typo correction messages
            if len(text_to_speak) <= 2 and all(
                "\u4e00" <= c <= "\u9fff" for c in text_to_speak
            ):
                self.logger.info(f"Skipping typo correction message: {text_to_speak}")
                return

            # Clean text for TTS
            cleaned_text = _clean_text_for_tts(text_to_speak)
            self.logger.info(f"Cleaned text for TTS: {cleaned_text}")

            if not cleaned_text:
                self.logger.warning("Text became empty after cleaning, skipping TTS.")
                return

            # 分段流式：按句切分，逐句生成并立即送入播放队列
            try:
                from nachobot_multimodal.utils.text_splitter import split_text_for_streaming
                segments = split_text_for_streaming(cleaned_text)
            except ImportError:
                segments = [cleaned_text]

            self.logger.info(f"TTS segment stream: {len(segments)} segments")

            preset_name = None
            if resolve_emotion_preset_remote is not None:
                try:
                    preset_name = await resolve_emotion_preset_remote(cleaned_text)
                except Exception as e:
                    self.logger.error(f"Failed to resolve emotion preset: {e}")

            for idx, seg_text in enumerate(segments):
                self.logger.info(f"Generating segment {idx+1}/{len(segments)}: {seg_text}")
                audio_path = await self.tts_handler.generate_speech(seg_text, preset_name=preset_name, split_method="cut0")
                if audio_path:
                    await self.audio_output.play(audio_path)

        except Exception as e:
            self.logger.exception(f"Error handling message from NachoBot: {e}")
