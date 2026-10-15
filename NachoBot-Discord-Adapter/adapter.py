"""Native Discord ingress, Core routing, and guarded Discord egress."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import discord

_root_dir = Path(__file__).resolve().parents[1]
_nachobot_path = _root_dir / "NachoBot"
if _nachobot_path.exists() and str(_nachobot_path) not in os.sys.path:
    os.sys.path.insert(0, str(_nachobot_path))

from config import AdapterConfig, DEFAULT_VOICE_REPLYER_PROMPT
from discord_client import NachoDiscordBot, VoiceSession, resolve_ffmpeg_executable
from discord_media import DiscordMediaFetcher
from discord_policy import accept_formats, is_chat_allowed, is_voice_allowed, voice_payload_formats
from outbound_audio import (
    MP3Attachment,
    prepare_mp3_attachment,
    prepare_voice_message,
    send_ephemeral_mp3_followup,
    send_native_voice_message,
)
from prompt_template import normalize_prompt_template
from slash_interactions import INTERACTION_KEY_RE, SlashInteractionBinding, SlashInteractionRegistry
from voice_codec import MAX_WAV_BYTES, pcm16_to_wav_base64, write_wav_base64
from voice_handler import VoiceHandler

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
except ImportError as exc:  # pragma: no cover - startup reports the actionable error
    raise RuntimeError("NachoBot ncnk_message package is unavailable") from exc


PLATFORM = "discord"
MAX_FILE_SEGMENT_BYTES = 1 * 1024 * 1024
MAX_IMAGE_SEGMENT_BYTES = 16 * 1024 * 1024
MAX_LOCAL_MEDIA_BYTES = 128 * 1024 * 1024
MAX_LOCAL_MEDIA_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_AUDIO_SECONDS = 60
MAX_ATTACHMENT_AUDIO_PCM_BYTES = 48_000 * 2 * 2 * MAX_ATTACHMENT_AUDIO_SECONDS
MAX_TEXT_CHUNK = 2000
CUSTOM_EMOJI = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{17,20})>")
_INVALID_TARGET = object()
VOICE_COMMANDS = {
    "/help": "#help",
    "/help-all": "#help_all",
    "/lang-switch": "#lang_switch",
    "/mus-rand": "#mus_rand",
    "/mute": "#mute",
    "/summary": "#summary",
    "/adv-on": "#adv_on",
    "/adv-off": "#adv_off",
}


def _field(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _discord_timestamp(value: Any) -> str | None:
    """Serialize Discord's original creation time without using it as Core time."""

    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


