import asyncio
import logging
import sys
import uuid
import time
import re
from pathlib import Path
from typing import Optional

from config import AdapterConfig
from discord_client import NachoDiscordBot
from voice_handler import VoiceHandler

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
    )
except ImportError:
    # Fallback if NachoBot not found (Logic won't work but prevents import error crash)
    print(
        "Warning: ncnk_message not found. Please ensure NachoBot is adjacent to this folder."
    )
    BaseMessageInfo = FormatInfo = GroupInfo = MessageBase = Router = RouteConfig = (
        Seg
    ) = TargetConfig = TemplateInfo = UserInfo = None

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def _mask_urls(text: str) -> str:
    if not text:
        return ""
    return _URL_RE.sub("[link]", text)


# Regex to match kaomoji and special emoticons (Ported from Bilibili Adapter)
_KAOMOJI_RE = re.compile(
    r"[\(\（]"  # Opening bracket
    r"[^\(\)\（\）]{1,15}"  # Content (1-15 chars, no nested brackets)
    r"[\)\）]"  # Closing bracket
    r"|"
    r"[｡ﾟ✧♪♡☆★●○◎◇◆□■△▲▽▼※→←↑↓]+"  # Special symbols
)


def _clean_text_for_tts(text: str) -> str:
    """Clean text for TTS: remove kaomoji, emoticons, and special characters."""
    if not text:
        return ""
    # Remove kaomoji like (๑•́ ₃ •̀๑), (=^･ω･^=), etc.
    cleaned = _KAOMOJI_RE.sub("", text)
    # Remove standalone special chars that might cause issues
    cleaned = re.sub(r"[～〜♪♡☆★]", "", cleaned)
    # Normalize multiple spaces/punctuation
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[。、！？]{2,}", "。", cleaned)
    return cleaned.strip()


class DiscordAdapter:
    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger

        # Initialize Voice & Bot
        self.voice_handler = VoiceHandler(config, logger)

        # Initialize TTS
        from tts_handler import TTSHandler

        self.tts_handler = TTSHandler(logger)

        self.bot = NachoDiscordBot(config, self.voice_handler, logger)
        self.bot.set_speech_callback(self.handle_speech_recognized)

        # Initialize Router (Connection to NachoBot Core)
        self.router = None
        if Router:
            route_config = RouteConfig(
                route_config={
                    "discord_vc": TargetConfig(
                        url=f"ws://{self.config.nachobot.host}:{self.config.nachobot.port}/ws",
                        token=None,
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
        self, guild_id: int, user_id: int, text: str, user_name: str = None
    ):
        """Called when audio is recognized as text."""
        self.logger.info(f"Speech from {user_name or user_id} in {guild_id}: {text}")

        if not self.router:
            return

        # Disable network search if configured
        processed_text = text
        if self.config.disable_network_search:
            processed_text = _mask_urls(processed_text)

        # Construct Message for NachoBot
        # Platform: 'discord_vc'

        additional_config = {}
        # Unconditionally disable tools/MCP for VC environment
        additional_config["disable_tools"] = True

        if self.config.disable_network_search:
            # We also mask URLs if specifically requested, though disable_tools usually covers search actions
            # Keeping mask logic for text sanitization if needed
            pass

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
                content_format=["text"],
                accept_format=["text", "voice"],  # We accept voice reply
            ),
            template_info=template_info,
            additional_config=additional_config,
        )

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="text", data=processed_text),
        )

        await self.router.send_message(message)

    async def handle_from_nachobot(self, message: MessageBase) -> None:
        """Handle outgoing messages from NachoBot (Core -> Adapter -> Discord)."""
        # This implementation depends on how Router calls this callback.
        # Assuming it calls this for messages directed TO this adapter.

        try:
            # We only care about text or voice segments
            text_to_speak = ""

            # Helper to extract seg
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

            # Flatten segments to text
            if segment:
                if isinstance(segment, dict):  # Dict segment
                    if segment.get("type") == "text":
                        text_to_speak = segment.get("data", "")
                elif isinstance(segment, list):  # List of segments
                    for seg in segment:
                        if isinstance(seg, dict):
                            if seg.get("type") == "text":
                                text_to_speak += seg.get("data", "")
                        elif hasattr(seg, "type") and hasattr(seg, "data"):
                            if seg.type == "text":
                                text_to_speak += seg.data
                elif hasattr(segment, "type") and hasattr(
                    segment, "data"
                ):  # Object segment
                    if segment.type == "text":
                        text_to_speak = segment.data

            if not text_to_speak:
                return

            # Strip invisible characters like zero-width space (\u200b) and literal escape sequences
            # This prevents generating TTS for "silent" replies
            if text_to_speak:
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

            # Filtering typo correction messages (Same as Bilibili Adapter)
            # Typically these are short messages containing only Chinese characters
            if len(text_to_speak) <= 2 and all(
                "\u4e00" <= c <= "\u9fff" for c in text_to_speak
            ):
                self.logger.info(f"Skipping typo correction message: {text_to_speak}")
                return

            # Target Guild extracted earlier

            # 1. Clean text for TTS
            cleaned_text = _clean_text_for_tts(text_to_speak)
            self.logger.info(f"Cleaned text for TTS: {cleaned_text}")

            if not cleaned_text:
                self.logger.warning("Text became empty after cleaning, skipping TTS.")
                return

            # 分段流式：按句切分，逐句生成并立即送入语音频道
            try:
                from tts_src.utils.text_splitter import split_text_for_streaming
                segments = split_text_for_streaming(cleaned_text)
            except ImportError:
                segments = [cleaned_text]

            self.logger.info(f"TTS segment stream: {len(segments)} segments")

            for idx, seg_text in enumerate(segments):
                self.logger.info(f"Generating segment {idx+1}/{len(segments)}: {seg_text}")
                audio_path = await self._generate_tts(seg_text)
                if audio_path:
                    await self.bot.speak(guild_id, audio_path)

        except Exception as e:
            self.logger.error(f"Error handling message from NachoBot: {e}")

    async def _generate_tts(self, text: str) -> Optional[str]:
        """Convert text to speech audio file."""
        return await self.tts_handler.generate_speech(text)
