import asyncio
import base64
import binascii
import logging
import sys
import uuid
import time
import re
from pathlib import Path

from config import AdapterConfig
from discord_client import NachoDiscordBot
from voice_handler import VoiceHandler
from voice_codec import write_wav_base64

# Add NachoBot path for ncnk_message module (Standard NachoBot Architecture)
# Assuming directory structure:
# root/
#   NachoBot/
#   NachoBot-DiscordVC-Adapter/
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
except ImportError:
    # Fallback if NachoBot not found (Logic won't work but prevents import error crash)
    print(
        "Warning: ncnk_message not found. Please ensure NachoBot is adjacent to this folder."
    )
    BaseMessageInfo = FormatInfo = GroupInfo = MessageBase = Router = RouteConfig = (
        Seg
    ) = TargetConfig = TemplateInfo = UserInfo = None

class DiscordAdapter:
    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger

        # Initialize Voice & Bot
        self.voice_handler = VoiceHandler(config, logger)

        self.bot = NachoDiscordBot(config, self.voice_handler, logger)
        self.bot.set_speech_callback(self.handle_speech_recognized)

        # Initialize Router (Connection to NachoBot Core)
        self.router = None
        if Router:
            route_config = RouteConfig(
                route_config={
                    "discord_vc": TargetConfig(
                        url=f"ws://{self.config.nachobot.host}:{self.config.nachobot.port}/ws",
                        token=get_core_token_from_env(),
                    )
                }
            )
            self.router = Router(route_config, custom_logger=logger)
            # Register handler for messages FROM NachoBot
            self.router.register_class_handler(self.handle_from_nachobot)
        else:
            self.logger.error("Router not initialized due to missing dependencies.")

    async def stop(self):
        self.logger.info("Stopping Discord Adapter...")
        if self.router:
            # Router typically runs in a loop, we might just cancel tasks if no explicit stop
            pass

        if self.bot:
            await self.bot.close()
            self.logger.info("Discord Bot closed.")

        # Wait a bit for background threads (like heartbeats) to clean up
        # This prevents "RuntimeError: Event loop is closed" on Windows
        await asyncio.sleep(1.0)

    async def run(self):
        tasks = []

        # Start Router (WebSocket)
        if self.router:
            tasks.append(asyncio.create_task(self.router.run()))

        # Start Discord Bot
        # bot.start() is async, we wrap it
        tasks.append(asyncio.create_task(self.bot.start(self.config.discord.token)))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            self.logger.info("Adapter run cancelled, stopping bot...")
            if self.bot and not self.bot.is_closed():
                await self.bot.close()

            # Allow time for background threads (heartbeat) to notice the closed connection
            # BEFORE we cancel all tasks and exit the loop
            self.logger.info("Waiting for background threads to cleanup...")
            await asyncio.sleep(2.0)

            self.logger.info("Cleaning up tasks...")
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception as e:
            self.logger.error(f"Error in adapter run: {e}")
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _inject_variables(self, template: str, variables: dict) -> str:
        """Inject variables into template, preserving undefined placeholders."""
        if not template or not variables:
            return template

        def replace(match):
            key = match.group(1)
            return variables.get(key, match.group(0))

        return re.sub(r"\{(\w+)\}", replace, template)

    async def handle_speech_recognized(
        self,
        guild_id: int,
        user_id: int,
        voice_data: str,
        user_name: str = None,
        precomputed_asr_result_id: str | None = None,
    ):
        """Send one finalized WAV voice segment to Core for perception."""
        self.logger.info(
            "Voice segment from %s in %s (base64 chars=%d)",
            user_name or user_id,
            guild_id,
            len(voice_data or ""),
        )

        if not self.router or not voice_data:
            return

        # Construct Message for NachoBot
        # Platform: 'discord_vc'

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
                "sample_rate": self.config.voice.sample_rate,
                "channels": 2,
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
                variables = self.config.prompts.variables

                if self.config.prompts.planner_prompt:
                    p_prompt = self.config.prompts.planner_prompt
                    template_items["planner_prompt"] = self._inject_variables(
                        p_prompt, variables
                    )
                    self.logger.info(
                        f"Set planner_prompt (len={len(template_items['planner_prompt'])})"
                    )

                if self.config.prompts.replyer_prompt:
                    r_prompt = self.config.prompts.replyer_prompt
                    template_items["replyer_prompt"] = self._inject_variables(
                        r_prompt, variables
                    )
                    self.logger.info(
                        f"Set replyer_prompt (len={len(template_items['replyer_prompt'])})"
                    )

                template_info = TemplateInfo(
                    template_items=template_items,
                    template_name=f"discord_vc_{guild_id}",
                    template_default=False,
                )
                self.logger.info(
                    f"Created TemplateInfo: name={template_info.template_name}, keys={list(template_items.keys())}"
                )

        message_info = BaseMessageInfo(
            platform="discord_vc",
            message_id=str(uuid.uuid4()),
            time=time.time(),
            user_info=UserInfo(
                platform="discord_vc",
                user_id=str(user_id),
                user_nickname=user_name or f"User{user_id}",
            ),
            group_info=GroupInfo(
                platform="discord_vc",
                group_id=str(guild_id),
                group_name=str(guild_id),
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

    async def handle_from_nachobot(self, message: MessageBase) -> None:
        """Queue Core-produced voice segments for Discord playback.

        Text and ``tts_text`` remain Core-owned response fields.  The adapter
        never turns either field into audio; only a returned ``voice`` segment
        is eligible for playback.
        """

        try:
            segment = None
            if isinstance(message, dict):
                segment = message.get("message_segment")
                # Group info access for dict
                try:
                    group_info = message.get("message_info", {}).get("group_info", {})
                    # ncnk_message usually serializes nested objects to dicts
                    if isinstance(group_info, dict):
                        guild_id = int(group_info.get("group_id", 0))
                    else:
                        # Fallback if object
                        guild_id = int(group_info.group_id)
                except Exception:
                    self.logger.error("Could not parse guild_id from message dict")
                    return
            else:
                segment = message.message_segment
                try:
                    guild_id = int(message.message_info.group_info.group_id)
                except Exception:
                    return

            voice_segments = self._voice_segments(segment)
            for voice_segment in voice_segments:
                try:
                    audio_path = write_wav_base64(voice_segment)
                except (ValueError, TypeError) as exc:
                    self.logger.warning("Dropping invalid Core voice segment: %s", type(exc).__name__)
                    continue
                await self.bot.speak(guild_id, audio_path)

            for event in self._voice_stream_events(segment):
                try:
                    self._handle_voice_stream_event(guild_id, event)
                except (ValueError, TypeError, binascii.Error) as exc:
                    self.logger.warning("Dropping invalid Core voice stream event: %s", type(exc).__name__)

        except Exception as e:
            self.logger.error(f"Error handling message from NachoBot: {e}")

    @staticmethod
    def _voice_segments(segment):
        """Yield only complete Core WAV audio, including nested seglists."""
        if isinstance(segment, dict):
            seg_type = segment.get("type")
            data = segment.get("data")
            if seg_type == "seglist" and isinstance(data, list):
                for child in data:
                    yield from DiscordAdapter._voice_segments(child)
            elif seg_type == "voice" and data:
                if isinstance(data, dict):
                    data = data.get("audio_base64") or data.get("audio")
                if data:
                    yield data
            return
        if isinstance(segment, list):
            for child in segment:
                yield from DiscordAdapter._voice_segments(child)
            return
        if hasattr(segment, "type") and hasattr(segment, "data"):
            if segment.type == "seglist" and isinstance(segment.data, list):
                for child in segment.data:
                    yield from DiscordAdapter._voice_segments(child)
            elif segment.type == "voice" and segment.data:
                data = segment.data
                if isinstance(data, dict):
                    data = data.get("audio_base64") or data.get("audio")
                if data:
                    yield data

    @staticmethod
    def _voice_stream_events(segment):
        if isinstance(segment, list):
            for child in segment:
                yield from DiscordAdapter._voice_stream_events(child)
            return
        if isinstance(segment, dict):
            kind, data = segment.get("type"), segment.get("data")
        else:
            kind, data = getattr(segment, "type", None), getattr(segment, "data", None)
        if kind == "seglist" and isinstance(data, list):
            for child in data:
                yield from DiscordAdapter._voice_stream_events(child)
        elif kind == "voice_stream" and isinstance(data, dict):
            yield data

    def _handle_voice_stream_event(self, guild_id: int, event: dict) -> None:
        action = event.get("event")
        stream_id = event.get("stream_id")
        if action not in {"start", "chunk", "end", "abort"} or not isinstance(stream_id, str) or not 1 <= len(stream_id) <= 128:
            raise ValueError("invalid voice stream identity")
        rate, channels, width = (event.get(name) for name in ("sample_rate", "channels", "sample_width"))
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (rate, channels, width)):
            raise ValueError("invalid voice stream format")
        if not 8_000 <= rate <= 192_000 or channels not in (1, 2) or width != 2 or event.get("codec") != "pcm_s16le":
            raise ValueError("unsupported voice stream format")
        if action == "start":
            if not self.bot.start_tts_stream(guild_id, stream_id, rate, channels, width):
                raise ValueError("Discord voice playback is unavailable")
            return
        source = self.bot.tts_streams.get(guild_id)
        if source is None or source.stream_id != stream_id or (source.sample_rate, source.channels, source.sample_width) != (rate, channels, width):
            raise ValueError("voice stream is not active or format changed")
        if action == "chunk":
            seq, encoded = event.get("seq"), event.get("audio_base64")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or not isinstance(encoded, str) or len(encoded) > 4 * ((64 * 1024 + 2) // 3):
                raise ValueError("invalid voice stream chunk")
            pcm = base64.b64decode(encoded, validate=True)
            if not pcm or len(pcm) > 64 * 1024 or len(pcm) % (channels * width):
                raise ValueError("invalid PCM frame")
            self.bot.feed_tts_stream(guild_id, stream_id, seq, pcm)
        elif action == "end":
            self.bot.end_tts_stream(guild_id, stream_id)
        else:
            self.bot.abort_tts_stream(guild_id, stream_id)
