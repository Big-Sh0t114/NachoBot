import logging
from typing import Optional, Callable
from collections import deque
import discord
from discord.ext import commands

from config import AdapterConfig
from voice_handler import SilenceDetectingSink, VoiceHandler

# Define intents
intents = discord.Intents.default()
# intents.message_content = True # If we need to read text messages


class VoiceCog(commands.Cog):
    def __init__(self, bot: "NachoDiscordBot"):
        self.bot = bot

    @discord.slash_command(
        name="join-vc", description="Join your voice channel (Server Only)"
    )
    async def join_voice_channel(self, ctx: discord.ApplicationContext):
        """Join the user's voice channel."""
        try:
            if not ctx.interaction.response.is_done():
                await ctx.defer()
        except discord.HTTPException as e:
            # Ignore "Interaction has already been acknowledged" (40060)
            if e.code == 40060:
                pass
            # Ignore "Unknown interaction" (10062) - likely expired due to network lag
            elif e.code == 10062:
                self.bot.logger.warning(
                    "Interaction expired (10062) - Check network/proxy latency"
                )
                return None
            else:
                raise e
        if not ctx.author.voice:
            await ctx.followup.send("你要我进哪里啊，自己先进一个通话吧(´-ω-`)")
            return None

        channel = ctx.author.voice.channel

        if ctx.voice_client:
            if ctx.voice_client.channel.id != channel.id:
                # Check if current channel has other members (len > 1 means current channel has bot + users)
                # If only bot is in current channel (len == 1), we allow move.
                if len(ctx.voice_client.channel.members) > 1:
                    await ctx.followup.send(
                        f"我已经在 {ctx.voice_client.channel.name} 里咯"
                    )
                    return None

                await ctx.voice_client.move_to(channel)
            else:
                await ctx.followup.send("笨蛋..人家已经在这里啦(´-ω-`)")
                return ctx.voice_client
        else:
            await channel.connect()

        vc = ctx.voice_client
        await ctx.followup.send(f"加入 {channel.name} 啦！")

        # Start listening immediately
        self.bot.start_listening(vc, ctx.guild.id)
        return vc

    @discord.slash_command(
        name="leave-vc", description="Leave the voice channel (Server Only)"
    )
    async def leave_voice_channel(self, ctx: discord.ApplicationContext):
        try:
            if not ctx.interaction.response.is_done():
                await ctx.defer()
        except discord.HTTPException as e:
            if e.code == 40060:
                pass
            elif e.code == 10062:
                self.bot.logger.warning(
                    "Interaction expired (10062) - Check network/proxy latency"
                )
                return None
            else:
                raise e
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.followup.send("走啦")
        else:
            await ctx.followup.send("人家还没进语音呢(´；ω；`)")


