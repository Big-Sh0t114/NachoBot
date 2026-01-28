import asyncio
import logging
import io
import time
import wave
import audioop
import numpy as np
from typing import Optional, Callable

from discord.sinks import Sink, Filters
import aiohttp

from config import AdapterConfig, VoiceConfig

# Discord sends stereo 48kHz PCM
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
DISCORD_WIDTH = 2  # 16-bit


class SilenceDetectingSink(Sink):
    """
    A sink that detects silence and triggers a callback with the captured audio segment.
    """

    def __init__(
        self,
        filters=None,
        callback: Callable = None,
        on_speech_start_callback: Callable = None,
        config: VoiceConfig = None,
    ):
        if filters is None:
            # filters = Filters.default() # Not valid
            # filters = Filters() # Also not valid, Sink expects a dict
            pass

        super().__init__(filters=filters)

        self.callback = callback
        self.on_speech_start_callback = on_speech_start_callback
        self.config = config
        self.vc = None
        self.audio_data = {}  # User ID -> Bytearray

        # VAD State per user
        self.last_speech_time = {}  # User ID -> timestamp
        self.is_speaking = {}  # User ID -> bool
        self.silence_start = {}  # User ID -> timestamp

        # Capture loop for thread-safe callbacks from DecodeManager thread
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()

        # Buffer for current utterance
        self.utterance_buffer = {}  # User ID -> Bytearray

        self.vad_threshold = config.vad_threshold if config else 500
        self.silence_threshold = config.silence_threshold if config else 0.5
        self.min_speech_duration = 0.3  # Minimum duration to consider speech

        # Background task for checking silence
        self.checker_task = None
        self._stopped = False
        logging.getLogger("VoiceHandler").info(
            f"SilenceDetectingSink initialized with loop: {self.loop}"
        )

    def init(self, vc):
        logging.getLogger("VoiceHandler").info(
            f"SilenceDetectingSink initialized with loop: {self.loop}"
        )
        self.vc = vc
        if not self.checker_task:
            self.checker_task = asyncio.create_task(self._silence_checker())

    def cleanup(self):
        self._stopped = True
        if self.checker_task:
            self.checker_task.cancel()

    @Filters.container
    def write(self, data, user):
        # logging.getLogger("VoiceHandler").debug(f"Packet from {user} len={len(data)}")
        if user not in self.utterance_buffer:
            self.utterance_buffer[user] = bytearray()
            self.is_speaking[user] = False

        # Convert to simple RMS for VAD
        # We process 20ms chunks usually, data length varies
        try:
            rms = audioop.rms(data, DISCORD_WIDTH)
        except Exception:
            rms = 0

        is_speech = rms > self.vad_threshold
        now = time.time()

        # State Machine
        if is_speech:
            self.last_speech_time[user] = now
            if not self.is_speaking[user]:
                # Started speaking
                self.is_speaking[user] = True
                logging.getLogger("VoiceHandler").debug(
                    f"User {user} started speaking (RMS: {rms})"
                )
                if self.on_speech_start_callback:
                    if self.loop and self.loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self.on_speech_start_callback(user), self.loop
                        )

        # Debug Log periodically
        if int(now * 10) % 20 == 0:  # Log every ~2 seconds
            logging.getLogger("VoiceHandler").debug(
                f"Current RMS: {rms} | Threshold: {self.vad_threshold} | Speaking: {self.is_speaking.get(user)}"
            )

        self.utterance_buffer[user].extend(data)

    async def _silence_checker(self):
        logging.getLogger("VoiceHandler").info("Silence checker started")
        while not self._stopped:
            try:
                await asyncio.sleep(0.1)
                now = time.time()

                # heartbeat log for checker every 10s
                if int(now) % 10 == 0 and int(now * 10) % 10 == 0:
                    logging.getLogger("VoiceHandler").debug(
                        f"Silence checker heartbeat. Users: {list(self.utterance_buffer.keys())}"
                    )

                users = list(self.utterance_buffer.keys())
                for user in users:
                    if not self.is_speaking.get(user, False):
                        continue

                    last_speech = self.last_speech_time.get(user, 0)
                    silence_duration = now - last_speech

                    if silence_duration > self.silence_threshold:
                        # Silence detected after speech
                        logging.getLogger("VoiceHandler").debug(
                            f"User {user} silence detected ({silence_duration:.2f}s > {self.silence_threshold}s). Processing..."
                        )

                        # Retrieve and reset buffer *before* potential callback to avoid race conditions
                        data_to_process = self.utterance_buffer.pop(user, bytearray())
                        self.is_speaking[user] = False

                        length_seconds = len(data_to_process) / (
                            DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * DISCORD_WIDTH
                        )

                        if length_seconds >= self.min_speech_duration:
                            # Flush and callback
                            logging.getLogger("VoiceHandler").info(
                                f"Speech detected from {user}: {length_seconds:.2f}s"
                            )
                            if self.callback:
                                # Run callback in background to not block loop
                                asyncio.create_task(
                                    self.callback(user, data_to_process)
                                )
                        else:
                            logging.getLogger("VoiceHandler").debug(
                                f"Snippet too short ({length_seconds:.2f}s < {self.min_speech_duration}s), ignoring."
                            )

                        # Reset (Ensure robust reset)
                        self.utterance_buffer[user] = bytearray()
                        self.is_speaking[user] = False
            except Exception as e:
                logging.getLogger("VoiceHandler").error(
                    f"Silence checker error: {e}", exc_info=True
                )
                await asyncio.sleep(1)


