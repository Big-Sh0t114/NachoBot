"""Pycord client and native slash/voice commands for the Discord adapter."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
import warnings
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import discord
from discord.errors import ClientException
from discord.sinks.core import Sink
from discord.sinks.errors import RecordingException
from discord.utils import MISSING
from discord.voice import VoiceClient as _PycordVoiceClient
from discord.voice.receive.reader import AudioReader
from discord.voice.state import VoiceConnectionState
from discord.ext import commands
from static_ffmpeg import run

from config import AdapterConfig
from core_audio_stream import CoreAudioStreamClient, DiscordCoreAudioStreamBridge
from discord_voice_compat import stop_playback_only
from tts_audio_source import PCMStreamSource
from voice_handler import SilenceDetectingSink, VoiceHandler


intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.dm_messages = True
intents.guilds = True
intents.voice_states = True

VOICE_CONNECT_TIMEOUT = 60.0
VOICE_RECOVERY_POLL_INTERVAL = 0.5
VOICE_TRANSIENT_REMOVAL_TTL = 5.0
VOICE_PHASE_NAMES = {
    "websocket_connected": "udp_discovery",
    "got_ip_discovery": "voice_secret_key",
    "connected": "connected",
    "disconnected": "disconnected",
    "set_guild_voice_state": "gateway_voice_state",
    "got_voice_state_update": "gateway_voice_state",
    "got_voice_server_update": "gateway_voice_server",
    "got_both_voice_updates": "gateway_voice_updates",
    "got_websocket_ready": "voice_websocket_ready",
}


class VoiceLifecycleError(RuntimeError):
    """A bounded voice join or recovery attempt could not reach a usable client."""

    def __init__(
        self,
        message: str,
        *,
        phase: str = "unknown",
        connection_state: str = "unknown",
    ):
        super().__init__(message)
        self.phase = phase
        self.connection_state = connection_state


@dataclass
class VoiceConnectAttempt:
    attempt_id: str
    channel_id: int
    voice_client: object | None = None


@dataclass(frozen=True)
class TransientVoiceRemoval:
    voice_client: object
    channel_id: int
    expires_at: float
    attempt_id: str | None


class RetryAwareVoiceConnectionState(VoiceConnectionState):
    """Tag only Pycord's non-cleaning disconnects before their Gateway update."""

    async def disconnect(
        self, *, force: bool = True, cleanup: bool = True, wait: bool = False
    ) -> None:
        if not cleanup and (force or self.is_connected()):
            owner = getattr(self.client, "_nacho_lifecycle_bot", None)
            if owner is not None:
                owner._mark_transient_voice_disconnect(self.client)
        await super().disconnect(force=force, cleanup=cleanup, wait=wait)


class DemuxingAudioReader(AudioReader):
    """Filter unsupported RTCP mux packets before Pycord treats them as RTP."""

    _SUPPORTED_RTCP_TYPES = frozenset((200, 201))

    def callback(self, packet_data: bytes) -> None:
        # RTP and RTCP both use version 2. The second octet distinguishes the
        # RTCP packet type range used by RFC 5761 from ordinary RTP payloads.
        if len(packet_data) < 2 or packet_data[0] >> 6 != 2:
            return

        packet_type = packet_data[1]
        if 192 <= packet_type <= 223:
            if packet_type not in self._SUPPORTED_RTCP_TYPES or len(packet_data) < 4:
                return

            # Supported report packets have a fixed header followed by one
            # 24-byte report block per count. Drop truncated reports before
            # the SDK parser can emit a malformed-packet traceback.
            report_count = packet_data[0] & 0x1F
            minimum_size = 28 if packet_type == 200 else 8
            minimum_size += report_count * 24
            if len(packet_data) < minimum_size:
                return
        else:
            # The SDK RTP parser unpacks the fixed header and each CSRC. Keep
            # short or structurally incomplete RTP datagrams away from it.
            minimum_size = 12 + (packet_data[0] & 0x0F) * 4
            if packet_data[0] & 0x10:
                minimum_size += 4
            if len(packet_data) < minimum_size:
                return

        super().callback(packet_data)


class RetryAwareVoiceClient(_PycordVoiceClient):
    """VoiceClient whose SDK reconnect removals are correlated by the bot."""

    def __init__(self, client, channel):
        self._nacho_lifecycle_bot = client
        super().__init__(client, channel)

    def create_connection_state(self) -> RetryAwareVoiceConnectionState:
        return RetryAwareVoiceConnectionState(self, hook=self._recv_hook)

    def start_recording(
        self,
        sink: Sink,
        callback: Callable[..., object] | None = None,
        *args: object,
        sync_start: bool = MISSING,
    ) -> None:
        """Start receiving audio through this client's RTCP-aware reader."""
        if not self.is_connected():
            raise RecordingException("not connected to a voice channel")
        if not isinstance(sink, Sink):
            raise TypeError(f"expected a Sink object, got {sink.__class__.__name__}")

        if self.is_recording():
            raise ClientException("Already recording audio")

        if args:
            warnings.warn(
                "'args' parameter is deprecated since 2.7 and will be removed in 3.0"
            )
        if sync_start is not MISSING:
            warnings.warn(
                "'sync_start' parameter is deprecated since 2.7 and will be removed in 3.0"
            )

        self._reader = DemuxingAudioReader(
            sink, self, after=callback, args=args, start=True
        )

    start_listening = start_recording


@lru_cache(maxsize=1)
def resolve_ffmpeg_executable() -> str:
    """Resolve the project-shared FFmpeg executable used for local decoding/playback."""
    configured_dir = os.environ.get("NACHOBOT_FFMPEG_DIR", "").strip()
    shared_root = (
        Path(configured_dir).expanduser()
        if configured_dir
        else Path(__file__).resolve().parent.parent / ".runtime" / "ffmpeg"
    )
    platform_dir = shared_root.resolve() / run.get_platform_key()
    platform_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_path, _ = run.get_or_fetch_platform_executables_else_raise(
        download_dir=str(platform_dir)
    )
    return ffmpeg_path


@dataclass
class AudioItem:
    path: str
    cleanup_owned: bool
    started: asyncio.Future | None = None


@dataclass
class VoiceSession:
    guild_id: int
    channel_id: int
    generation: str
    voice_client: discord.VoiceClient
    voice_ws: object | None = None
    voice_session_id: str | None = None
    sink: SilenceDetectingSink | None = None
    capture_scopes: dict[str, tuple[int, str]] = field(default_factory=dict)
    stream_ids: set[str] = field(default_factory=set)
    queue: deque[AudioItem] = field(default_factory=deque)
    current_audio: AudioItem | None = None
    current_playback_attempt: str | None = None
    interrupted_audio: AudioItem | None = None
    is_user_speaking: bool = False
    closed: bool = False
    watcher_task: asyncio.Task | None = None


