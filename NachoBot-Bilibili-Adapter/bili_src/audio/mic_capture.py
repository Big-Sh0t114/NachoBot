"""
Local Microphone Capture and ASR Worker for BilibiliAdapter

Captures audio from system default microphone, detects speech using energy-based VAD,
and sends to SiliconFlow ASR API for transcription.
"""

import asyncio
import io
import logging
import wave
import struct
import queue
import time
from dataclasses import dataclass
from typing import Callable, Optional, Awaitable

try:
    import sounddevice as sd

    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False
    sd = None


@dataclass
class MicConfig:
    """Microphone capture configuration"""

    enable: bool = False
    room_id: int = 0
    subtitle_path: str = "subtitles1.txt"
    silence_threshold: float = 0.01  # RMS threshold for speech detection
    silence_duration: float = 0.5  # Seconds of silence to end speech segment
    sample_rate: int = 16000
    sample_rate: int = 16000
    channels: int = 1
    on_speech_start: Optional[Callable[[], Awaitable[None]]] = None


class MicCaptureWorker:
    """
    Captures audio from local microphone and triggers ASR when speech is detected.
    Uses energy-based Voice Activity Detection (VAD).
    """

    def __init__(
        self,
        config: MicConfig,
        on_speech_recognized: Callable[[str], Awaitable[None]],
        logger: logging.Logger,
    ):
        self.config = config
        self.on_speech_recognized = on_speech_recognized
        self.logger = logger

        self._running = False
        self._audio_buffer: list = []
        self._is_speaking = False
        self._silence_samples = 0
        self._samples_per_chunk = int(config.sample_rate * 0.1)  # 100ms chunks
        self._samples_per_chunk = int(config.sample_rate * 0.1)  # 100ms chunks
        self._silence_sample_threshold = int(
            config.silence_duration * config.sample_rate / self._samples_per_chunk
        )
        self._last_activity = 0.0

        # Queue to pass complete audio segments from callback thread to main thread
        self._processing_queue = queue.Queue()
        self._paused = False

        # ASR callback (to be set by adapter)
        self._asr_callback: Optional[Callable[[bytes], Awaitable[Optional[str]]]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_asr_callback(
        self, callback: Callable[[bytes], Awaitable[Optional[str]]]
    ) -> None:
        """Set the ASR callback function"""
        self._asr_callback = callback

    def pause(self) -> None:
        """Pause microphone capture"""
        if not self._paused:
            self._paused = True
            self.logger.info("Microphone capture paused")

    def resume(self) -> None:
        """Resume microphone capture"""
        if self._paused:
            self._paused = False
            self.logger.info("Microphone capture resumed")

    def is_paused(self) -> bool:
        return self._paused

    def _calculate_rms(self, audio_data: bytes) -> float:
        """Calculate RMS (root mean square) of audio data"""
        if len(audio_data) < 2:
            return 0.0
        # Convert bytes to 16-bit samples
        samples = struct.unpack(f"<{len(audio_data) // 2}h", audio_data)
        if not samples:
            return 0.0
        # Calculate RMS
        sum_squares = sum(s * s for s in samples)
        rms = (sum_squares / len(samples)) ** 0.5
        # Normalize to 0-1 range (16-bit audio max is 32767)
        return rms / 32767.0

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback for sounddevice audio stream (runs in a separate thread)"""
        self._last_activity = time.time()
        if status:
            self.logger.warning(f"Audio stream status: {status}")

        # If paused, skip processing
        if self._paused:
            return

        # Convert to bytes
        audio_bytes = indata.tobytes()

        # Calculate RMS for VAD
        rms = self._calculate_rms(audio_bytes)

        if rms > self.config.silence_threshold:
            # Speech detected
            if not self._is_speaking:
                self._is_speaking = True
                self._is_speaking = True
                self.logger.debug("Speech started")
                if self.config.on_speech_start:
                    # Execute callback in a thread-safe manner if needed,
                    # but here we are in a different thread.
                    # Use asyncio.run_coroutine_threadsafe if loop is available,
                    # or just fire and forget if it's not critical to wait.
                    # Since adapter logic is async, we need to bridge this.
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self.config.on_speech_start(), self._loop
                        )

            self._audio_buffer.append(audio_bytes)
            self._silence_samples = 0
        else:
            # Silence
            if self._is_speaking:
                self._audio_buffer.append(audio_bytes)
                self._silence_samples += 1

                if self._silence_samples >= self._silence_sample_threshold:
                    # End of speech segment
                    self._is_speaking = False
                    self.logger.debug(
                        f"Speech ended, buffer size: {len(self._audio_buffer)} chunks"
                    )

                    # Push to queue instead of processing directly
                    if self._audio_buffer:
                        audio_data = b"".join(self._audio_buffer)
                        self._audio_buffer = []
                        self._silence_samples = 0
                        self._processing_queue.put(audio_data)

    async def _process_queue_loop(self) -> None:
        """Coroutine to process audio segments from the queue"""
        while self._running:
            try:
                # Check queue non-blocking
                try:
                    audio_data = self._processing_queue.get_nowait()
                    await self._process_audio(audio_data)
                except queue.Empty:
                    await asyncio.sleep(0.1)
            except Exception as e:
                self.logger.error(f"Error in processing loop: {e}")
                await asyncio.sleep(0.1)

    async def _process_audio(self, audio_data: bytes) -> None:
        """Process audio data through ASR"""
        if not self._asr_callback:
            self.logger.warning("ASR callback not set")
            return

        try:
            # Convert raw PCM to WAV format for API
            wav_data = self._pcm_to_wav(audio_data)

            # Call ASR
            text = await self._asr_callback(wav_data)

            if text and text.strip():
                self.logger.info(f"ASR recognized: {text}")

                # Update subtitle file
                self._update_subtitle(text)

                # Trigger callback
                await self.on_speech_recognized(text)
        except Exception as e:
            self.logger.error(f"ASR processing failed: {e}")

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """Convert raw PCM data to WAV format"""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(self.config.channels)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(self.config.sample_rate)
            wav_file.writeframes(pcm_data)
        return buffer.getvalue()

    def _update_subtitle(self, text: str) -> None:
        """Write recognized text to subtitle file"""
        try:
            with open(self.config.subtitle_path, "w", encoding="utf-8-sig") as f:
                f.write(text)
            self.logger.debug(f"Subtitle updated: {self.config.subtitle_path}")
        except Exception as e:
            self.logger.error(f"Failed to update subtitle: {e}")

    async def start(self) -> None:
        """Start microphone capture"""
        if not SOUNDDEVICE_AVAILABLE:
            self.logger.error("sounddevice not installed. Run: pip install sounddevice")
            return

        if not self.config.enable:
            self.logger.info("Microphone capture disabled")
            return

        self._running = True
        self.logger.info(
            f"Starting microphone capture (configured: sample_rate={self.config.sample_rate}, threshold={self.config.silence_threshold})"
        )
        self._loop = asyncio.get_running_loop()

        try:
            # Query default device to use native sample rate (prevents Exclusive Mode/OBS conflict)
            try:
                device_info = sd.query_devices(kind="input")
                native_rate = int(device_info.get("default_samplerate", 16000))
                self.logger.info(f"Device native sample rate: {native_rate}")

                # Update config to match native rate
                self.config.sample_rate = native_rate

                # Recalculate VAD parameters dependent on sample rate
                self._samples_per_chunk = int(self.config.sample_rate * 0.1)
                self._silence_sample_threshold = int(
                    self.config.silence_duration
                    * self.config.sample_rate
                    / self._samples_per_chunk
                )
            except Exception as e:
                self.logger.warning(
                    f"Failed to query device native rate, using default 16000: {e}"
                )

            # Start processing loop
            proc_task = asyncio.create_task(self._process_queue_loop())

            # Start audio stream using updated config.sample_rate
            with sd.InputStream(
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                blocksize=self._samples_per_chunk,
                callback=self._audio_callback,
            ) as stream:
                # Wait for running flag to be cleared
                self._last_activity = time.time()
                while self._running:
                    if not stream.active:
                        self.logger.error("Audio stream is no longer active!")
                        break

                    # Watchdog: If callback hasn't run for > 3 seconds, assume dead
                    if time.time() - self._last_activity > 3.0:
                        self.logger.error(
                            "Audio stream Watchdog timeout (no callback for 3s)!"
                        )
                        break

                    await asyncio.sleep(0.5)

            # Ensure flag is cleared so processing loop can exit
            self._running = False

            # Wait for processing loop to finish (it will exit when _running is False)
            await proc_task

        except Exception as e:
            self.logger.error(f"Microphone capture error: {e}")
            self._running = False

    def stop(self) -> None:
        """Stop microphone capture"""
        self._running = False
        self.logger.info("Microphone capture stopped")