class VoiceHandler:
    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.stt_config = config.stt
        self.proxy_url = (
            config.discord.proxy_url if config.discord.proxy_enabled else None
        )

    async def process_audio(self, user_id: int, pcm_data: bytes) -> Optional[str]:
        """
        Process PCM data: Convert to WAV -> Send to ASR -> Return Text
        """
        if not self.stt_config.enabled:
            return None

        # 1. Convert PCM to WAV
        # Discord: Stereo, 48kHz, 16bit
        # Most APIs process Mono 16kHz better, or just standard formats
        # We'll save as Mono 16kHz for efficiency if we can, or just dump standard WAV

        # Ensure correct length
        if len(pcm_data) % (DISCORD_CHANNELS * DISCORD_WIDTH) != 0:
            # Padding? or Trimming
            trim_len = len(pcm_data) - (
                len(pcm_data) % (DISCORD_CHANNELS * DISCORD_WIDTH)
            )
            pcm_data = pcm_data[:trim_len]

        try:
            # We can use numpy to mix stereo to mono
            audio_np = np.frombuffer(pcm_data, dtype=np.int16)
            if DISCORD_CHANNELS == 2:
                audio_np = audio_np.reshape(-1, 2)
                # Average channels for mono
                audio_mono = audio_np.mean(axis=1).astype(np.int16)
            else:
                audio_mono = audio_np

            # Create in-memory WAV
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, "wb") as wav_file:
                wav_file.setnchannels(1)  # Mono
                wav_file.setsampwidth(DISCORD_WIDTH)
                wav_file.setframerate(DISCORD_SAMPLE_RATE)
                wav_file.writeframes(audio_mono.tobytes())

            wav_buffer.seek(0)
            wav_data = wav_buffer.read()

            # 2. Call ASR API
            return await self._call_asr_api(wav_data)

        except Exception as e:
            self.logger.error(f"Error processing audio: {e}")
            return None

    async def _call_asr_api(self, wav_data: bytes) -> Optional[str]:
        if not self.stt_config.api_key:
            self.logger.warning("No ASR API Key configured")
            return None

        url = f"{self.stt_config.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.stt_config.api_key}"}

        data = aiohttp.FormData()
        # Name is important for OpenAI API to detect format
        data.add_field("file", wav_data, filename="audio.wav", content_type="audio/wav")
        data.add_field("model", self.stt_config.model)
        # Optional: prompt, language

        try:
            # User requested Voice Processing NOT to use proxy

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        self.logger.error(f"ASR API error {resp.status}: {text}")
                        return None

                    result = await resp.json()
                    text = result.get("text", "").strip()
                    if text:
                        # Sanitize text to remove control characters (except newlines/tabs)
                        # This prevents 400 Bad Request from LLM if ASR returns garbage
                        text = "".join(
                            ch for ch in text if ch.isprintable() or ch in "\n\r\t"
                        )
                        self.logger.info(f"ASR Recognized: {text}")
                        return text
                    return None
        except Exception as e:
            self.logger.error(f"ASR request failed: {e}")
            return None
