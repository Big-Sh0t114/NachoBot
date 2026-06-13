"""
Audio Output Module - Plays TTS audio to a virtual audio cable device.

Uses sounddevice to output WAV audio files to a specified virtual audio
cable (e.g., VB-Audio Virtual Cable), allowing the bot's voice to be
piped into any application's microphone input.
"""

import asyncio
import logging
import wave
import os
from collections import deque
from typing import Optional

import numpy as np

from config import AudioOutputConfig


class AudioOutput:
    """
    Manages audio playback to a virtual audio cable device.
    Supports queuing, sequential playback, and interruption.
    """

    def __init__(self, config: AudioOutputConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self._device_id: Optional[int] = None
        self._queue: deque[str] = deque()
        self._is_playing = False
        self._is_paused = False
        self._play_lock = asyncio.Lock()
        self._current_stop_event: Optional[asyncio.Event] = None
        self._interrupted_wav: Optional[str] = None
        self._sd = None  # sounddevice module (lazy import)

    def _cleanup_file(self, wav_path: str):
        """Clean up the temporary WAV file."""
        try:
            if wav_path and os.path.exists(wav_path):
                os.remove(wav_path)
                self.logger.debug(f"Cleaned up temp audio file: {wav_path}")
        except Exception as e:
            self.logger.warning(f"Failed to clean up temp audio file {wav_path}: {e}")

    def _ensure_sounddevice(self):
        """Lazy import sounddevice to avoid import errors if not installed."""
        if self._sd is None:
            try:
                import sounddevice as sd
                self._sd = sd
            except ImportError:
                self.logger.error(
                    "sounddevice is required for audio output! "
                    "Install with: pip install sounddevice"
                )
                raise
        return self._sd

    def initialize(self):
        """Find and configure the virtual audio cable device."""
        sd = self._ensure_sounddevice()

        devices = sd.query_devices()
        target_name = self.config.device_name.lower()

        self.logger.info("Available audio output devices:")
        for i, dev in enumerate(devices):
            if dev["max_output_channels"] > 0:
                self.logger.info(f"  [{i}] {dev['name']} (out={dev['max_output_channels']}ch)")
                if target_name in dev["name"].lower():
                    self._device_id = i
                    self.logger.info(f"  >>> Matched target device: [{i}] {dev['name']}")

        if self._device_id is None:
            self.logger.warning(
                f"Virtual audio cable '{self.config.device_name}' not found! "
                f"Falling back to default output device. "
                f"Please check device_name in config.toml."
            )
        else:
            self.logger.info(f"Audio output device: [{self._device_id}] {devices[self._device_id]['name']}")

    async def play(self, wav_path: str):
        """Queue a WAV file for playback."""
        # Queue Limit: drop oldest if >= 5
        if len(self._queue) >= 5:
            dropped = self._queue.popleft()
            self.logger.info(f"Queue limit reached, dropped oldest audio: {dropped}")

        self._queue.append(wav_path)

        if not self._is_playing and not self._is_paused:
            asyncio.create_task(self._play_next())

    async def stop_current(self):
        """Stop the currently playing audio (for interruption)."""
        if self._current_stop_event:
            self._current_stop_event.set()

    async def stop_and_pause(self):
        """Stop current playback and pause the queue."""
        self._is_paused = True
        self.logger.info("AudioOutput paused")
        if self._current_stop_event:
            self._current_stop_event.set()

    async def stop(self):
        """Completely stop playback and clean up all remaining files."""
        self.logger.info("Stopping AudioOutput and cleaning up")
        self._is_paused = False
        if self._current_stop_event:
            self._current_stop_event.set()
        
        while self._queue:
            dropped = self._queue.popleft()
            self._cleanup_file(dropped)
            
        if self._interrupted_wav:
            self._cleanup_file(self._interrupted_wav)
            self._interrupted_wav = None

    def resume(self):
        """Resume playback."""
        self._is_paused = False
        self.logger.info("AudioOutput resumed")
        if self._interrupted_wav:
            self._queue.appendleft(self._interrupted_wav)
            self._interrupted_wav = None
        if not self._is_playing and self._queue:
            asyncio.create_task(self._play_next())

    async def _play_next(self):
        """Play the next audio file in the queue."""
        async with self._play_lock:
            while self._queue:
                if self._is_paused:
                    break

                wav_path = self._queue.popleft()
                self._is_playing = True
                self._current_stop_event = asyncio.Event()

                try:
                    await self._play_wav(wav_path)
                except Exception as e:
                    self.logger.error(f"Error playing audio '{wav_path}': {e}")
                finally:
                    if self._current_stop_event and self._current_stop_event.is_set():
                        # Interrupted
                        if self._is_paused:
                            self._interrupted_wav = wav_path
                    self._current_stop_event = None

                    if self._interrupted_wav != wav_path:
                        self._cleanup_file(wav_path)

            self._is_playing = False

    async def _play_wav(self, wav_path: str):
        """Play a single WAV file to the virtual audio cable."""
        sd = self._ensure_sounddevice()

        try:
            with wave.open(wav_path, "rb") as wf:
                n_channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                framerate = wf.getframerate()
                n_frames = wf.getnframes()
                raw_data = wf.readframes(n_frames)

            # Convert to numpy array
            if sample_width == 2:
                dtype = np.int16
            elif sample_width == 4:
                dtype = np.int32
            else:
                self.logger.error(f"Unsupported sample width: {sample_width}")
                return

            audio = np.frombuffer(raw_data, dtype=dtype)

            if n_channels > 1:
                audio = audio.reshape(-1, n_channels)

            # Convert to float32 for sounddevice (normalized to [-1.0, 1.0])
            if dtype == np.int16:
                audio_float = audio.astype(np.float32) / 32767.0
            else:
                audio_float = audio.astype(np.float32) / 2147483647.0

            # Ensure stereo for most virtual audio cables
            if audio_float.ndim == 1:
                audio_float = np.column_stack([audio_float, audio_float])

            # Determine the device's native sample rate
            device_sr = framerate
            if self._device_id is not None:
                try:
                    dev_info = sd.query_devices(self._device_id)
                    device_sr = int(dev_info["default_samplerate"])
                except Exception:
                    device_sr = 48000  # safe default for most virtual cables

            # Resample if WAV rate differs from device rate
            if framerate != device_sr:
                from scipy.signal import resample
                original_len = audio_float.shape[0]
                target_len = int(original_len * device_sr / framerate)
                self.logger.debug(
                    f"Resampling {framerate}Hz → {device_sr}Hz "
                    f"({original_len} → {target_len} samples)"
                )
                audio_float = resample(audio_float, target_len, axis=0).astype(np.float32)
                playback_rate = device_sr
            else:
                playback_rate = framerate

            duration = audio_float.shape[0] / playback_rate
            self.logger.info(f"Playing {wav_path} ({duration:.2f}s) to device {self._device_id or 'default'}")

            # Play using sounddevice (blocking in executor to not block event loop)
            loop = asyncio.get_running_loop()
            stop_event = self._current_stop_event

            def _blocking_play():
                try:
                    sd.play(
                        audio_float,
                        samplerate=playback_rate,
                        device=self._device_id,
                        blocking=False,
                    )

                    # Wait for playback to finish or stop event
                    import time
                    while sd.get_stream().active:
                        if stop_event and stop_event.is_set():
                            sd.stop()
                            return
                        time.sleep(0.05)
                except Exception as e:
                    raise e

            await loop.run_in_executor(None, _blocking_play)

        except FileNotFoundError:
            self.logger.error(f"WAV file not found: {wav_path}")
        except Exception as e:
            self.logger.error(f"Error playing WAV: {e}", exc_info=True)