class DiscordTransportCog(commands.Cog):
    """Slash commands whose business behavior is owned by Core."""

    def __init__(self, bot: "NachoDiscordBot", adapter):
        self.bot = bot
        self.adapter = adapter

    async def _forward(self, ctx: discord.ApplicationContext, command: str):
        if not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)
        try:
            delivered = await self.adapter.forward_slash(ctx, command)
        except Exception as exc:
            self.bot.logger.warning("Core slash forwarding failed (%s)", type(exc).__name__)
            delivered = False
        if delivered is True:
            # The actual Core output was sent through this interaction's
            # followup webhook. Do not add an accepted/duplicate notification.
            return
        text = (
            "当前聊天没有启用这个命令。"
            if delivered is None
            else "Core 没有返回可发送的结果，或当前不可用，请稍后重试。"
        )
        await ctx.followup.send(
            text,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    @discord.slash_command(name="help", description="查看 NachoBot 帮助")
    async def help_command(self, ctx: discord.ApplicationContext):
        await self._forward(ctx, "#help")

    @discord.slash_command(name="help-all", description="查看完整帮助")
    async def help_all_command(self, ctx: discord.ApplicationContext):
        await self._forward(ctx, "#help_all")

    @discord.slash_command(name="lang-switch", description="切换语言")
    async def language_command(self, ctx: discord.ApplicationContext):
        await self._forward(ctx, "#lang_switch")

    @discord.slash_command(name="mus-rand", description="随机播放一首歌")
    async def random_music_command(self, ctx: discord.ApplicationContext):
        await self._forward(ctx, "#mus_rand")

    @discord.slash_command(name="mute", description="切换静音状态")
    async def mute_command(self, ctx: discord.ApplicationContext):
        await self._forward(ctx, "#mute")

    @discord.slash_command(name="summary", description="总结当前聊天")
    async def summary_command(self, ctx: discord.ApplicationContext):
        await self._forward(ctx, "#summary")

    @discord.slash_command(name="adv-on", description="开启高级模式")
    async def adv_on_command(self, ctx: discord.ApplicationContext):
        await self._forward(ctx, "#adv_on")

    @discord.slash_command(name="adv-off", description="关闭高级模式")
    async def adv_off_command(self, ctx: discord.ApplicationContext):
        await self._forward(ctx, "#adv_off")

    @discord.slash_command(name="song", description="点播歌曲")
    async def song_command(
        self,
        ctx: discord.ApplicationContext,
        keyword: discord.Option(str, "歌曲名称或关键词", required=True),
    ):
        await self._forward(ctx, f"点歌 {str(keyword)[:160]}")


class VoiceCog(commands.Cog):
    """Local voice-channel controls; Core still handles all voice interpretation."""

    def __init__(self, bot: "NachoDiscordBot", adapter):
        self.bot = bot
        self.adapter = adapter

    async def _defer(self, ctx: discord.ApplicationContext) -> bool:
        if ctx.interaction.response.is_done():
            return False
        await ctx.defer(ephemeral=True)
        return True

    @discord.slash_command(name="join-vc", description="加入你当前所在的语音频道")
    async def join_voice_channel(self, ctx: discord.ApplicationContext):
        await self._defer(ctx)
        author = ctx.author
        if ctx.guild is None or getattr(author, "voice", None) is None:
            await ctx.followup.send("这里好像不给用呢...", ephemeral=True)
            return
        if not self.adapter.is_context_allowed(author, ctx.channel):
            await ctx.followup.send("这里好像不给用呢...", ephemeral=True)
            return
        channel = author.voice.channel
        if not self.adapter.is_voice_channel_allowed(author.id, channel.id):
            await ctx.followup.send("好像进不去呢...", ephemeral=True)
            return

        try:
            status, current_channel_name = await self.bot.join_voice_channel(
                ctx.guild, channel
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - show one user-facing lifecycle failure
            await ctx.followup.send(
                "欸，好像什么地方出错了呢...", ephemeral=True
            )
            return

        if status == "occupied":
            await ctx.followup.send(
                f"我已经在 {current_channel_name} 里了", ephemeral=True
            )
        elif status == "already":
            await ctx.followup.send("意外的，我已经在这里了", ephemeral=True)
        else:
            await ctx.followup.send(f"加入 {channel.name} 啦！", ephemeral=True)

    @discord.slash_command(name="leave-vc", description="离开语音频道")
    async def leave_voice_channel(self, ctx: discord.ApplicationContext):
        await self._defer(ctx)
        if ctx.guild is None or ctx.guild.voice_client is None:
            await ctx.followup.send("我还没进去呢(｀ω´)", ephemeral=True)
            return
        if not self.adapter.is_context_allowed(ctx.author, ctx.channel):
            await ctx.followup.send("这里好像用不了呢...", ephemeral=True)
            return
        try:
            await self.bot.leave_voice_channel(ctx.guild)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - clean up and report the failed leave
            self.bot.logger.warning(
                "Discord voice leave failed guild_id=%s error=%s",
                int(ctx.guild.id),
                type(exc).__name__,
            )
            await ctx.followup.send(
                "欸，好像什么地方出错了呢...", ephemeral=True
            )
            return
        await ctx.followup.send("走啦", ephemeral=True)


class NachoDiscordBot(discord.Bot):
    def __init__(
        self,
        config: AdapterConfig,
        voice_handler: VoiceHandler,
        logger: logging.Logger,
        adapter=None,
    ):
        proxy_url = (
            config.discord.proxy_url
            if config.discord.proxy_enabled and config.discord.proxy_url
            else None
        )
        # The adapter is the sole owner of this Gateway identity and its slash commands.
        super().__init__(intents=intents, proxy=proxy_url, auto_sync_commands=True)
        self.adapter_config = config
        self.voice_handler = voice_handler
        self.logger = logger
        self.transport_adapter = adapter
        self.speech_callback: Callable | None = None
        self.core_audio_stream_client = CoreAudioStreamClient(
            host=config.nachobot.host,
            port=config.nachobot.port,
            token=os.environ.get("NACHOBOT_CORE_TOKEN", ""),
        )
        self.core_audio_streams = DiscordCoreAudioStreamBridge(
            self.core_audio_stream_client, logger
        )
        self.voice_sessions: dict[int, VoiceSession] = {}
        self.tts_streams: dict[int, tuple[str, PCMStreamSource]] = {}
        self._active_sinks: set[SilenceDetectingSink] = set()
        self._owned_temp_audio: set[str] = set()
        self._voice_locks: dict[int, asyncio.Lock] = {}
        self._voice_recovery_tasks: dict[int, asyncio.Task] = {}
        self._voice_recovery_intent: set[int] = set()
        self._voice_expected_disconnects: dict[int, tuple[object, int]] = {}
        self._voice_transient_disconnects: dict[int, deque[TransientVoiceRemoval]] = {}
        self._voice_connect_attempts: dict[int, VoiceConnectAttempt] = {}
        self._voice_gateway_online = True
        self._voice_closing = False
        if adapter is not None:
            self.add_cog(DiscordTransportCog(self, adapter))
            if config.voice.enabled:
                self.add_cog(VoiceCog(self, adapter))

    async def on_ready(self):
        self._voice_gateway_online = True
        await self._restore_voice_recovery()
        self.logger.info("Discord Gateway is ready; native commands are managed by this adapter")

    async def on_message(self, message: discord.Message):
        if self.transport_adapter is not None:
            await self.transport_adapter.handle_discord_message(message)

    async def on_voice_state_update(self, member, before, after):
        if not self.user or int(member.id) != int(self.user.id):
            return
        before_id = int(before.channel.id) if before.channel else None
        after_id = int(after.channel.id) if after.channel else None
        guild_id = int(member.guild.id)
        self._ensure_voice_runtime_state()

        # Pycord's cleanup=False retry path can itself emit channel=None. Its
        # local VoiceClient marks that exact client/channel before the Gateway
        # update, and the correlated event is consumed above. An unmarked
        # removal ends the user's join intent.
        if before_id is not None and after_id is None:
            expected = self._voice_expected_disconnects.get(guild_id)
            current_voice_client = getattr(member.guild, "voice_client", None)
            if self._consume_transient_voice_disconnect(
                guild_id, before_id, current_voice_client
            ):
                return
            if expected is not None and before_id == expected[1]:
                if (
                    current_voice_client is expected[0]
                    or current_voice_client is None
                    or self._voice_is_connected(current_voice_client)
                ):
                    self._voice_expected_disconnects.pop(guild_id, None)
                    return
                # A different, disconnected client is present. This event
                # belongs to its kick/removal, not the older cleanup marker.
                self._voice_expected_disconnects.pop(guild_id, None)
            self._voice_recovery_intent.discard(guild_id)
            await self._cancel_voice_recovery(guild_id)
            async with self._voice_lock(guild_id):
                self._voice_recovery_intent.discard(guild_id)
                await self._cancel_voice_recovery(guild_id)
                await self._invalidate_voice_session_unlocked(guild_id)
                voice_client = getattr(member.guild, "voice_client", None)
                if voice_client is not None:
                    await self._disconnect_voice_client(
                        member.guild, voice_client, guild_id=guild_id
                    )
            return

        if guild_id in self._voice_recovery_intent:
            # A channel move can invalidate the receive generation. The
            # guild-owned recovery task will wait for the voice client to be
            # ready, then install one fresh sink.
            self._ensure_voice_recovery(guild_id)

    async def on_disconnect(self):
        self._ensure_voice_runtime_state()
        self._voice_gateway_online = False
        await self._cancel_all_voice_recovery()
        for guild_id in set(self.voice_sessions) | set(self._voice_recovery_intent):
            async with self._voice_lock(guild_id):
                await self._invalidate_voice_session_unlocked(guild_id)

    async def on_resumed(self):
        self._voice_gateway_online = True
        await self._restore_voice_recovery()

    def set_speech_callback(self, callback: Callable):
        self.speech_callback = callback

    def _ensure_voice_runtime_state(self) -> None:
        # A few tests and maintenance hooks build the bot with __new__ to avoid
        # opening Core or Gateway clients. Keep those safe while production
        # instances initialize these fields in __init__.
        if not hasattr(self, "_voice_locks"):
            self._voice_locks = {}
        if not hasattr(self, "_voice_recovery_tasks"):
            self._voice_recovery_tasks = {}
        if not hasattr(self, "_voice_recovery_intent"):
            self._voice_recovery_intent = set()
        if not hasattr(self, "_voice_expected_disconnects"):
            self._voice_expected_disconnects = {}
        if not hasattr(self, "_voice_transient_disconnects"):
            self._voice_transient_disconnects = {}
        if not hasattr(self, "_voice_connect_attempts"):
            self._voice_connect_attempts = {}
        if not hasattr(self, "_voice_gateway_online"):
            self._voice_gateway_online = True
        if not hasattr(self, "_voice_closing"):
            self._voice_closing = False

    def _voice_lock(self, guild_id: int) -> asyncio.Lock:
        self._ensure_voice_runtime_state()
        guild_id = int(guild_id)
        lock = self._voice_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._voice_locks[guild_id] = lock
        return lock

    @staticmethod
    def _voice_phase(voice_client) -> tuple[str, str]:
        """Return only an allowlisted SDK state and a safe coarse phase."""
        connection = getattr(voice_client, "_connection", None)
        state = getattr(connection, "state", None)
        name = getattr(state, "name", None)
        if name not in VOICE_PHASE_NAMES:
            return "unknown", "unknown"
        return VOICE_PHASE_NAMES[name], name

    @staticmethod
    def _voice_is_connected(voice_client) -> bool:
        try:
            return bool(voice_client and voice_client.is_connected())
        except Exception:  # noqa: BLE001 - connection state may change during teardown
            return False

    def _voice_is_ready(self, voice_client, channel_id: int) -> bool:
        if not self._voice_is_connected(voice_client):
            return False
        channel = getattr(voice_client, "channel", None)
        if channel is None:
            return False
        try:
            if int(channel.id) != int(channel_id):
                return False
        except (AttributeError, TypeError, ValueError):
            return False
        connection = getattr(voice_client, "_connection", None)
        state = getattr(connection, "state", None)
        name = getattr(state, "name", None)
        return name in (None, "connected")

    async def _wait_for_voice_ready(
        self,
        guild,
        voice_client,
        channel_id: int,
        *,
        timeout: float | None = None,
        gateway_required: bool = True,
    ) -> None:
        if timeout is None:
            timeout = VOICE_CONNECT_TIMEOUT
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if gateway_required and not self._voice_gateway_online:
                phase, state_name = self._voice_phase(voice_client)
                raise VoiceLifecycleError(
                    "Discord Gateway is offline",
                    phase=phase,
                    connection_state=state_name,
                )
            if getattr(guild, "voice_client", None) is not voice_client:
                phase, state_name = self._voice_phase(voice_client)
                raise VoiceLifecycleError(
                    "Voice client changed during recovery",
                    phase=phase,
                    connection_state=state_name,
                )
            if self._voice_is_ready(voice_client, channel_id):
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                phase, state_name = self._voice_phase(voice_client)
                raise VoiceLifecycleError(
                    "Voice readiness timed out",
                    phase=phase,
                    connection_state=state_name,
                )
            await asyncio.sleep(min(0.2, remaining))

    def _log_voice_failure(
        self,
        guild_id: int,
        channel_id: int,
        attempt_id: str,
        voice_client,
        error: BaseException,
        *,
        action: str,
    ) -> None:
        phase = getattr(error, "phase", None)
        state_name = getattr(error, "connection_state", "unknown")
        if phase is None:
            phase, state_name = self._voice_phase(voice_client)
        else:
            if state_name not in VOICE_PHASE_NAMES:
                _, state_name = self._voice_phase(voice_client)
            if phase not in set(VOICE_PHASE_NAMES.values()) | {"unknown"}:
                phase = "unknown"
        self.logger.warning(
            "Discord voice %s failed guild_id=%s channel_id=%s attempt=%s "
            "phase=%s connection_state=%s error=%s",
            action,
            int(guild_id),
            int(channel_id),
            attempt_id[:8],
            phase,
            state_name,
            type(error).__name__,
        )

    async def _disconnect_voice_client(self, guild, voice_client, *, guild_id: int) -> None:
        # Identity check prevents cleanup from an old connector/recovery attempt
        # disconnecting a newer client installed for the same guild.
        if getattr(guild, "voice_client", None) is not voice_client:
            return
        marker = (voice_client, int(getattr(getattr(voice_client, "channel", None), "id", -1)))
        self._voice_expected_disconnects[int(guild_id)] = marker
        try:
            await voice_client.disconnect(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask voice teardown
            self.logger.debug(
                "Discord voice disconnect cleanup failed guild_id=%s error=%s",
                int(guild_id),
                type(exc).__name__,
            )
        finally:
            asyncio.get_running_loop().call_later(
                5.0,
                self._clear_expected_voice_disconnect,
                int(guild_id),
                marker,
            )

    def _clear_expected_voice_disconnect(self, guild_id: int, marker) -> None:
        if self._voice_expected_disconnects.get(int(guild_id)) == marker:
            self._voice_expected_disconnects.pop(int(guild_id), None)

    def _mark_transient_voice_disconnect(self, voice_client) -> None:
        """Record the exact SDK retry client before it sends channel=None."""
        self._ensure_voice_runtime_state()
        guild = getattr(voice_client, "guild", None)
        channel = getattr(voice_client, "channel", None)
        if guild is None or channel is None:
            return

        guild_id = int(guild.id)
        channel_id = int(channel.id)
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._prune_transient_voice_disconnects(guild_id, now)

        attempt_id = None
        attempt = self._voice_connect_attempts.get(guild_id)
        if attempt is not None and attempt.channel_id == channel_id:
            if attempt.voice_client is None or attempt.voice_client is voice_client:
                attempt.voice_client = voice_client
                attempt_id = attempt.attempt_id

        marker = TransientVoiceRemoval(
            voice_client=voice_client,
            channel_id=channel_id,
            expires_at=now + VOICE_TRANSIENT_REMOVAL_TTL,
            attempt_id=attempt_id,
        )
        self._voice_transient_disconnects.setdefault(guild_id, deque()).append(marker)
        loop.call_later(
            VOICE_TRANSIENT_REMOVAL_TTL,
            self._expire_transient_voice_disconnect,
            guild_id,
            marker,
        )

    def _prune_transient_voice_disconnects(self, guild_id: int, now: float) -> None:
        records = self._voice_transient_disconnects.get(int(guild_id))
        if records is None:
            return
        live = [record for record in records if record.expires_at > now]
        if live:
            self._voice_transient_disconnects[int(guild_id)] = deque(live)
        else:
            self._voice_transient_disconnects.pop(int(guild_id), None)

    def _expire_transient_voice_disconnect(
        self, guild_id: int, marker: TransientVoiceRemoval
    ) -> None:
        records = self._voice_transient_disconnects.get(int(guild_id))
        if records is None:
            return
        live = [record for record in records if record is not marker]
        if live:
            self._voice_transient_disconnects[int(guild_id)] = deque(live)
        else:
            self._voice_transient_disconnects.pop(int(guild_id), None)

    def _consume_transient_voice_disconnect(
        self, guild_id: int, channel_id: int, voice_client
    ) -> bool:
        if voice_client is None:
            return False
        guild_id = int(guild_id)
        self._prune_transient_voice_disconnects(
            guild_id, asyncio.get_running_loop().time()
        )
        records = self._voice_transient_disconnects.get(guild_id)
        if records is None:
            return False
        current_attempt = self._voice_connect_attempts.get(guild_id)
        for index, marker in enumerate(records):
            if (
                marker.voice_client is voice_client
                and marker.channel_id == int(channel_id)
                and (
                    marker.attempt_id is None
                    or current_attempt is None
                    or current_attempt.attempt_id == marker.attempt_id
                )
            ):
                del records[index]
                if not records:
                    self._voice_transient_disconnects.pop(guild_id, None)
                return True
        return False

    async def _connect_voice_channel(self, guild, channel, attempt_id: str):
        """Connect once, sampling only safe SDK phase names while it handshakes."""
        guild_id = int(guild.id)
        attempt = VoiceConnectAttempt(attempt_id, int(channel.id))
        self._voice_connect_attempts[guild_id] = attempt
        connector = asyncio.create_task(
            channel.connect(
                timeout=VOICE_CONNECT_TIMEOUT,
                reconnect=True,
                cls=RetryAwareVoiceClient,
            ),
            name=f"discord-voice-connect-{guild_id}-{attempt_id[:8]}",
        )
        candidate = None
        last_phase = "unknown"
        last_state = "unknown"
        deadline = asyncio.get_running_loop().time() + VOICE_CONNECT_TIMEOUT
        try:
            while not connector.done():
                current = getattr(guild, "voice_client", None)
                if current is not None:
                    candidate = current
                    attempt.voice_client = current
                    observed_phase, observed_state = self._voice_phase(candidate)
                    if observed_phase not in ("unknown", "disconnected"):
                        last_phase = observed_phase
                        last_state = observed_state
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise VoiceLifecycleError(
                        "Discord voice connection timed out",
                        phase=last_phase,
                        connection_state=last_state,
                    )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(connector), timeout=min(0.2, remaining)
                    )
                except TimeoutError:
                    continue
            voice_client = await connector
            attempt.voice_client = voice_client
            return voice_client
        except asyncio.CancelledError:
            connector.cancel()
            await asyncio.gather(connector, return_exceptions=True)
            current = getattr(guild, "voice_client", None)
            candidate = candidate or current
            if candidate is not None:
                await self._disconnect_voice_client(
                    guild, candidate, guild_id=guild_id
                )
            raise
        except Exception as exc:
            connector.cancel() if not connector.done() else None
            await asyncio.gather(connector, return_exceptions=True)
            current = getattr(guild, "voice_client", None)
            candidate = candidate or current
            phase = last_phase
            state_name = last_state
            if candidate is not None:
                observed_phase, observed_state = self._voice_phase(candidate)
                if observed_phase not in ("unknown", "disconnected"):
                    phase = observed_phase
                    state_name = observed_state
                await self._disconnect_voice_client(
                    guild, candidate, guild_id=guild_id
                )
            raise VoiceLifecycleError(
                "Discord voice connection failed",
                phase=phase,
                connection_state=state_name,
            ) from exc
        finally:
            if self._voice_connect_attempts.get(guild_id) is attempt:
                self._voice_connect_attempts.pop(guild_id, None)

    def _ensure_voice_recovery(self, guild_id: int) -> None:
        self._ensure_voice_runtime_state()
        guild_id = int(guild_id)
        if (
            self._voice_closing
            or not self._voice_gateway_online
            or guild_id not in self._voice_recovery_intent
        ):
            return
        task = self._voice_recovery_tasks.get(guild_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._watch_voice_session(guild_id),
            name=f"discord-voice-recovery-{guild_id}",
        )
        self._voice_recovery_tasks[guild_id] = task
        session = self.voice_sessions.get(guild_id)
        if session is not None:
            session.watcher_task = task

        def forget(completed: asyncio.Task, *, key: int = guild_id) -> None:
            if self._voice_recovery_tasks.get(key) is completed:
                self._voice_recovery_tasks.pop(key, None)

        task.add_done_callback(forget)

    async def _cancel_voice_recovery(self, guild_id: int) -> None:
        task = self._voice_recovery_tasks.pop(int(guild_id), None)
        if task is None or task is asyncio.current_task() or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _cancel_all_voice_recovery(self) -> None:
        current = asyncio.current_task()
        tasks = list(self._voice_recovery_tasks.values())
        self._voice_recovery_tasks.clear()
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not current), return_exceptions=True
        )

    async def _restore_voice_recovery(self) -> None:
        self._ensure_voice_runtime_state()
        if self._voice_closing:
            return
        for guild_id in list(self._voice_recovery_intent):
            guild = self.get_guild(guild_id)
            voice_client = getattr(guild, "voice_client", None) if guild else None
            if voice_client is None or getattr(voice_client, "channel", None) is None:
                self._voice_recovery_intent.discard(guild_id)
                continue
            self._ensure_voice_recovery(guild_id)

    async def join_voice_channel(self, guild, channel) -> tuple[str, str | None]:
        """Serialize a user join/move and start one receive session when ready."""
        self._ensure_voice_runtime_state()
        guild_id = int(guild.id)
        channel_id = int(channel.id)
        attempt_id = uuid.uuid4().hex
        async with self._voice_lock(guild_id):
            if self._voice_closing or not self._voice_gateway_online:
                raise VoiceLifecycleError("Discord Gateway is unavailable")

            voice_client = getattr(guild, "voice_client", None)
            connected_before = voice_client
            move_started = False
            try:
                if voice_client is not None and getattr(voice_client, "channel", None) is not None:
                    old_channel = voice_client.channel
                    old_channel_id = int(old_channel.id)
                    if old_channel_id != channel_id:
                        if self._voice_is_connected(voice_client) and len(
                            getattr(old_channel, "members", ())
                        ) > 1:
                            return "occupied", str(getattr(old_channel, "name", "that channel"))
                        if not self._voice_is_ready(voice_client, old_channel_id):
                            try:
                                await self._wait_for_voice_ready(
                                    guild, voice_client, old_channel_id
                                )
                            except VoiceLifecycleError:
                                await self._invalidate_voice_session_unlocked(guild_id)
                                await self._disconnect_voice_client(
                                    guild, voice_client, guild_id=guild_id
                                )
                                voice_client = None
                        if voice_client is not None:
                            await self._invalidate_voice_session_unlocked(guild_id)
                            move_started = True
                            try:
                                await asyncio.wait_for(
                                    voice_client.move_to(channel),
                                    timeout=VOICE_CONNECT_TIMEOUT,
                                )
                            except TimeoutError as exc:
                                phase, state_name = self._voice_phase(voice_client)
                                raise VoiceLifecycleError(
                                    "Discord voice move timed out",
                                    phase=phase,
                                    connection_state=state_name,
                                ) from exc
                    else:
                        session = self.voice_sessions.get(guild_id)
                        if (
                            session
                            and self._session_is_current(session)
                            and voice_client.is_recording()
                        ):
                            return "already", None
                        if not self._voice_is_ready(voice_client, channel_id):
                            try:
                                await self._wait_for_voice_ready(
                                    guild, voice_client, channel_id
                                )
                            except VoiceLifecycleError:
                                await self._invalidate_voice_session_unlocked(guild_id)
                                await self._disconnect_voice_client(
                                    guild, voice_client, guild_id=guild_id
                                )
                                voice_client = None

                if voice_client is None:
                    voice_client = await self._connect_voice_channel(
                        guild, channel, attempt_id
                    )

                await self._wait_for_voice_ready(guild, voice_client, channel_id)
                if self._voice_closing or not self._voice_gateway_online:
                    raise VoiceLifecycleError("Discord Gateway is unavailable")
                if (
                    guild_id in self.voice_sessions
                    and self.voice_sessions[guild_id].voice_client is not voice_client
                ):
                    await self._invalidate_voice_session_unlocked(guild_id)
                session = await self._start_listening_unlocked(voice_client, guild_id)
                if session is None:
                    phase, state_name = self._voice_phase(voice_client)
                    raise VoiceLifecycleError(
                        "Discord receive session did not start",
                        phase=phase,
                        connection_state=state_name,
                    )
                if self._voice_closing or not self._voice_gateway_online:
                    raise VoiceLifecycleError("Discord Gateway is unavailable")
                self._voice_recovery_intent.add(guild_id)
                self._ensure_voice_recovery(guild_id)
                return "joined", None
            except asyncio.CancelledError:
                if connected_before is None or move_started:
                    self._voice_recovery_intent.discard(guild_id)
                    await self._cancel_voice_recovery(guild_id)
                    await self._invalidate_voice_session_unlocked(guild_id)
                cleanup_client = voice_client
                if cleanup_client is None:
                    cleanup_client = getattr(guild, "voice_client", None)
                if cleanup_client is not None and (
                    connected_before is None
                    or cleanup_client is not connected_before
                    or move_started
                ):
                    await self._disconnect_voice_client(
                        guild, cleanup_client, guild_id=guild_id
                    )
                raise
            except Exception as exc:
                self._voice_recovery_intent.discard(guild_id)
                await self._cancel_voice_recovery(guild_id)
                self._log_voice_failure(
                    guild_id,
                    channel_id,
                    attempt_id,
                    voice_client or getattr(guild, "voice_client", None),
                    exc,
                    action="join",
                )
                await self._invalidate_voice_session_unlocked(guild_id)
                cleanup_client = voice_client or getattr(guild, "voice_client", None)
                if cleanup_client is not None:
                    await self._disconnect_voice_client(
                        guild, cleanup_client, guild_id=guild_id
                    )
                raise

    async def leave_voice_channel(self, guild) -> None:
        self._ensure_voice_runtime_state()
        guild_id = int(guild.id)
        self._voice_recovery_intent.discard(guild_id)
        await self._cancel_voice_recovery(guild_id)
        async with self._voice_lock(guild_id):
            self._voice_recovery_intent.discard(guild_id)
            await self._cancel_voice_recovery(guild_id)
            await self._invalidate_voice_session_unlocked(guild_id)
            voice_client = getattr(guild, "voice_client", None)
            if voice_client is not None:
                await self._disconnect_voice_client(
                    guild, voice_client, guild_id=guild_id
                )

    def _session_is_current(self, session: VoiceSession) -> bool:
        return (
            not session.closed
            and self.voice_sessions.get(session.guild_id) is session
            and session.voice_client.is_connected()
            and session.voice_client.channel is not None
            and int(session.voice_client.channel.id) == session.channel_id
            and getattr(session.voice_client, "ws", None) is session.voice_ws
            and str(getattr(session.voice_client, "session_id", "") or "") == str(session.voice_session_id or "")
        )

    def get_voice_session(self, channel_id: str | int, generation: str | None = None) -> VoiceSession | None:
        try:
            native_channel = int(channel_id)
        except (TypeError, ValueError):
            return None
        for session in self.voice_sessions.values():
            if session.channel_id == native_channel and self._session_is_current(session):
                if generation is None or session.generation == generation:
                    return session
        return None

    async def close(self):
        self._ensure_voice_runtime_state()
        self._voice_closing = True
        self._voice_recovery_intent.clear()
        await self._cancel_all_voice_recovery()
        try:
            guild_ids = set(self.voice_sessions)
            for voice_client in list(getattr(self, "voice_clients", ())):
                guild = getattr(voice_client, "guild", None)
                if guild is not None:
                    guild_ids.add(int(guild.id))
            for guild_id in guild_ids:
                try:
                    async with self._voice_lock(guild_id):
                        await self._invalidate_voice_session_unlocked(guild_id)
                        guild = self.get_guild(guild_id)
                        voice_client = getattr(guild, "voice_client", None) if guild else None
                        if voice_client is None:
                            voice_client = next(
                                (
                                    item
                                    for item in list(getattr(self, "voice_clients", ()))
                                    if int(getattr(getattr(item, "guild", None), "id", -1))
                                    == guild_id
                                ),
                                None,
                            )
                            guild = getattr(voice_client, "guild", None) if voice_client else None
                        if guild is not None and voice_client is not None:
                            await self._disconnect_voice_client(
                                guild, voice_client, guild_id=guild_id
                            )
                except Exception as exc:  # noqa: BLE001 - continue closing other guilds
                    self.logger.warning(
                        "Discord voice session cleanup failed (%s)",
                        type(exc).__name__,
                    )
            for _, source in list(self.tts_streams.values()):
                source.abort()
            self.tts_streams.clear()
        finally:
            try:
                await super().close()
            finally:
                try:
                    await self.core_audio_streams.abort_all()
                finally:
                    await self.core_audio_stream_client.close()

    async def invalidate_voice_session(self, guild_id: int):
        guild_id = int(guild_id)
        async with self._voice_lock(guild_id):
            await self._invalidate_voice_session_unlocked(guild_id)

    async def _invalidate_voice_session_unlocked(self, guild_id: int):
        session = self.voice_sessions.pop(int(guild_id), None)
        if session is None:
            return
        session.closed = True
        try:
            # The watcher is guild-owned and intentionally survives receive
            # session invalidation. Leave, Gateway loss, kick, and shutdown
            # cancel it explicitly before entering this locked cleanup path.
            try:
                self.abort_tts_stream(session.channel_id)
            except Exception as exc:
                self.logger.debug("Discord TTS stream cleanup failed (%s)", type(exc).__name__)
            try:
                if session.voice_client.is_playing() or session.voice_client.is_paused():
                    session.voice_client.stop()
            except Exception as exc:
                self.logger.debug("Discord voice playback stop failed (%s)", type(exc).__name__)
            for item in list(session.queue):
                if item.started and not item.started.done():
                    item.started.set_result(False)
                if item.cleanup_owned:
                    self._cleanup_audio_file(item.path)
            session.queue.clear()
            for item in (session.current_audio, session.interrupted_audio):
                if item and item.cleanup_owned:
                    self._cleanup_audio_file(item.path)
            session.current_audio = None
            session.interrupted_audio = None
            try:
                if session.voice_client.is_recording():
                    session.voice_client.stop_recording()
            except Exception:
                self.logger.debug("Discord recording stop failed", exc_info=True)
            if session.sink is not None:
                try:
                    await session.sink.aclose()
                except Exception as exc:  # noqa: BLE001 - preserve failed-start cleanup
                    self.logger.debug(
                        "Discord voice sink cleanup failed (%s)", type(exc).__name__
                    )
                finally:
                    self._active_sinks.discard(session.sink)
        finally:
            stream_ids = list(session.stream_ids)
            session.stream_ids.clear()
            session.capture_scopes.clear()
            if stream_ids:
                await asyncio.gather(
                    *(self.core_audio_streams.abort(stream_id) for stream_id in stream_ids),
                    return_exceptions=True,
                )

    @staticmethod
    def _stream_id(session: VoiceSession, capture_id: str) -> str:
        return f"discord:{session.channel_id}:{session.generation}:{capture_id}"

    def _capture_allowed(self, session: VoiceSession, user_id: int) -> bool:
        if not self.adapter_config.voice.enabled or not self._session_is_current(session):
            return False
        adapter = self.transport_adapter
        return adapter is None or adapter.is_voice_user_allowed(user_id, session.channel_id)

    async def start_listening(self, vc: discord.VoiceClient, guild_id: int) -> VoiceSession | None:
        guild_id = int(guild_id)
        async with self._voice_lock(guild_id):
            channel = getattr(vc, "channel", None) if vc else None
            if channel is None or not self._voice_is_ready(vc, int(channel.id)):
                return None
            session = await self._start_listening_unlocked(vc, guild_id)
            if session is not None and guild_id in self._voice_recovery_intent:
                self._ensure_voice_recovery(guild_id)
                session.watcher_task = self._voice_recovery_tasks.get(guild_id)
            return session

    async def _start_listening_unlocked(
        self, vc: discord.VoiceClient, guild_id: int
    ) -> VoiceSession | None:
        channel = getattr(vc, "channel", None) if vc else None
        if (
            not self.adapter_config.voice.enabled
            or channel is None
            or not self._voice_is_ready(vc, int(channel.id))
        ):
            return None
        current = self.voice_sessions.get(int(guild_id))
        channel_id = int(channel.id)
        if (
            current
            and current.channel_id == channel_id
            and current.voice_client is vc
            and self._session_is_current(current)
            and vc.is_recording()
        ):
            return current
        if current:
            await self._invalidate_voice_session_unlocked(int(guild_id))
        elif vc.is_recording():
            try:
                vc.stop_recording()
            except Exception:
                self.logger.debug("Stale Discord recording could not be stopped", exc_info=True)

        session = VoiceSession(
            guild_id=int(guild_id),
            channel_id=channel_id,
            generation=uuid.uuid4().hex,
            voice_client=vc,
            voice_ws=getattr(vc, "ws", None),
            voice_session_id=str(getattr(vc, "session_id", "") or ""),
        )
        self.voice_sessions[int(guild_id)] = session

        def key(capture_id: str) -> str:
            return self._stream_id(session, capture_id)

        def snapshot(capture_id: str, user_id: int) -> tuple[int, str] | None:
            if not self._capture_allowed(session, user_id):
                return None
            logical_user = (
                self.transport_adapter.identity_map.logical_user(user_id)
                if self.transport_adapter is not None
                else str(user_id)
            )
            logical_channel = (
                self.transport_adapter.identity_map.logical_channel(channel_id)
                if self.transport_adapter is not None
                else str(channel_id)
            )
            import hashlib
            import json

            seed = json.dumps(
                [logical_channel, logical_user, session.generation, capture_id],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            scope = "dsc_" + hashlib.sha256(seed).hexdigest()
            session.capture_scopes[capture_id] = (int(user_id), scope)
            session.stream_ids.add(key(capture_id))
            return int(user_id), scope

        def capture_start(capture_id: str, user_id: int):
            if snapshot(capture_id, user_id) is None:
                return False
            if self.voice_handler.start_stream(key(capture_id)):
                return True
            session.capture_scopes.pop(capture_id, None)
            session.stream_ids.discard(key(capture_id))
            return False

        def capture_audio(capture_id: str, pcm_data: bytes):
            if self._session_is_current(session):
                self.voice_handler.accept_pcm(key(capture_id), pcm_data)

        async def capture_finish(capture_id: str):
            if not self._session_is_current(session):
                return None
            return await asyncio.to_thread(self.voice_handler.finish_stream, key(capture_id))

        async def capture_abort(capture_id: str):
            await asyncio.to_thread(self.voice_handler.abort_stream, key(capture_id))
            session.capture_scopes.pop(capture_id, None)
            session.stream_ids.discard(key(capture_id))

        async def stream_start(capture_id: str, user_id: int):
            entry = session.capture_scopes.get(capture_id)
            if entry is None:
                entry = snapshot(capture_id, user_id)
            if entry is None or not self._session_is_current(session):
                return False
            return await self.core_audio_streams.start(key(capture_id), entry[1])

        async def stream_audio(capture_id: str, pcm_data: bytes):
            if not self._session_is_current(session):
                await self.core_audio_streams.abort(key(capture_id))
                return False
            return await self.core_audio_streams.send_pcm(key(capture_id), pcm_data)

        async def stream_finish(capture_id: str):
            if not self._session_is_current(session):
                await self.core_audio_streams.abort(key(capture_id))
                return None
            return await self.core_audio_streams.finish(key(capture_id))

        async def stream_abort(capture_id: str):
            await self.core_audio_streams.abort(key(capture_id))

        async def speech_start(user_id: int):
            if not self._capture_allowed(session, user_id):
                return
            session.is_user_speaking = True
            self.abort_tts_stream(session.channel_id)
            if session.current_audio and (vc.is_playing() or vc.is_paused()):
                session.interrupted_audio = session.current_audio
                stop_playback_only(vc)

        async def sink_callback(user_id: int, voice_data: str | None, result_id=None, capture_id=None):
            capture_key = str(capture_id)
            snapshot_value = session.capture_scopes.pop(capture_key, None)
            session.stream_ids.discard(key(capture_key))
            if snapshot_value is None or not self._session_is_current(session):
                return
            session.is_user_speaking = False
            if voice_data and self.speech_callback:
                await self.speech_callback(
                    session,
                    int(user_id),
                    voice_data,
                    result_id,
                    str(capture_id),
                    snapshot_value[1],
                )
            self._play_next(session)

        sink = SilenceDetectingSink(
            callback=sink_callback,
            on_speech_start_callback=speech_start,
            on_stream_start_callback=stream_start,
            on_stream_audio_callback=stream_audio,
            on_stream_finish_callback=stream_finish,
            on_stream_abort_callback=stream_abort,
            on_capture_start_callback=capture_start,
            on_capture_audio_callback=capture_audio,
            on_capture_finish_callback=capture_finish,
            on_capture_abort_callback=capture_abort,
            capture_filter=lambda user_id: self._capture_allowed(session, user_id),
            config=self.adapter_config.voice,
        )
        session.sink = sink
        try:
            vc.start_recording(sink, self._on_recording_stopped)
        except Exception:
            self.voice_sessions.pop(int(guild_id), None)
            session.closed = True
            try:
                await sink.aclose()
            except Exception as exc:  # noqa: BLE001 - preserve failed-start cleanup
                self.logger.debug(
                    "Discord voice sink rollback failed (%s)", type(exc).__name__
                )
            raise
        self._active_sinks.add(sink)
        session.watcher_task = self._voice_recovery_tasks.get(int(guild_id))
        return session

    async def _watch_voice_session(self, guild_id: int):
        """Keep a joined guild's receive sink alive across VC websocket resume."""
        guild_id = int(guild_id)
        try:
            while (
                not self._voice_closing
                and self._voice_gateway_online
                and guild_id in self._voice_recovery_intent
            ):
                await asyncio.sleep(VOICE_RECOVERY_POLL_INTERVAL)
                if (
                    self._voice_closing
                    or not self._voice_gateway_online
                    or guild_id not in self._voice_recovery_intent
                ):
                    return

                session = self.voice_sessions.get(guild_id)
                voice_client = session.voice_client if session else None
                if (
                    session is not None
                    and self._session_is_current(session)
                    and voice_client.is_recording()
                ):
                    continue

                async with self._voice_lock(guild_id):
                    if (
                        self._voice_closing
                        or not self._voice_gateway_online
                        or guild_id not in self._voice_recovery_intent
                    ):
                        return
                    guild = self.get_guild(guild_id)
                    voice_client = getattr(guild, "voice_client", None) if guild else None
                    session = self.voice_sessions.get(guild_id)
                    if (
                        session is not None
                        and self._session_is_current(session)
                        and session.voice_client.is_recording()
                    ):
                        continue
                    if session is not None:
                        await self._invalidate_voice_session_unlocked(guild_id)
                    if (
                        guild is None
                        or voice_client is None
                        or getattr(voice_client, "channel", None) is None
                    ):
                        self._voice_recovery_intent.discard(guild_id)
                        self.logger.warning(
                            "Discord voice recovery stopped guild_id=%s "
                            "channel_id=unknown attempt=unknown phase=unknown "
                            "connection_state=unknown error=VoiceClientUnavailable",
                            guild_id,
                        )
                        return

                    channel_id = int(voice_client.channel.id)
                    attempt_id = uuid.uuid4().hex
                    try:
                        await self._wait_for_voice_ready(
                            guild, voice_client, channel_id
                        )
                        fresh = await self._start_listening_unlocked(
                            voice_client, guild_id
                        )
                        if fresh is None:
                            phase, state_name = self._voice_phase(voice_client)
                            raise VoiceLifecycleError(
                                "Discord receive session did not restart",
                                phase=phase,
                                connection_state=state_name,
                            )
                        fresh.watcher_task = asyncio.current_task()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - failed recovery must stop cleanly
                        self._log_voice_failure(
                            guild_id,
                            channel_id,
                            attempt_id,
                            voice_client,
                            exc,
                            action="recovery",
                        )
                        self._voice_recovery_intent.discard(guild_id)
                        await self._invalidate_voice_session_unlocked(guild_id)
                        await self._disconnect_voice_client(
                            guild, voice_client, guild_id=guild_id
                        )
                        return
        except asyncio.CancelledError:
            raise

    async def _on_recording_stopped(self, sink: SilenceDetectingSink, *args):
        await sink.aclose()
        self._active_sinks.discard(sink)
        for guild_id, session in list(self.voice_sessions.items()):
            if session.sink is sink and guild_id in self._voice_recovery_intent:
                self._ensure_voice_recovery(guild_id)

    def _play_next(self, session: VoiceSession, error=None):
        """Attempt to start queued audio without completing the current item.

        Completion is handled only by ``_audio_attempt_finished`` with the
        item and attempt token captured by that specific ``VoiceClient.play``.
        This prevents a queue notification from freeing an active owned source
        and makes callbacks from interrupted playback harmless after a resume.
        """
        if not self._session_is_current(session):
            return
        if error:
            self.logger.debug("Ignoring unspecific Discord voice queue notification (%s)", type(error).__name__)
        if session.is_user_speaking:
            return
        vc = session.voice_client
        active_tts = self.tts_streams.get(session.channel_id)
        if active_tts and active_tts[0] == session.generation:
            return
        if vc.is_playing() or vc.is_paused():
            return

        item = session.current_audio
        if item is not None:
            if session.interrupted_audio is not item:
                # An active attempt owns this item until its matching callback
                # reports completion. Do not infer completion from is_playing.
                return
            session.interrupted_audio = None
        else:
            if not session.queue:
                return
            item = session.queue.popleft()
            session.current_audio = item

        attempt = uuid.uuid4().hex
        session.current_playback_attempt = attempt
        try:
            source = discord.FFmpegPCMAudio(item.path, executable=resolve_ffmpeg_executable())

            def after_callback(exc):
                self.loop.call_soon_threadsafe(
                    self._audio_attempt_finished,
                    session,
                    item,
                    attempt,
                    exc,
                )

            vc.play(source, after=after_callback)
            if item.started and not item.started.done():
                item.started.set_result(True)
        except Exception as exc:
            if session.current_audio is item and session.current_playback_attempt == attempt:
                session.current_audio = None
                session.current_playback_attempt = None
                if session.interrupted_audio is item:
                    session.interrupted_audio = None
            if item.started and not item.started.done():
                item.started.set_result(False)
            if item.cleanup_owned:
                self._cleanup_audio_file(item.path)
            self.logger.warning("Discord voice playback failed (%s)", type(exc).__name__)
            self.loop.call_soon(self._play_next, session)

    def _audio_attempt_finished(
        self,
        session: VoiceSession,
        item: AudioItem,
        attempt: str,
        error=None,
    ):
        """Complete one playback attempt without touching a newer attempt."""
        if not self._session_is_current(session):
            return
        if (
            session.current_audio is not item
            or session.current_playback_attempt != attempt
        ):
            return

        session.current_playback_attempt = None
        if session.interrupted_audio is item:
            # Speech-start/TTS interruption retains this owned source for a
            # later attempt, including when the after callback races speech end.
            self._play_next(session)
            return

        session.current_audio = None
        if error:
            self.logger.warning("Discord voice playback ended with an error (%s)", type(error).__name__)
        if item.cleanup_owned:
            self._cleanup_audio_file(item.path)
        self._play_next(session)

    def own_temp_audio(self, audio_source: str) -> None:
        self._owned_temp_audio.add(str(Path(audio_source).resolve()))

    def _cleanup_audio_file(self, audio_source: str) -> None:
        try:
            path = str(Path(audio_source).resolve())
            if path in self._owned_temp_audio:
                os.remove(path)
                self._owned_temp_audio.discard(path)
        except OSError:
            pass

    def _remove_queued_audio(self, session: VoiceSession, item: AudioItem) -> None:
        try:
            session.queue.remove(item)
        except ValueError:
            return
        if item.started and not item.started.done():
            item.started.set_result(False)
        if item.cleanup_owned:
            self._cleanup_audio_file(item.path)

    async def speak(
        self,
        channel_id: int | str,
        audio_source: str,
        generation: str | None = None,
        *,
        cleanup: bool = True,
        wait_until_started: bool = False,
    ):
        session = self.get_voice_session(channel_id, generation)
        if session is None:
            if cleanup and str(Path(audio_source).resolve()) in self._owned_temp_audio:
                self._cleanup_audio_file(audio_source)
            return False
        loop = asyncio.get_running_loop()
        started = loop.create_future() if wait_until_started else None
        item = AudioItem(
            path=str(audio_source),
            cleanup_owned=bool(cleanup and str(Path(audio_source).resolve()) in self._owned_temp_audio),
            started=started,
        )
        if len(session.queue) >= 5:
            dropped = session.queue.popleft()
            if dropped.started and not dropped.started.done():
                dropped.started.set_result(False)
            if dropped.cleanup_owned:
                self._cleanup_audio_file(dropped.path)
        session.queue.append(item)
        self._play_next(session)
        if started:
            try:
                return await asyncio.wait_for(started, timeout=120.0)
            except asyncio.TimeoutError:
                self._remove_queued_audio(session, item)
                return False
            except asyncio.CancelledError:
                self._remove_queued_audio(session, item)
                raise
        return True

    def start_tts_stream(self, channel_id: int | str, generation: str, stream_id: str, sample_rate: int, channels: int, sample_width: int) -> bool:
        session = self.get_voice_session(channel_id, generation)
        if session is None:
            return False
        source = PCMStreamSource(stream_id, sample_rate, channels, sample_width)
        vc = session.voice_client
        if session.current_audio and (vc.is_playing() or vc.is_paused()):
            session.interrupted_audio = session.current_audio
        self.abort_tts_stream(session.channel_id)
        if vc.is_playing() or vc.is_paused():
            stop_playback_only(vc)
        self.tts_streams[session.channel_id] = (session.generation, source)

        def after_callback(error):
            def finished():
                entry = self.tts_streams.get(session.channel_id)
                if entry and entry[1] is source:
                    self.tts_streams.pop(session.channel_id, None)
                source.cleanup()
                if error:
                    self.logger.warning("Discord TTS stream playback ended with an error (%s)", type(error).__name__)
                self._play_next(session)
            self.loop.call_soon_threadsafe(finished)

        try:
            vc.play(source, after=after_callback)
        except Exception:
            self.tts_streams.pop(session.channel_id, None)
            source.abort()
            raise
        return True

    def feed_tts_stream(self, channel_id: int | str, generation: str, stream_id: str, seq: int, pcm: bytes) -> bool:
        session = self.get_voice_session(channel_id, generation)
        entry = self.tts_streams.get(int(channel_id)) if str(channel_id).isdecimal() else None
        if session is None or entry is None or entry[0] != generation or entry[1].stream_id != stream_id:
            return False
        try:
            entry[1].feed(seq, pcm)
        except ValueError:
            self.abort_tts_stream(channel_id, stream_id)
            return False
        return True

    def end_tts_stream(self, channel_id: int | str, generation: str, stream_id: str) -> bool:
        session = self.get_voice_session(channel_id, generation)
        entry = self.tts_streams.get(int(channel_id)) if str(channel_id).isdecimal() else None
        if session is None or entry is None or entry[0] != generation or entry[1].stream_id != stream_id:
            return False
        entry[1].finish()
        return True

    def abort_tts_stream(self, channel_id: int | str, stream_id: str | None = None) -> bool:
        try:
            key = int(channel_id)
        except (TypeError, ValueError):
            return False
        entry = self.tts_streams.get(key)
        if entry is None or (stream_id is not None and entry[1].stream_id != stream_id):
            return False
        self.tts_streams.pop(key, None)
        source = entry[1]
        source.abort()
        session = self.get_voice_session(key, entry[0])
        if (
            session
            and getattr(session.voice_client, "source", None) is source
            and (session.voice_client.is_playing() or session.voice_client.is_paused())
        ):
            stop_playback_only(session.voice_client)
        return True
