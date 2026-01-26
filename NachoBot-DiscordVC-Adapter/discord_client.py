import logging
from typing import Optional, Callable
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
        # Defer response to avoid "Unknown interaction" if connection takes > 3s
        await ctx.defer()

        if not ctx.author.voice:
            await ctx.respond("你要我进哪里啊，自己先进一个通话吧(´-ω-`)")
            return None

        channel = ctx.author.voice.channel

        if ctx.voice_client:
            if ctx.voice_client.channel.id != channel.id:
                # Check if current channel has other members (len > 1 means current channel has bot + users)
                # If only bot is in current channel (len == 1), we allow move.
                if len(ctx.voice_client.channel.members) > 1:
                    await ctx.respond(f"我已经在 {ctx.voice_client.channel.name} 里咯")
                    return None

                await ctx.voice_client.move_to(channel)
            else:
                await ctx.respond("笨蛋..人家已经在这里啦(´-ω-`)")
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
        await ctx.defer()
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
        # discord.py supports 'proxy' kwarg in Client/Bot init
        proxy_url = "http://127.0.0.1:7897"
        # Disable auto_sync_commands to avoid conflict with Koishi (who manages registration)
        super().__init__(intents=intents, proxy=proxy_url, auto_sync_commands=False)
        self.adapter_config = config
        self.voice_handler = voice_handler
        self.logger = logger
        self.speech_callback: Optional[Callable[[int, int, str], None]] = (
            None  # guild_id, user_id, text
        )

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

    def start_listening(self, vc: discord.VoiceClient, guild_id: int):
        if not vc:
            return

        if vc.recording:
            return

        self.logger.info(f"Starting to listen in guild {guild_id}")

        # Create a callback wrapper to include guild_id
        async def sink_callback(user_id, data):
            await self._on_sink_callback(user_id, data, guild_id)

        sink = SilenceDetectingSink(
            callback=sink_callback, config=self.adapter_config.voice
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

        if vc.is_playing():
            # Option: Stop current or Queue? For now, we interrupt.
            vc.stop()

        try:
            # FFmpegPCMAudio requires ffmpeg installed and in PATH
            source = discord.FFmpegPCMAudio(audio_source)
            vc.play(source)
            self.logger.info(f"Playing audio: {audio_source}")
        except Exception as e:
            self.logger.error(f"Failed to play audio: {e}")