class NachoDiscordBot(discord.Bot):
    def __init__(
        self, config: AdapterConfig, voice_handler: VoiceHandler, logger: logging.Logger
    ):
        # Configure proxy URL if env vars set (though we might redundant set it here)
        # Configure proxy URL if set in config
        proxy_url = config.discord.proxy_url if config.discord.proxy_enabled else None
        # Disable auto_sync_commands to avoid conflict with Koishi (who manages registration)
        super().__init__(intents=intents, proxy=proxy_url, auto_sync_commands=False)
        self.adapter_config = config
        self.voice_handler = voice_handler
        self.logger = logger
        self.speech_callback: Optional[Callable[[int, int, str], None]] = (
            None  # guild_id, user_id, text
        )
        self.audio_queues: dict[
            int, deque
        ] = {}  # guild_id -> deque of audio_source paths
        self.guild_states: dict[
            int, dict
        ] = {}  # guild_id -> {is_user_speaking: bool, current_audio: str, interrupted_audio: str}

        # Add Cogs
        self.add_cog(VoiceCog(self))

    async def on_ready(self):
        self.logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        # Sync commands explicitly if needed, but py-cord does it automatically usually
        # await self.sync_commands()
        self.logger.info("Commands synced.")

    def set_speech_callback(self, callback: Callable):
        self.speech_callback = callback

    async def _on_sink_callback(self, user_id: int, pcm_data: bytes, guild_id: int):
        """Callback from Sink when speech is detected."""
        self.logger.debug(f"Processing speech from user {user_id} in guild {guild_id}")

        # Process audio to text
        text = await self.voice_handler.process_audio(user_id, pcm_data)

        if text and self.speech_callback:
            # Resolve user name
            user_name = f"User{user_id}"
            try:
                guild = self.get_guild(guild_id)
                if guild:
                    member = guild.get_member(user_id)
                    if not member:
                        self.logger.info(f"Member {user_id} not in cache, fetching...")
                        member = await guild.fetch_member(user_id)

                    if member:
                        user_name = member.display_name
                        self.logger.info(f"Resolved user name: {user_name}")
                    else:
                        self.logger.warning(
                            f"Member {user_id} found neither in cache nor API"
                        )
                else:
                    self.logger.warning(f"Guild {guild_id} not found in cache")
            except Exception as e:
                self.logger.warning(f"Failed to resolve user name for {user_id}: {e}")

            # Notify adapter with resolved user name
            await self.speech_callback(guild_id, user_id, text, user_name)

        # Logic for resuming AFTER speech ends
        if guild_id in self.guild_states:
            state = self.guild_states[guild_id]
            state["is_user_speaking"] = False

            # Resume interrupted audio if exists
            if state.get("interrupted_audio"):
                self.logger.info(
                    f"Resuming interrupted audio: {state['interrupted_audio']}"
                )
                if guild_id not in self.audio_queues:
                    self.audio_queues[guild_id] = deque()
                self.audio_queues[guild_id].appendleft(state["interrupted_audio"])
                state["interrupted_audio"] = None

            # Continue playback
            self._play_next(guild_id)

    async def _on_speech_start_callback(self, user_id: int, guild_id: int):
        """Callback from Sink when speech START is detected."""
        # self.logger.debug(f"Speech start detected from user {user_id} in guild {guild_id}")

        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = {
                "is_user_speaking": False,
                "current_audio": None,
                "interrupted_audio": None,
            }

        state = self.guild_states[guild_id]
        state["is_user_speaking"] = True

        # Stop current playback if any
        guild = self.get_guild(guild_id)
        if guild and guild.voice_client and guild.voice_client.is_playing():
            if state.get("current_audio"):
                self.logger.info(
                    f"Stopping audio due to user {user_id} interruption. Saving checkpoint."
                )
                state["interrupted_audio"] = state["current_audio"]
            guild.voice_client.stop()

    def start_listening(self, vc: discord.VoiceClient, guild_id: int):
        if not vc:
            return

        if vc.is_recording():
            return

        self.logger.info(f"Starting to listen in guild {guild_id}")

        # Create a callback wrapper to include guild_id
        async def sink_callback(user_id, data):
            await self._on_sink_callback(user_id, data, guild_id)

        async def speech_start_callback(user_id):
            await self._on_speech_start_callback(user_id, guild_id)

        sink = SilenceDetectingSink(
            callback=sink_callback,
            on_speech_start_callback=speech_start_callback,
            config=self.adapter_config.voice,
        )

        vc.start_recording(
            sink,
            self._on_recording_stopped,
            # Note: start_recording takes a callback for when RECORDING STOPS,
            # but our Sink handles continuous chunks.
        )

    async def _on_recording_stopped(self, sink: SilenceDetectingSink, *args):
        self.logger.info("Recording stopped.")
        # We might want to restart? Or just let it end.
        sink.cleanup()

    def _play_next(self, guild_id: int, error=None):
        """Play the next audio in the queue for the given guild."""
        if error:
            self.logger.error(f"Error in playback for guild {guild_id}: {error}")

        if guild_id not in self.audio_queues or not self.audio_queues[guild_id]:
            return

        guild = self.get_guild(guild_id)
        if not guild:
            return

        vc: discord.VoiceClient = guild.voice_client
        if not vc or not vc.is_connected():
            self.audio_queues[guild_id].clear()
            return

        if vc.is_playing():
            return

        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = {
                "is_user_speaking": False,
                "current_audio": None,
                "interrupted_audio": None,
            }
        state = self.guild_states[guild_id]

        if state["is_user_speaking"]:
            # self.logger.info("User is speaking, pausing playback.")
            return

        audio_source = self.audio_queues[guild_id].popleft()
        state["current_audio"] = audio_source

        try:
            source = discord.FFmpegPCMAudio(audio_source)

            def after_callback(e):
                self.loop.call_soon_threadsafe(self._play_next, guild_id, e)

            vc.play(source, after=after_callback)
            self.logger.info(f"Playing audio: {audio_source}")
        except Exception as e:
            self.logger.error(f"Failed to play audio: {e}")
            if guild_id in self.guild_states:
                self.guild_states[guild_id]["current_audio"] = None
            self.loop.call_soon_threadsafe(
                self._play_next, guild_id, e
            )  # Try next one safely

    async def speak(self, guild_id: int, audio_source: str):
        """Play audio in the voice channel of the given guild."""
        guild = self.get_guild(guild_id)
        if not guild:
            self.logger.warning(f"Could not find guild {guild_id} to speak in")
            return

        vc: discord.VoiceClient = guild.voice_client
        if not vc or not vc.is_connected():
            self.logger.warning(f"Not connected to voice in guild {guild_id}")
            return

        if guild_id not in self.audio_queues:
            self.audio_queues[guild_id] = deque()

        # Queue Limit Logic: Drop oldest if > 5
        if len(self.audio_queues[guild_id]) >= 5:
            dropped = self.audio_queues[guild_id].popleft()
            self.logger.info(f"Queue limit reached, dropped oldest audio: {dropped}")

        self.audio_queues[guild_id].append(audio_source)

        if not vc.is_playing():
            self._play_next(guild_id)
        else:
            self.logger.info(f"Audio queued: {audio_source}")
