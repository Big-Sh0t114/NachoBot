import asyncio
import io
import logging
import os
import queue
import tempfile
import wave
import winsound
from typing import Deque, Optional


class AudioPlayer:
    """
    Manages audio playback with support for queuing, interruption, and resuming.
    Uses winsound for playback and calculates duration for timing.
    """

    def __init__(self, logger: logging.Logger, on_start=None, on_stop=None):
        self.logger = logger
        self.on_start = on_start
        self.on_stop = on_stop
        self.queue: Deque[bytes] = queue.deque()
        self.current_audio: Optional[bytes] = None
        self.interrupted_audio: Optional[bytes] = None
        self.is_playing = False
        self.is_paused = False
        self.stop_event = asyncio.Event()  # Set when stopped/interrupted
        self.play_task: Optional[asyncio.Task] = None
        self._loop = None

    def start(self):
        """Start the playback loop."""
        if self.play_task and not self.play_task.done():
            return
        self._loop = asyncio.get_running_loop()
        self.stop_event.clear()
        self.play_task = self._loop.create_task(self._playback_loop())
        self.logger.info("AudioPlayer started")

    async def _playback_loop(self):
        while True:
            try:
                if self.is_paused:
                    await asyncio.sleep(0.1)
                    continue

                if not self.queue:
                    await asyncio.sleep(0.1)
                    continue

                # Get next audio
                audio_data = self.queue.popleft()
                self.current_audio = audio_data
                self.is_playing = True

                # Calculate duration
                duration = self._get_wav_duration(audio_data)
                # self.logger.debug(f"Playing audio segment ({duration:.2f}s)")

                # Play (Async)
                if self.on_start:
                    if asyncio.iscoroutinefunction(self.on_start):
                        asyncio.create_task(self.on_start())
                    else:
                        self.on_start()
                self._play_sound(audio_data)

                # Wait for duration (or interruption)
                # We wait for duration, checking stop_event periodically or using wait_for
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=duration)
                    # If we got here, stop_event was set (Interrupted!)
                    self.logger.info("Audio playback interrupted!")
                    self._stop_sound()
                    self.interrupted_audio = self.current_audio  # Save current
                    self.current_audio = None
                except asyncio.TimeoutError:
                    # Finished playing naturally
                    pass

                self.current_audio = None
                self.is_playing = False
                if self.on_stop:
                    if asyncio.iscoroutinefunction(self.on_stop):
                        asyncio.create_task(self.on_stop())
                    else:
                        self.on_stop()
                self.stop_event.clear()  # Reset for next

            except Exception as e:
                self.logger.error(f"AudioPlayer loop error: {e}")
                await asyncio.sleep(1)

    def _play_sound(self, audio_data: bytes):
        try:
            # Save to temp
            temp_path = os.path.join(tempfile.gettempdir(), "nachobot_tts_player.wav")
            with open(temp_path, "wb") as f:
                f.write(audio_data)
            winsound.PlaySound(temp_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            self.logger.error(f"Winsound play error: {e}")

    def _stop_sound(self):
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def _get_wav_duration(self, audio_data: bytes) -> float:
        try:
            with io.BytesIO(audio_data) as f:
                with wave.open(f, "rb") as wav_file:
                    frames = wav_file.getnframes()
                    rate = wav_file.getframerate()
                    return frames / float(rate)
        except Exception:
            return 2.0  # Fallback

    def play(self, audio_data: bytes):
        """Add audio to queue."""
        self.queue.append(audio_data)

    def stop_and_pause(self):
        """Stop current playback immediately and pause."""
        self.is_paused = True
        self.stop_event.set()  # Signal loop to stop waiting
        self.logger.info("AudioPlayer stopped and paused.")

    def resume(self):
        """Resume playback, re-queueing interrupted audio."""
        if self.interrupted_audio:
            self.logger.info("Resuming interrupted audio...")
            self.queue.appendleft(self.interrupted_audio)
            self.interrupted_audio = None
        self.is_paused = False
        self.stop_event.clear()  # Ensure clear
        self.logger.info("AudioPlayer resumed.")