def _native_snowflake(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    # Current Discord IDs are 17-20 digit Snowflakes. In particular, small
    # numeric legacy Koishi aliases must never be mistaken for transport IDs.
    if not text.isascii() or not text.isdecimal() or not 17 <= len(text) <= 20:
        return None
    if not 0 < int(text) < 2**64:
        return None
    return text


def _safe_filename(value: Any, fallback: str = "attachment") -> str:
    name = str(value or fallback).replace("\x00", "")
    name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = "".join(char for char in name if ord(char) >= 32).strip()
    return name[:120] or fallback


def _decode_base64_bounded(value: Any, max_bytes: int) -> bytes:
    if not isinstance(value, str) or len(value) > ((max_bytes + 2) // 3) * 4:
        raise ValueError("invalid or oversized Base64 payload")
    try:
        encoded = value.encode("ascii")
        data = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError("invalid Base64 payload") from exc
    if len(data) > max_bytes or base64.b64encode(data) != encoded:
        raise ValueError("invalid or oversized Base64 payload")
    return data


def _chunk_text(text: str, limit: int = MAX_TEXT_CHUNK) -> list[str]:
    """Split text without ever producing a Discord message over the hard limit."""
    if not text:
        return []
    chunks: list[str] = []
    remaining = str(text)
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip(" \n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _segments(segment: Any):
    if isinstance(segment, list):
        for child in segment:
            yield from _segments(child)
        return
    kind = _field(segment, "type")
    data = _field(segment, "data")
    if kind == "seglist":
        if isinstance(data, list):
            for child in data:
                yield from _segments(child)
        return
    if kind:
        yield str(kind), data


class _InteractionFollowupDestination:
    """Adapt native channel sends to an ephemeral interaction webhook."""

    def __init__(
        self,
        binding: SlashInteractionBinding,
        native_destination: Any,
        registry: SlashInteractionRegistry,
        target: dict[str, str],
        http_client: Any,
    ):
        self._binding = binding
        self._registry = registry
        self._target = target
        self.id = getattr(native_destination, "id", None)
        self._http_client = http_client

    async def send(self, content=None, **kwargs):
        # Interaction webhooks cannot send a native MessageReference. Remove it
        # defensively, even though bound egress already clears pending replies.
        kwargs.pop("reference", None)
        kwargs["allowed_mentions"] = discord.AllowedMentions.none()
        kwargs["ephemeral"] = True
        kwargs["wait"] = True
        async with self._binding.send_lock:
            if self._registry.get(self._binding.key, self._target) is not self._binding:
                raise RuntimeError("Discord slash interaction binding expired")
            return await self._binding.followup.send(content=content, **kwargs)

    async def send_mp3(self, attachment: MP3Attachment):
        """Send private playable audio while retaining the live slash binding."""
        async with self._binding.send_lock:
            if self._registry.get(self._binding.key, self._target) is not self._binding:
                raise RuntimeError("Discord slash interaction binding expired")
            webhook = self._binding.followup
            return await send_ephemeral_mp3_followup(
                self._http_client,
                getattr(webhook, "id", None),
                getattr(webhook, "token", None),
                self.id,
                attachment,
            )


class DiscordAdapter:
    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.identity_map = config.identity_map
        self.voice_handler = VoiceHandler(config, logger)
        self.bot = NachoDiscordBot(config, self.voice_handler, logger, adapter=self)
        self.bot.set_speech_callback(self.handle_speech_recognized)
        self.media = DiscordMediaFetcher(proxy_url=config.media_proxy_url)
        self.router = Router(
            RouteConfig(
                route_config={
                    PLATFORM: TargetConfig(
                        url=f"ws://{config.nachobot.host}:{config.nachobot.port}/ws",
                        token=get_core_token_from_env(),
                    )
                }
            ),
            custom_logger=logger,
        )
        self.router.register_class_handler(self._dispatch_from_core)
        self._stopping = False
        self._ingress_tasks: set[asyncio.Task] = set()
        self._outbound_tasks: set[asyncio.Task] = set()
        self._slash_interactions = SlashInteractionRegistry()

    def is_context_allowed(self, user: Any, channel: Any) -> bool:
        native_user = str(_field(user, "id", ""))
        logical_user = self.identity_map.logical_user(native_user)
        guild = _field(channel, "guild")
        if guild is None:
            return is_chat_allowed(self.config, logical_user, None)
        logical_channel = self.identity_map.logical_channel(_field(channel, "id", ""))
        return is_chat_allowed(self.config, logical_user, logical_channel)

    def is_voice_user_allowed(self, native_user_id: Any, native_channel_id: Any) -> bool:
        logical_user = self.identity_map.logical_user(native_user_id)
        logical_channel = self.identity_map.logical_channel(native_channel_id)
        return is_voice_allowed(self.config, logical_user, logical_channel)

    def is_voice_channel_allowed(self, native_user_id: Any, native_channel_id: Any) -> bool:
        return self.is_voice_user_allowed(native_user_id, native_channel_id)

    def _is_mentioned_or_replied_to(self, message: Any) -> bool:
        bot_user = getattr(self.bot, "user", None)
        bot_id = _native_snowflake(_field(bot_user, "id"))
        if bot_id is None:
            return False

        for mention in _field(message, "mentions", ()) or ():
            if _native_snowflake(_field(mention, "id")) == bot_id:
                return True

        content = str(_field(message, "content", "") or "")
        if re.search(rf"<@!?{re.escape(bot_id)}>", content):
            return True

        reference = _field(message, "reference")
        resolved = _field(reference, "resolved") if reference is not None else None
        reply_author = _field(resolved, "author") if resolved is not None else None
        return _native_snowflake(_field(reply_author, "id")) == bot_id

    @staticmethod
    def _render_native_mentions(content: str, message: Any) -> str:
        users: dict[str, str] = {}
        channels: dict[str, str] = {}
        roles: dict[str, str] = {}

        def add_mentions(attribute: str, target: dict[str, str], prefix: str) -> None:
            for item in _field(message, attribute, ()) or ():
                native_id = _native_snowflake(_field(item, "id"))
                if native_id is None:
                    continue
                name = str(
                    _field(item, "display_name", None)
                    or _field(item, "name", None)
                    or ""
                ).strip()
                label = f"{name[:80]} ({native_id})" if name else native_id
                target[native_id] = f"{prefix}{label}"

        add_mentions("mentions", users, "@")
        add_mentions("channel_mentions", channels, "#")
        add_mentions("role_mentions", roles, "@")

        content = re.sub(
            r"<@!?([0-9]{17,20})>",
            lambda match: users.get(match.group(1), f"@{match.group(1)}"),
            content,
        )
        content = re.sub(
            r"<@&([0-9]{17,20})>",
            lambda match: roles.get(match.group(1), f"@{match.group(1)}"),
            content,
        )
        return re.sub(
            r"<#([0-9]{17,20})>",
            lambda match: channels.get(match.group(1), f"#{match.group(1)}"),
            content,
        )

    def _target(self, channel_id: Any, user_id: Any, guild_id: Any, mode: str, generation: str = "") -> dict:
        native_channel = _native_snowflake(channel_id)
        native_user = _native_snowflake(user_id)
        native_guild = _native_snowflake(guild_id) if guild_id not in (None, "") else ""
        if native_channel is None or native_user is None or (guild_id not in (None, "") and native_guild is None):
            raise ValueError("Discord transport target is not a native Snowflake")
        return {
            "schema_version": 1,
            "transport": PLATFORM,
            "channel_id": native_channel,
            "user_id": native_user,
            "guild_id": native_guild or "",
            "mode": mode,
            "voice_generation": generation,
        }

    async def _content_segments(self, content: str) -> list[Seg]:
        result: list[Seg] = []
        cursor = 0
        for match in CUSTOM_EMOJI.finditer(content or ""):
            if match.start() > cursor:
                result.append(Seg(type="text", data=content[cursor:match.start()]))
            animated, name, emoji_id = match.groups()
            ext = "gif" if animated else "png"
            url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=128&quality=lossless"
            media = await self.media.download(
                url, max_bytes=min(self.config.max_attachment_bytes, MAX_IMAGE_SEGMENT_BYTES),
                filename=f"{name}.{ext}",
            )
            if media and media.content_type.startswith("image/"):
                result.append(Seg(type="emoji", data=base64.b64encode(media.content).decode("ascii")))
            else:
                result.append(Seg(type="text", data=match.group(0)))
            cursor = match.end()
        if cursor < len(content or ""):
            result.append(Seg(type="text", data=content[cursor:]))
        return result

    async def _attachment_segment(self, attachment: Any) -> Seg:
        filename = _safe_filename(_field(attachment, "filename", "attachment"))
        declared_size = _field(attachment, "size")
        extension = Path(filename).suffix.lower()
        is_video = (
            str(_field(attachment, "content_type", "") or "").lower().startswith("video/")
            or extension in {".mp4", ".mov", ".webm", ".mkv"}
        )
        max_download = min(64 * 1024 * 1024, MAX_LOCAL_MEDIA_BYTES) if is_video else int(self.config.max_attachment_bytes)
        if isinstance(declared_size, int) and declared_size > max_download:
            return Seg(type="text", data=f"[附件 {filename} 超过大小限制，已忽略]")
        downloaded = await self.media.download(
            str(_field(attachment, "url", "")),
            max_bytes=max_download,
            expected_size=declared_size if isinstance(declared_size, int) else None,
            filename=filename,
        )
        if downloaded is None:
            return Seg(type="text", data=f"[附件 {filename} 下载失败或超过大小限制]")
        mime = downloaded.content_type
        if mime.startswith("image/") and len(downloaded.content) <= MAX_IMAGE_SEGMENT_BYTES:
            return Seg(type="image", data=base64.b64encode(downloaded.content).decode("ascii"))
        if is_video and len(downloaded.content) <= 64 * 1024 * 1024:
            return Seg(
                type="video",
                data={
                    "name": filename,
                    "base64": base64.b64encode(downloaded.content).decode("ascii"),
                    "size": len(downloaded.content),
                },
            )
        if mime.startswith("audio/") or Path(filename).suffix.lower() in {".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".aac"}:
            wav_payload = await self._transcode_audio_attachment(downloaded.content)
            if wav_payload:
                return Seg(type="voice", data=wav_payload)
            return Seg(type="text", data=f"[音频 {filename} 无法在时长/解码大小限制内转换]")
        if len(downloaded.content) <= MAX_FILE_SEGMENT_BYTES:
            return Seg(
                type="file",
                data={
                    "name": filename,
                    "base64": base64.b64encode(downloaded.content).decode("ascii"),
                    "size": len(downloaded.content),
                },
            )
        return Seg(type="text", data=f"[文件 {filename} 超过 Core 1 MiB 文件处理限制，已忽略]")

    async def _transcode_audio_attachment(self, content: bytes) -> str | None:
        if not content or len(content) > self.config.max_attachment_bytes:
            return None
        try:
            executable = resolve_ffmpeg_executable()
            process = await asyncio.create_subprocess_exec(
                executable,
                "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                "-t", str(MAX_ATTACHMENT_AUDIO_SECONDS), "-vn", "-f", "s16le",
                "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, RuntimeError):
            return None
        output = bytearray()

        async def write_input():
            assert process.stdin is not None
            process.stdin.write(content)
            await process.stdin.drain()
            process.stdin.close()

        async def read_output():
            assert process.stdout is not None
            while True:
                chunk = await process.stdout.read(64 * 1024)
                if not chunk:
                    return
                if len(output) + len(chunk) > MAX_ATTACHMENT_AUDIO_PCM_BYTES:
                    raise ValueError("decoded audio exceeded the 60 second bound")
                output.extend(chunk)

        input_task = asyncio.create_task(write_input())
        output_task = asyncio.create_task(read_output())
        wait_task = asyncio.create_task(process.wait())
        tasks = (input_task, output_task, wait_task)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=45.0)
            if process.returncode != 0 or not output:
                return None
            return pcm16_to_wav_base64(
                bytes(output), sample_rate=48_000, channels=2,
                max_duration_seconds=MAX_ATTACHMENT_AUDIO_SECONDS,
            ) or None
        except (asyncio.TimeoutError, ValueError, OSError):
            return None
        finally:
            async def cleanup_process() -> None:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if process.stdin is not None:
                    try:
                        if not process.stdin.is_closing():
                            process.stdin.close()
                        await asyncio.wait_for(process.stdin.wait_closed(), 1.0)
                    except (AttributeError, OSError, RuntimeError, asyncio.TimeoutError):
                        pass
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), 5.0)
                except asyncio.TimeoutError:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await process.wait()
                await asyncio.gather(*tasks, return_exceptions=True)

            cleanup_task = asyncio.create_task(cleanup_process())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup_task), 6.0)
                except (asyncio.TimeoutError, Exception):
                    if not cleanup_task.done():
                        cleanup_task.cancel()
                    await asyncio.gather(cleanup_task, return_exceptions=True)
                raise

    async def handle_discord_message(self, message: Any) -> bool:
        task = asyncio.current_task()
        if task is not None:
            self._ingress_tasks.add(task)
        try:
            return await self._handle_discord_message(message)
        finally:
            if task is not None:
                self._ingress_tasks.discard(task)

    async def _handle_discord_message(self, message: Any) -> bool:
        if self._stopping or not self.router:
            return False
        author = _field(message, "author")
        channel = _field(message, "channel")
        if author is None or channel is None:
            return False
        author_id = str(_field(author, "id", ""))
        if (
            bool(_field(author, "bot", False))
            or _field(message, "webhook_id")
            or author_id in self.identity_map.bot_self_ids
            or (self.bot.user and author_id == str(self.bot.user.id))
        ):
            return False
        if not self.is_context_allowed(author, channel):
            return False

        guild = _field(message, "guild")
        native_channel = str(_field(channel, "id", ""))
        logical_user = self.identity_map.logical_user(author_id)
        group_info = None
        guild_id = ""
        if guild is not None:
            logical_channel = self.identity_map.logical_channel(native_channel)
            guild_id = str(_field(guild, "id", ""))
            group_info = GroupInfo(platform=PLATFORM, group_id=logical_channel, group_name=str(_field(channel, "name", logical_channel)))

        content = str(_field(message, "content", "") or "")
        rendered_content = self._render_native_mentions(content, message)
        segments = await self._content_segments(rendered_content)
        for attachment in _field(message, "attachments", ()) or ():
            segments.append(await self._attachment_segment(attachment))
        for sticker in _field(message, "stickers", ()) or ():
            url = str(_field(sticker, "url", ""))
            sticker_name = _safe_filename(_field(sticker, "name", "sticker"))
            media = await self.media.download(
                url, max_bytes=min(self.config.max_attachment_bytes, MAX_IMAGE_SEGMENT_BYTES),
                filename=f"{sticker_name}.png",
            )
            if media and media.content_type.startswith("image/"):
                segments.append(Seg(type="emoji", data=base64.b64encode(media.content).decode("ascii")))
            else:
                segments.append(Seg(type="text", data=f"[贴纸: {sticker_name}]") )
        for embed in _field(message, "embeds", ()) or ():
            title = str(_field(embed, "title", "") or "").strip()
            description = str(_field(embed, "description", "") or "").strip()
            if title or description:
                segments.append(Seg(type="text", data="\n".join(part for part in (title, description) if part)))
        reference = _field(message, "reference")
        referenced_id = _field(reference, "message_id") if reference else None
        if referenced_id is not None:
            segments.append(Seg(type="reply", data={"message_id": str(referenced_id)}))
        if not segments:
            return False

        content_formats = sorted({segment.type for segment in segments})
        native_created_at = _field(message, "created_at")
        discord_transport = {
            "channel_type": type(channel).__name__,
            "thread_id": native_channel if "Thread" in type(channel).__name__ else "",
        }
        original_timestamp = _discord_timestamp(native_created_at)
        if original_timestamp is not None:
            discord_transport["created_at"] = original_timestamp

        additional_config = {
            "delivery_target": self._target(native_channel, author_id, guild_id, "text"),
            "discord_transport": discord_transport,
            "runtime_capabilities": {
                "schema_version": 1,
                "identity_mode": "standard",
                "reply_delivery": "chunked",
                "voice_stream": False,
                "voice_payload_formats": voice_payload_formats(),
            },
            "visual_policy": {
                "version": 1,
                "profile": "discord-native-v1",
                "image": {
                    "prompt": "请用中文概括图片主体、场景、动作和可见文字，只输出简洁的一段纯文本。看不清的内容不要猜。",
                    "temperature": self.config.visual_image.temperature,
                    "max_tokens": self.config.visual_image.max_tokens,
                    "extra_params": dict(self.config.visual_image.extra_params),
                },
                "emoji": {
                    "prompt": "请描述表情包的画面、文字、情绪和梗意，直接输出简洁的中文描述。",
                    "temperature": self.config.visual_image.temperature,
                    "max_tokens": self.config.visual_image.max_tokens,
                    "extra_params": dict(self.config.visual_image.extra_params),
                },
            },
        }
        if self._is_mentioned_or_replied_to(message):
            additional_config["is_mentioned"] = 1.0

        info = BaseMessageInfo(
            platform=PLATFORM,
            message_id=str(_field(message, "id", uuid.uuid4().hex)),
            # Discord's server clock can be ahead of Core's local read cursor.
            # Stamp at forwarding time, after media processing, and keep the
            # native value separately for transport diagnostics.
            time=time.time(),
            group_info=group_info,
            user_info=UserInfo(
                platform=PLATFORM,
                user_id=logical_user,
                user_nickname=str(_field(author, "display_name", None) or _field(author, "name", logical_user)),
                user_cardname=str(_field(author, "display_name", "") or "") if guild else None,
            ),
            format_info=FormatInfo(content_format=content_formats, accept_format=accept_formats(self.config)),
            additional_config=additional_config,
        )
        message_base = MessageBase(
            message_info=info,
            message_segment=Seg(type="seglist", data=segments),
            raw_message=str(_field(message, "content", "") or ""),
        )
        try:
            result = await self.router.send_message(message_base)
            return result is not False
        except Exception as exc:
            self.logger.warning("Discord ingress to Core failed (%s)", type(exc).__name__)
            return False

    async def forward_slash(self, ctx: Any, command_text: str) -> bool | None:
        task = asyncio.current_task()
        if task is not None:
            self._ingress_tasks.add(task)
        try:
            return await self._forward_slash(ctx, command_text)
        finally:
            if task is not None:
                self._ingress_tasks.discard(task)

    async def _forward_slash(self, ctx: Any, command_text: str) -> bool | None:
        if not self.is_context_allowed(ctx.author, ctx.channel):
            return None
        native_user = str(ctx.author.id)
        native_channel = str(ctx.channel.id)
        guild = getattr(ctx, "guild", None)
        guild_id = str(guild.id) if guild else ""
        group_info = None
        if guild is not None:
            group_info = GroupInfo(
                platform=PLATFORM,
                group_id=self.identity_map.logical_channel(native_channel),
                group_name=str(getattr(ctx.channel, "name", native_channel)),
            )
        additional = {
            "delivery_target": self._target(native_channel, native_user, guild_id, "text"),
            "runtime_capabilities": {
                "schema_version": 1,
                "identity_mode": "standard",
                "reply_delivery": "chunked",
                "voice_stream": False,
                "voice_payload_formats": voice_payload_formats(),
            },
        }
        message = MessageBase(
            message_info=BaseMessageInfo(
                platform=PLATFORM,
                message_id=str(getattr(ctx.interaction, "id", uuid.uuid4().hex)),
                time=time.time(),
                group_info=group_info,
                user_info=UserInfo(
                    platform=PLATFORM,
                    user_id=self.identity_map.logical_user(native_user),
                    user_nickname=str(getattr(ctx.author, "display_name", None) or ctx.author.name),
                    user_cardname=str(getattr(ctx.author, "display_name", "") or "") if guild else None,
                ),
                format_info=FormatInfo(content_format=["text"], accept_format=accept_formats(self.config)),
                additional_config=additional,
            ),
            message_segment=Seg(type="text", data=command_text),
        )
        # Do the normal native/group/DM route checks before creating a callback
        # binding. A slash interaction is not permission to bypass delivery
        # target validation.
        if await self._resolve_egress(message) is None:
            return False
        target = additional["delivery_target"]
        binding = self._slash_interactions.register(ctx.followup, target)
        target["interaction_key"] = binding.key
        try:
            result = await self.router.send_message(message)
            if result is False:
                self._slash_interactions.discard(binding.key)
                return False
            # Core dispatch is fire-and-forget from its callback. Only the
            # originating slash task waits for its own actual Discord result.
            return await self._slash_interactions.wait(binding)
        except asyncio.CancelledError:
            self._slash_interactions.discard(binding.key)
            raise
        except Exception as exc:
            self._slash_interactions.discard(binding.key)
            self.logger.warning("Discord slash dispatch failed (%s)", type(exc).__name__)
            return False

    async def handle_speech_recognized(
        self,
        session: VoiceSession,
        native_user_id: int,
        voice_data: str,
        precomputed_asr_result_id: str | None = None,
        capture_id: str | None = None,
        scope: str | None = None,
    ):
        if (
            not voice_data
            or not self.config.voice.enabled
            or not self.bot._session_is_current(session)
            or not isinstance(scope, str)
            or not scope
            or len(scope) > 256
        ):
            return
        user_id = self.identity_map.logical_user(native_user_id)
        channel_id = self.identity_map.logical_channel(session.channel_id)
        member = session.voice_client.guild.get_member(native_user_id) if session.voice_client.guild else None
        nickname = str(getattr(member, "display_name", f"User{user_id}"))
        additional_config: dict[str, Any] = {
            "runtime_capabilities": {
                "schema_version": 1,
                "identity_mode": "external",
                "planner_bypass": True,
                "reply_delivery": "tts_text",
                "tts_language": "zh",
                "voice_stream": True,
                "voice_payload_formats": voice_payload_formats(),
            },
            "voice_format": {"mime_type": "audio/wav", "sample_rate": self.config.voice.sample_rate, "channels": 2},
            "precomputed_asr_scope": scope,
            "delivery_target": self._target(
                session.channel_id, native_user_id, session.guild_id,
                "voice", session.generation,
            ),
        }
        if isinstance(precomputed_asr_result_id, str) and precomputed_asr_result_id.strip() and len(precomputed_asr_result_id) <= 256:
            additional_config["precomputed_asr_result_id"] = precomputed_asr_result_id

        voice_replyer_prompt = (
            self.config.prompts.replyer_prompt
            if self.config.prompts.replyer_prompt.strip()
            else DEFAULT_VOICE_REPLYER_PROMPT
        )
        template_info = None
        if self.config.prompts.planner_prompt or voice_replyer_prompt:
            template_items = {}
            variables = self.config.prompts.variables
            if self.config.prompts.planner_prompt:
                template_items["planner_prompt"] = self._inject_variables(
                    self.config.prompts.planner_prompt, variables
                )
            template_items["replyer_prompt"] = self._inject_variables(
                voice_replyer_prompt, variables
            )
            template_info = TemplateInfo(
                template_items=template_items,
                template_name=f"discord_voice_{channel_id}",
                template_default=False,
            )
        message = MessageBase(
            message_info=BaseMessageInfo(
                platform=PLATFORM,
                message_id=f"voice-{capture_id or uuid.uuid4().hex}",
                time=time.time(),
                group_info=GroupInfo(platform=PLATFORM, group_id=channel_id, group_name=str(session.voice_client.channel.name)),
                user_info=UserInfo(platform=PLATFORM, user_id=user_id, user_nickname=nickname),
                format_info=FormatInfo(
                    content_format=["voice"],
                    accept_format=accept_formats(self.config, voice_context=True),
                ),
                template_info=template_info,
                additional_config=additional_config,
            ),
            message_segment=Seg(type="voice", data=voice_data),
        )
        try:
            await self.router.send_message(message)
        except Exception as exc:
            self.logger.warning("Discord voice ingress to Core failed (%s)", type(exc).__name__)

    @staticmethod
    def _inject_variables(template: str, variables: dict) -> str:
        return normalize_prompt_template(template, variables)

    async def run(self):
        self._stopping = False
        router_task = asyncio.create_task(self.router.run(), name="discord-core-router")
        bot_task = asyncio.create_task(self.bot.start(self.config.discord.token), name="discord-gateway")
        tasks = (router_task, bot_task)
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                error = task.exception() if not task.cancelled() else None
                if error:
                    raise error
                if not self._stopping:
                    raise RuntimeError(f"Discord service task stopped: {task.get_name()}")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stop()

    def _dispatch_from_core(self, message: Any) -> None:
        if self._stopping:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.logger.warning("Dropping Discord Core egress without an active event loop")
            return
        task = loop.create_task(
            self.handle_from_nachobot(message), name="discord-core-egress"
        )
        self._outbound_tasks.add(task)
        task.add_done_callback(self._outbound_task_done)

    def _outbound_task_done(self, task: asyncio.Task) -> None:
        self._outbound_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            self.logger.warning(
                "Discord Core egress task failed (%s)", type(error).__name__
            )

    async def _cancel_and_drain(self, tasks: set[asyncio.Task]) -> None:
        current = asyncio.current_task()
        pending = [task for task in tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        tasks.difference_update(pending)

    async def stop(self):
        if self._stopping:
            return
        self._stopping = True
        self._slash_interactions.clear()
        await self._cancel_and_drain(self._ingress_tasks)
        await self._cancel_and_drain(self._outbound_tasks)
        try:
            await self.router.stop()
        except Exception as exc:
            self.logger.debug("Core Router stop failed (%s)", type(exc).__name__)
        try:
            if not self.bot.is_closed():
                await self.bot.close()
        finally:
            await self.media.close()

    @staticmethod
    def _delivery_target(message: Any) -> dict | None | object:
        info = _field(message, "message_info", {})
        additional = _field(info, "additional_config", {})
        if not isinstance(additional, dict) or "delivery_target" not in additional:
            return None
        target = additional.get("delivery_target")
        if not isinstance(target, dict):
            return _INVALID_TARGET
        if target.get("schema_version") != 1 or target.get("transport") != PLATFORM:
            return _INVALID_TARGET
        if target.get("mode") not in {"text", "voice"}:
            return _INVALID_TARGET
        for key in ("channel_id", "user_id"):
            if _native_snowflake(target.get(key)) is None:
                return _INVALID_TARGET
        guild_id = target.get("guild_id", "")
        if guild_id and _native_snowflake(guild_id) is None:
            return _INVALID_TARGET
        generation = target.get("voice_generation", "")
        if not isinstance(generation, str) or len(generation) > 128:
            return _INVALID_TARGET
        if target.get("mode") == "voice" and not generation:
            return _INVALID_TARGET
        if "interaction_key" in target:
            interaction_key = target.get("interaction_key")
            if (
                target.get("mode") != "text"
                or not isinstance(interaction_key, str)
                or not INTERACTION_KEY_RE.fullmatch(interaction_key)
            ):
                return _INVALID_TARGET
        return dict(target)

    def _resolve_group_native(self, group_info: Any, target: dict | None = None) -> str | None:
        logical_group = str(_field(group_info, "group_id", "") or "")
        if not logical_group:
            return None
        mapped = self.identity_map.native_channel(logical_group)
        native = mapped or _native_snowflake(logical_group)
        if native is None:
            return None
        if target and str(target.get("channel_id")) != native:
            return None
        if self.identity_map.logical_channel(native) != logical_group:
            return None
        return native

    async def _resolve_egress(self, message: Any) -> tuple[str, Any, dict] | None:
        info = _field(message, "message_info", {})
        if _field(info, "platform") != PLATFORM:
            return None
        group = _field(info, "group_info")
        user = _field(info, "user_info")
        target = self._delivery_target(message)
        if target is _INVALID_TARGET:
            return None
        mode = target.get("mode") if isinstance(target, dict) else "text"
        if group is not None:
            native_channel = self._resolve_group_native(group, target)
            if native_channel is None:
                return None
            if mode == "voice":
                session = self.bot.get_voice_session(native_channel, target.get("voice_generation")) if target else None
                if session is None or str(session.guild_id) != str(target.get("guild_id", "")):
                    return None
                return mode, session, target
            channel = self.bot.get_channel(int(native_channel))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(native_channel))
                except Exception:
                    return None
            if not callable(getattr(channel, "send", None)):
                return None
            if target and target.get("guild_id") and str(getattr(getattr(channel, "guild", None), "id", "")) != str(target["guild_id"]):
                return None
            return mode, channel, target or {}

        logical_user = str(_field(user, "user_id", "") or "")
        if not logical_user:
            return None
        native_user = self.identity_map.native_user(logical_user) or _native_snowflake(logical_user)
        if native_user is None:
            return None
        if isinstance(target, dict) and target.get("user_id") != native_user:
            return None
        if mode == "voice":
            return None
        if isinstance(target, dict) and target.get("channel_id"):
            native_channel = str(target["channel_id"])
            known_private = any(
                row.get("user_id") == native_user and row.get("channel_id") == native_channel
                for row in self.identity_map.private_channels
            )
            channel = self.bot.get_channel(int(native_channel))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(native_channel))
                except Exception:
                    channel = None
            recipient = getattr(channel, "recipient", None) if channel else None
            if channel is not None and str(getattr(recipient, "id", "")) == native_user:
                return "text", channel, target
            if known_private:
                user_object = self.bot.get_user(int(native_user))
                if user_object is None:
                    try:
                        user_object = await self.bot.fetch_user(int(native_user))
                    except Exception:
                        return None
                try:
                    dm = await user_object.create_dm()
                except Exception:
                    return None
                if str(dm.id) == native_channel:
                    return "text", dm, target
            return None
        user_object = self.bot.get_user(int(native_user))
        if user_object is None:
            try:
                user_object = await self.bot.fetch_user(int(native_user))
            except Exception:
                return None
        try:
            return "text", await user_object.create_dm(), target or {}
        except Exception:
            return None

    async def _send_audio_attachment(
        self,
        destination: Any,
        source: bytes | Path,
        *,
        reply_id: str | None,
        private_followup: bool,
        require_wav: bool = False,
    ) -> dict[str, str]:
        input_limit = MAX_LOCAL_MEDIA_BYTES
        output_limit = min(int(self.config.max_attachment_bytes), MAX_LOCAL_MEDIA_UPLOAD_BYTES)
        ffmpeg = resolve_ffmpeg_executable()
        if private_followup:
            attachment = await prepare_mp3_attachment(
                source,
                ffmpeg_executable=ffmpeg,
                max_input_bytes=input_limit,
                max_output_bytes=output_limit,
                require_wav=require_wav,
            )
            return await destination.send_mp3(attachment)

        voice = await prepare_voice_message(
            source,
            ffmpeg_executable=ffmpeg,
            max_input_bytes=input_limit,
            max_output_bytes=output_limit,
            require_wav=require_wav,
        )
        reference = self._message_reference(destination, reply_id)
        reference_data = reference.to_message_reference_dict() if reference is not None else None
        return await send_native_voice_message(
            self.bot.http,
            destination.id,
            voice,
            message_reference=reference_data,
        )

    async def handle_from_nachobot(self, message: Any) -> None:
        try:
            await self._handle_from_nachobot(message)
        except asyncio.CancelledError:
            target = self._delivery_target(message)
            if isinstance(target, dict):
                interaction_key = target.get("interaction_key")
                if isinstance(interaction_key, str):
                    self._slash_interactions.discard(interaction_key)
            raise

    async def _handle_from_nachobot(self, message: Any) -> None:
        resolved = await self._resolve_egress(message)
        if resolved is None:
            self.logger.warning("Dropping Discord egress without a valid mapped native target")
            return
        mode, destination, target = resolved
        interaction_key = target.get("interaction_key")
        interaction_binding = None
        if interaction_key is not None:
            interaction_binding = self._slash_interactions.get(interaction_key, target)
            if interaction_binding is None:
                self.logger.debug("Dropping Discord egress with no matching live interaction")
                return
        text_destination = (
            getattr(destination.voice_client, "channel", None)
            if mode == "voice"
            else destination
        )
        if interaction_binding is not None:
            text_destination = _InteractionFollowupDestination(
                interaction_binding,
                text_destination,
                self._slash_interactions,
                target,
                getattr(self.bot, "http", None),
            )
        segment = _field(message, "message_segment")
        segments = list(_segments(segment))
        pending_reply_id: str | None = next(
            (
                str(reply_id)
                for kind, data in segments
                if kind == "reply"
                for reply_id in [
                    _field(data, "message_id") if isinstance(data, dict) else data
                ]
                if _native_snowflake(reply_id)
            ),
            None,
        )
        if interaction_binding is not None:
            pending_reply_id = None
        actual_ids: list[str] = []
        send_ok = True
        voice_delivery = False
        for kind, data in segments:
            # A voice target receives conversational text through Discord
            # playback. Keep explicitly returned media attachments on the
            # existing text-channel path.
            if mode == "voice" and kind == "text":
                continue
            if not send_ok and kind not in {"voice", "voice_stream", "voicefile"}:
                continue
            try:
                if kind == "reply":
                    continue
                if kind == "text":
                    if not callable(getattr(text_destination, "send", None)):
                        continue
                    for chunk in _chunk_text(str(data or "")):
                        kwargs = self._native_send_kwargs(
                            text_destination, pending_reply_id
                        )
                        sent = await text_destination.send(chunk, **kwargs)
                        if sent is None or getattr(sent, "id", None) is None:
                            send_ok = False
                            break
                        actual_ids.append(str(sent.id))
                        pending_reply_id = None
                elif kind in {"image", "emoji"}:
                    if not callable(getattr(text_destination, "send", None)):
                        continue
                    payload = data.get("base64") if isinstance(data, dict) else data
                    content = _decode_base64_bounded(payload, min(self.config.max_attachment_bytes, MAX_IMAGE_SEGMENT_BYTES))
                    filename = _safe_filename(data.get("name") if isinstance(data, dict) else None, f"discord-{kind}.png")
                    file = discord.File(io.BytesIO(content), filename=filename)
                    kwargs = self._native_send_kwargs(
                        text_destination, pending_reply_id, file=file
                    )
                    sent = await text_destination.send(**kwargs)
                    actual_ids.append(str(sent.id))
                    pending_reply_id = None
                elif kind == "file":
                    if not callable(getattr(text_destination, "send", None)):
                        continue
                    if isinstance(data, dict):
                        content = _decode_base64_bounded(data.get("base64"), MAX_FILE_SEGMENT_BYTES)
                        size = data.get("size")
                        if isinstance(size, bool) or not isinstance(size, int) or size != len(content):
                            raise ValueError("file size metadata mismatch")
                        file = discord.File(io.BytesIO(content), filename=_safe_filename(data.get("name")))
                    elif isinstance(data, str):
                        path = self._resolve_local_media_path(data, kind="file")
                        if path.stat().st_size > min(self.config.max_attachment_bytes, MAX_LOCAL_MEDIA_UPLOAD_BYTES):
                            raise ValueError("sandbox artifact exceeds configured Discord upload limit")
                        file = discord.File(str(path), filename=path.name)
                    else:
                        raise ValueError("file payload must be bounded data or an authorized sandbox path")
                    kwargs = self._native_send_kwargs(
                        text_destination, pending_reply_id, file=file
                    )
                    sent = await text_destination.send(**kwargs)
                    actual_ids.append(str(sent.id))
                    pending_reply_id = None
                elif kind == "video":
                    if not callable(getattr(text_destination, "send", None)):
                        continue
                    if not isinstance(data, dict):
                        raise ValueError("video payload must be an object")
                    content = _decode_base64_bounded(data.get("base64"), 64 * 1024 * 1024)
                    size = data.get("size")
                    if isinstance(size, bool) or not isinstance(size, int) or size != len(content):
                        raise ValueError("video size metadata mismatch")
                    if len(content) > min(self.config.max_attachment_bytes, MAX_LOCAL_MEDIA_UPLOAD_BYTES):
                        raise ValueError("video exceeds configured Discord upload limit")
                    file = discord.File(io.BytesIO(content), filename=_safe_filename(data.get("name"), "video.mp4"))
                    sent = await text_destination.send(
                        **self._native_send_kwargs(
                            text_destination, pending_reply_id, file=file
                        )
                    )
                    actual_ids.append(str(sent.id))
                    pending_reply_id = None
                elif kind in {"voicefile", "videofile"}:
                    path = self._resolve_local_media_path(data, kind=kind)
                    if kind == "voicefile" and mode == "voice":
                        owned_path = await self._copy_local_media_to_temp_async(
                            path
                        )
                        self.bot.own_temp_audio(owned_path)
                        accepted = await self.bot.speak(
                            destination.channel_id,
                            owned_path,
                            target.get("voice_generation"),
                            cleanup=True,
                            wait_until_started=True,
                        )
                        if not accepted:
                            send_ok = False
                        else:
                            voice_delivery = True
                            actual_ids.append(
                                f"voice-playback:{destination.channel_id}:{target['voice_generation']}:{uuid.uuid4().hex}"
                            )
                        continue
                    if kind == "voicefile":
                        if path.stat().st_size > MAX_LOCAL_MEDIA_BYTES:
                            raise ValueError("local audio exceeds configured processing limit")
                        owned_path = await self._copy_local_media_to_temp_async(path)
                        try:
                            sent = await self._send_audio_attachment(
                                text_destination,
                                Path(owned_path),
                                reply_id=pending_reply_id,
                                private_followup=interaction_binding is not None,
                            )
                        finally:
                            try:
                                os.unlink(owned_path)
                            except OSError:
                                pass
                        actual_ids.append(sent["message_id"])
                        pending_reply_id = None
                        continue
                    if not callable(getattr(text_destination, "send", None)):
                        send_ok = False
                        continue
                    if path.stat().st_size > min(self.config.max_attachment_bytes, MAX_LOCAL_MEDIA_UPLOAD_BYTES):
                        raise ValueError("local media exceeds configured Discord upload limit")
                    owned_path = await self._copy_local_media_to_temp_async(path)
                    try:
                        file = discord.File(owned_path, filename=path.name)
                        try:
                            sent = await text_destination.send(
                                **self._native_send_kwargs(
                                    text_destination, pending_reply_id, file=file
                                ),
                            )
                        finally:
                            file.close()
                    finally:
                        try:
                            os.unlink(owned_path)
                        except OSError:
                            pass
                    actual_ids.append(str(sent.id))
                    pending_reply_id = None
                elif kind == "voice":
                    payload = (
                        data.get("audio_base64") or data.get("audio") or data.get("base64")
                        if isinstance(data, dict)
                        else data
                    )
                    if mode == "voice":
                        if not self.config.voice.enabled:
                            continue
                        audio_path = write_wav_base64(payload)
                        self.bot.own_temp_audio(audio_path)
                        accepted = await self.bot.speak(
                            destination.channel_id,
                            audio_path,
                            target.get("voice_generation"),
                            cleanup=True,
                            wait_until_started=True,
                        )
                        if not accepted:
                            send_ok = False
                        else:
                            voice_delivery = True
                            actual_ids.append(
                                f"voice-playback:{destination.channel_id}:{target['voice_generation']}:{uuid.uuid4().hex}"
                            )
                    elif self.config.voice.use_tts:
                        content = _decode_base64_bounded(
                            payload,
                            min(MAX_WAV_BYTES, int(self.config.max_attachment_bytes)),
                        )
                        sent = await self._send_audio_attachment(
                            text_destination,
                            content,
                            reply_id=pending_reply_id,
                            private_followup=interaction_binding is not None,
                            require_wav=True,
                        )
                        actual_ids.append(sent["message_id"])
                        pending_reply_id = None
                elif kind == "voice_stream":
                    if mode != "voice" or not self.config.voice.enabled or not isinstance(data, dict):
                        continue
                    if self._handle_voice_stream_event(destination, target, data):
                        voice_delivery = True
                else:
                    self.logger.debug("Ignoring unsupported Discord Core segment type: %s", kind[:48])
            except Exception as exc:
                send_ok = False
                self.logger.warning("Discord segment delivery failed (%s)", type(exc).__name__)
                continue

        delivered = send_ok and bool(actual_ids or voice_delivery)
        if interaction_binding is not None:
            self._slash_interactions.complete(interaction_binding, delivered)
        if send_ok and actual_ids:
            info = _field(message, "message_info", {})
            source_id = str(_field(info, "message_id", "") or "")
            if source_id:
                try:
                    await self.router.send_custom_message(
                        PLATFORM,
                        "message_id_echo",
                        {"type": "echo", "echo": source_id, "actual_id": actual_ids[0], "platform": PLATFORM},
                    )
                except Exception as exc:
                    self.logger.warning("Discord message receipt failed (%s)", type(exc).__name__)
        elif not actual_ids and not voice_delivery:
            self.logger.debug("Discord Core response contained no deliverable segments")

    def _resolve_local_media_path(self, value: Any, *, kind: str) -> Path:
        raw = str(value or "").strip()
        if not raw or "\x00" in raw:
            raise ValueError("invalid local media path")
        path = Path(raw).resolve(strict=True)
        roots = {
            "voicefile": (
                _root_dir / "NachoBot" / "music",
                _root_dir / "NachoBot" / "data" / "media-tmp",
            ),
            "videofile": (
                _root_dir / "NachoBot" / "music",
                _root_dir / "NachoBot" / "data" / "video",
                _root_dir / "NachoBot" / "data" / "media-tmp",
            ),
            "file": (_root_dir / "NachoBot" / "data" / "sandbox",),
        }.get(kind, ())
        contained = False
        for root_candidate in roots:
            try:
                path.relative_to(root_candidate.resolve(strict=True))
                contained = True
                break
            except (OSError, ValueError):
                continue
        if not contained:
            raise ValueError("local media path is outside the allowed shared media root")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or path.stat().st_size <= 0 or path.stat().st_size > MAX_LOCAL_MEDIA_BYTES:
            raise ValueError("local media file is not regular or exceeds its bound")
        return path

    @staticmethod
    def _copy_local_media_to_temp(source: Path) -> str:
        source_size = source.stat().st_size
        if source_size <= 0 or source_size > MAX_LOCAL_MEDIA_BYTES:
            raise ValueError("local media copy exceeds its size bound")
        fd, target = tempfile.mkstemp(
            prefix="nachobot-discord-core-media-",
            suffix=source.suffix or ".bin",
        )
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                remaining = source_size
                while remaining:
                    chunk = input_file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("local media ended before its declared size")
                    output.write(chunk)
                    remaining -= len(chunk)
            return target
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(target)
            except OSError:
                pass
            raise

    @staticmethod
    def _copy_local_audio_to_temp(source: Path) -> str:
        return DiscordAdapter._copy_local_media_to_temp(source)

    async def _copy_local_media_to_temp_async(self, source: Path) -> str:
        task = asyncio.create_task(
            asyncio.to_thread(self._copy_local_media_to_temp, source)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                owned_path = await asyncio.wait_for(asyncio.shield(task), 5.0)
            except (asyncio.TimeoutError, Exception):
                def cleanup_when_ready(done: asyncio.Task) -> None:
                    if done.cancelled():
                        return
                    try:
                        result = done.result()
                    except Exception:
                        return
                    try:
                        os.unlink(result)
                    except OSError:
                        pass

                task.add_done_callback(cleanup_when_ready)
            else:
                try:
                    os.unlink(owned_path)
                except OSError:
                    pass
            raise

    def _message_reference(self, destination: Any, message_id: Any):
        native_id = _native_snowflake(message_id)
        if native_id is None:
            return None
        return discord.MessageReference(
            message_id=int(native_id),
            channel_id=int(destination.id),
            fail_if_not_exists=False,
        )

    def _native_send_kwargs(
        self, destination: Any, reply_id: str | None, **kwargs: Any
    ) -> dict[str, Any]:
        result = {
            "allowed_mentions": discord.AllowedMentions.none(),
            **kwargs,
        }
        if reply_id:
            reference = self._message_reference(destination, reply_id)
            if reference is not None:
                result["reference"] = reference
        return result

    def _handle_voice_stream_event(self, session: VoiceSession, target: dict, event: dict) -> bool:
        action = event.get("event")
        stream_id = event.get("stream_id")
        if not self.bot._session_is_current(session):
            raise ValueError("voice stream session is stale")
        if action not in {"start", "chunk", "end", "abort"} or not isinstance(stream_id, str) or not 1 <= len(stream_id) <= 128:
            raise ValueError("invalid voice stream identity")
        if target.get("mode") != "voice" or target.get("channel_id") != str(session.channel_id) or target.get("voice_generation") != session.generation:
            raise ValueError("voice stream target is stale")
        rate, channels, width = (event.get(name) for name in ("sample_rate", "channels", "sample_width"))
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (rate, channels, width)):
            raise ValueError("invalid voice stream format")
        if not 8_000 <= rate <= 192_000 or channels not in (1, 2) or width != 2 or event.get("codec") != "pcm_s16le":
            raise ValueError("unsupported voice stream format")
        channel_id = session.channel_id
        generation = session.generation
        if action == "start":
            if not self.bot.start_tts_stream(channel_id, generation, stream_id, rate, channels, width):
                raise ValueError("Discord voice playback is unavailable")
            return True
        entry = self.bot.tts_streams.get(channel_id)
        source = entry[1] if entry and entry[0] == generation else None
        if source is None or source.stream_id != stream_id or (source.sample_rate, source.channels, source.sample_width) != (rate, channels, width):
            raise ValueError("voice stream is not active or format changed")
        if action == "chunk":
            seq, encoded = event.get("seq"), event.get("audio_base64")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or not isinstance(encoded, str) or len(encoded) > 4 * ((64 * 1024 + 2) // 3):
                raise ValueError("invalid voice stream chunk")
            pcm = base64.b64decode(encoded, validate=True)
            if not pcm or len(pcm) > 64 * 1024 or len(pcm) % (channels * width):
                raise ValueError("invalid PCM frame")
            if not self.bot.feed_tts_stream(channel_id, generation, stream_id, seq, pcm):
                raise ValueError("voice stream chunk was rejected")
        elif action == "end":
            if not self.bot.end_tts_stream(channel_id, generation, stream_id):
                raise ValueError("voice stream end was rejected")
        else:
            if not self.bot.abort_tts_stream(channel_id, stream_id):
                raise ValueError("voice stream abort was rejected")
        return True
