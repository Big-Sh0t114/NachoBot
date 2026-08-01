"""
Local microphone capture and streaming ASR worker for BilibiliAdapter.

Captures audio from the system default microphone, detects speech using
energy-based VAD, and incrementally feeds Multimodal-Adapter's shared ASR.

Supports Push-to-Talk (PTT) mode: when enabled, audio is only captured while
the configured key is held down.
"""

import asyncio
import logging
import queue
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import numpy as np
from multimodal_bridge import ensure_multimodal_import
from scipy.signal import resample_poly


ensure_multimodal_import()

from nachobot_multimodal.asr.streaming import StreamingASR  # noqa: E402

try:
    import sounddevice as sd

    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False
    sd = None


class PTTKeyMonitor:
    """Global keyboard monitor for Push-to-Talk functionality.

    Uses pynput to listen for key press/release events globally.
    Thread-safe: the key state flag is set from the listener thread
    and read from the audio callback thread.
    """

    # Mapping of common key names to pynput Key attributes
    _SPECIAL_KEYS = {
        "ctrl": "ctrl_l", "ctrl_l": "ctrl_l", "ctrl_r": "ctrl_r",
        "alt": "alt_l", "alt_l": "alt_l", "alt_r": "alt_r",
        "shift": "shift_l", "shift_l": "shift_l", "shift_r": "shift_r",
        "caps_lock": "caps_lock", "tab": "tab", "space": "space",
        "enter": "enter", "backspace": "backspace", "delete": "delete",
        "esc": "esc", "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
        "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8", "f9": "f9",
        "f10": "f10", "f11": "f11", "f12": "f12",
    }

    def __init__(self, key_name: str, logger):
        self.logger = logger
        self._key_held = False
        self._listener = None
        self._target_key = None
        self._target_char = None

        key_lower = key_name.strip().lower()

        try:
            from pynput import keyboard
            if key_lower in self._SPECIAL_KEYS:
                self._target_key = getattr(keyboard.Key, self._SPECIAL_KEYS[key_lower], None)
                if self._target_key is None:
                    self.logger.error(f"PTT: Unknown special key '{key_name}', falling back to 'v'")
                    self._target_char = 'v'
            else:
                self._target_char = key_lower[0] if key_lower else 'v'
        except ImportError:
            self.logger.error("pynput is required for push-to-talk! pip install pynput")
            raise

    @property
    def is_held(self) -> bool:
        return self._key_held

    def start(self):
        """Start the global keyboard listener."""
        try:
            from pynput import keyboard

            def on_press(key):
                if self._match_key(key):
                    self._key_held = True

            def on_release(key):
                if self._match_key(key):
                    self._key_held = False

            self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self._listener.daemon = True
            self._listener.start()

            key_display = self._target_char or str(self._target_key).replace("Key.", "")
            self.logger.info(f"PTT keyboard listener started — hold [{key_display}] to talk")
        except Exception as e:
            self.logger.error(f"Failed to start PTT keyboard listener: {e}")
            raise

    def stop(self):
        """Stop the global keyboard listener."""
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self._key_held = False

    def _match_key(self, key) -> bool:
        """Check if the pressed/released key matches our target.

        On Windows with a Chinese IME active, key.char may be None even for
        regular letter keys.  Fall back to key.vk (virtual-key code) which
        always reflects the physical key.
        """
        if self._target_key is not None:
            return key == self._target_key
        if self._target_char is not None:
            # 1) Try key.char first (works when no IME is intercepting)
            try:
                if hasattr(key, 'char') and key.char and key.char.lower() == self._target_char:
                    return True
            except AttributeError:
                pass
            # 2) Fall back to virtual-key code (works with any IME)
            try:
                if hasattr(key, 'vk') and key.vk is not None:
                    target_vk = ord(self._target_char.upper())  # e.g. 'v' -> 86 (0x56)
                    return key.vk == target_vk
            except (AttributeError, TypeError):
                pass
        return False


@dataclass
class MicConfig:
    """Microphone capture configuration"""

    enable: bool = False
    room_id: int = 0
    subtitle_path: str = "subtitles1.txt"
    silence_threshold: float = 0.01  # RMS threshold for speech detection
    silence_duration: float = 0.5  # Seconds of silence to end speech segment
    sample_rate: int = 16000
    channels: int = 1
    on_speech_start: Optional[Callable[[], Awaitable[None]]] = None
    on_speech_end: Optional[Callable[[], Awaitable[None]]] = None
    push_to_talk: bool = False       # Enable push-to-talk mode
    ptt_key: str = "v"               # Key to hold for push-to-talk


class MicCaptureWorker:
    """
    Capture local microphone audio and drive one incremental ASR stream.

    Supports Push-to-Talk (PTT) mode: when enabled, audio frames are only
    processed while the configured key is held down.
    """

    ASR_SAMPLE_RATE = 16000
    ASR_PREROLL_SECONDS = 0.3
    STREAM_ID_PREFIX = "bilibili:microphone"

    def __init__(
        self,
        config: MicConfig,
        on_speech_recognized: Callable[[str], Awaitable[None]],
        logger,
        asr: Optional[StreamingASR] = None,
    ):
        self.config = config
        self.on_speech_recognized = on_speech_recognized
        self.logger = logger

        self._running = False
        self._is_speaking = False
        self._silence_samples = 0
        self._samples_per_chunk = int(config.sample_rate * 0.1)  # 100ms chunks
        self._silence_sample_threshold = max(
            1,
            int(
                config.silence_duration
                * config.sample_rate
                / self._samples_per_chunk
            ),
        )
        self._preroll_max_chunks = self._calculate_preroll_chunks()
        self._preroll_buffer: deque[bytes] = deque(
            maxlen=self._preroll_max_chunks
        )
        self._last_activity = 0.0
        self._state_lock = threading.RLock()

        # The sounddevice callback queues tiny lifecycle events. A coroutine
        # consumes them in FIFO order and runs CPU ASR work in a worker thread.
        self._processing_queue: queue.Queue[
            tuple[str, Optional[bytes]]
        ] = queue.Queue()
        self._paused = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._asr = asr
        self._asr_stream_active = False
        self._stream_id = f"{self.STREAM_ID_PREFIX}:{config.room_id}"

        # Push-to-Talk state
        self._ptt_monitor: Optional[PTTKeyMonitor] = None
        if config.push_to_talk:
            try:
                self._ptt_monitor = PTTKeyMonitor(config.ptt_key, logger)
            except Exception:
                logger.warning("PTT monitor failed to initialize, mic will use continuous capture mode")
                self._ptt_monitor = None

    def _calculate_preroll_chunks(self) -> int:
        return max(
            1,
            int(
                np.ceil(
                    self.ASR_PREROLL_SECONDS
                    * self.config.sample_rate
                    / max(1, self._samples_per_chunk)
                )
            ),
        )

    def _ensure_asr(self) -> bool:
        if self._asr is None:
            self._asr = StreamingASR(
                logger=logging.getLogger("BilibiliStreamingASR")
            )
        if not self._asr.supports_streaming:
            self.logger.error(
                "Bilibili microphone streaming ASR unavailable; check "
                "Multimodal ASR model and CPU runtime"
            )
            return False
        return True

    def pause(self) -> None:
        """Pause microphone capture"""
        if not self._paused:
            self._paused = True
            if not self._ptt_monitor:
                with self._state_lock:
                    self._finish_capture_stream("microphone paused")
                    self._preroll_buffer.clear()
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
        """Queue PCM chunks from sounddevice's real-time callback thread."""
        self._last_activity = time.time()
        if status:
            self.logger.warning(f"Audio stream status: {status}")

        # If paused, skip processing — UNLESS PTT mode is active
        # (in PTT mode, the key press is the sole gate; the control loop's
        #  pause state should not block PTT-gated audio)
        if self._paused and not self._ptt_monitor:
            with self._state_lock:
                self._finish_capture_stream("microphone paused")
                self._preroll_buffer.clear()
            return

        # PTT gate: skip frame if push-to-talk is enabled and key is not held
        if self._ptt_monitor and not self._ptt_monitor.is_held:
            with self._state_lock:
                self._finish_capture_stream("PTT released")
                self._preroll_buffer.clear()
            return

        audio_bytes = indata.tobytes()
        rms = self._calculate_rms(audio_bytes)
        is_speech = rms > self.config.silence_threshold
        notify_speech_start = False

        with self._state_lock:
            if not self._is_speaking:
                self._preroll_buffer.append(audio_bytes)

            if is_speech:
                if not self._is_speaking:
                    self._is_speaking = True
                    notify_speech_start = True
                    self._processing_queue.put(("start", None))
                    for chunk in self._preroll_buffer:
                        self._processing_queue.put(("audio", chunk))
                    self._preroll_buffer.clear()

                    if self._ptt_monitor:
                        self.logger.info(
                            f"Speech started (PTT active, rms={rms:.4f})"
                        )
                    else:
                        self.logger.debug("Speech started")
                else:
                    self._processing_queue.put(("audio", audio_bytes))
                self._silence_samples = 0
            elif self._is_speaking:
                # Continue decoding trailing silence until the VAD endpoint.
                self._processing_queue.put(("audio", audio_bytes))
                self._silence_samples += 1

                if self._silence_samples >= self._silence_sample_threshold:
                    self._finish_capture_stream("VAD endpoint")

        if (
            notify_speech_start
            and self.config.on_speech_start
            and self._loop
            and self._loop.is_running()
        ):
            asyncio.run_coroutine_threadsafe(
                self.config.on_speech_start(),
                self._loop,
            )

    def _finish_capture_stream(self, reason: str) -> None:
        """Queue finalization for the current utterance; caller holds state lock."""
        if not self._is_speaking:
            return
        self._is_speaking = False
        self._silence_samples = 0
        self._processing_queue.put(("finish", None))
        self.logger.debug(f"Speech stream ended ({reason})")

    async def _process_queue_loop(self) -> None:
        """Consume ordered streaming-ASR events without blocking capture."""
        while self._running or not self._processing_queue.empty():
            try:
                try:
                    event_name, audio_data = self._processing_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue

                try:
                    await self._process_stream_event(event_name, audio_data)
                finally:
                    self._processing_queue.task_done()
            except Exception:
                self.logger.exception("Error in streaming ASR loop")
                if self._asr and self._asr_stream_active:
                    await asyncio.to_thread(
                        self._asr.abort_stream,
                        self._stream_id,
                    )
                self._asr_stream_active = False

    async def _process_stream_event(
        self,
        event_name: str,
        audio_data: Optional[bytes],
    ) -> None:
        if not self._asr:
            return

        if event_name == "start":
            self._asr_stream_active = bool(
                await asyncio.to_thread(
                    self._asr.start_stream,
                    self._stream_id,
                )
            )
            return

        if event_name == "audio":
            if self._asr_stream_active and audio_data:
                await asyncio.to_thread(
                    self._accept_pcm_chunk,
                    audio_data,
                )
            return

        if event_name == "finish":
            text = None
            try:
                if self._asr_stream_active:
                    text = await asyncio.to_thread(
                        self._asr.finish_stream,
                        self._stream_id,
                    )
                self._asr_stream_active = False
                text = self._sanitize_text(text)
                if text:
                    self.logger.info(f"Streaming ASR recognized: {text}")
                    self._update_subtitle(text)
                    await self.on_speech_recognized(text)
            finally:
                if self.config.on_speech_end:
                    await self.config.on_speech_end()
            return

        if event_name == "abort":
            if self._asr_stream_active:
                await asyncio.to_thread(
                    self._asr.abort_stream,
                    self._stream_id,
                )
            self._asr_stream_active = False

    def _accept_pcm_chunk(self, pcm_data: bytes) -> Optional[str]:
        """Convert one int16 capture chunk and decode it incrementally."""
        sample_width = 2 * self.config.channels
        usable = len(pcm_data) - (len(pcm_data) % sample_width)
        if usable <= 0 or not self._asr:
            return None

        samples = np.frombuffer(pcm_data[:usable], dtype=np.int16)
        if self.config.channels > 1:
            samples = samples.reshape(-1, self.config.channels).mean(axis=1)
        samples = samples.astype(np.float32) / 32768.0
        if self.config.sample_rate != self.ASR_SAMPLE_RATE:
            samples = resample_poly(
                samples,
                self.ASR_SAMPLE_RATE,
                self.config.sample_rate,
            )
        samples_16k = np.ascontiguousarray(samples, dtype=np.float32)
        return self._asr.accept_stream_audio(
            self._stream_id,
            samples_16k,
        )

    @staticmethod
    def _sanitize_text(text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        sanitized = "".join(
            character
            for character in text.strip()
            if character.isprintable() or character in "\n\r\t"
        )
        return sanitized or None

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

        self._loop = asyncio.get_running_loop()
        if not await asyncio.to_thread(self._ensure_asr):
            return

        self._running = True
        proc_task: Optional[asyncio.Task] = None
        try:
            self.logger.info(
                "Starting streaming microphone capture "
                f"(sample_rate={self.config.sample_rate}, "
                f"threshold={self.config.silence_threshold})"
            )

            # Query the native rate to avoid exclusive-mode / OBS conflicts.
            try:
                device_info = sd.query_devices(kind="input")
                native_rate = int(device_info.get("default_samplerate", 16000))
                self.logger.info(f"Device native sample rate: {native_rate}")

                # Update config to match native rate
                self.config.sample_rate = native_rate

                # Recalculate VAD parameters dependent on sample rate
                self._samples_per_chunk = int(self.config.sample_rate * 0.1)
                self._silence_sample_threshold = max(
                    1,
                    int(
                        self.config.silence_duration
                        * self.config.sample_rate
                        / self._samples_per_chunk
                    ),
                )
                self._preroll_max_chunks = self._calculate_preroll_chunks()
                self._preroll_buffer = deque(
                    maxlen=self._preroll_max_chunks
                )
            except Exception as e:
                self.logger.warning(
                    f"Failed to query device native rate, using default 16000: {e}"
                )

            if self._ptt_monitor:
                self._ptt_monitor.start()
                self.logger.info("Microphone in Push-to-Talk mode")
            else:
                self.logger.info("Microphone in continuous capture mode")

            proc_task = asyncio.create_task(self._process_queue_loop())

            with sd.InputStream(
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                blocksize=self._samples_per_chunk,
                callback=self._audio_callback,
            ) as stream:
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
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Microphone capture error")
        finally:
            with self._state_lock:
                self._finish_capture_stream("capture stopped")
            self._running = False
            if proc_task:
                await proc_task
            if self._asr and self._asr_stream_active:
                await asyncio.to_thread(
                    self._asr.abort_stream,
                    self._stream_id,
                )
                self._asr_stream_active = False
            if self._ptt_monitor:
                self._ptt_monitor.stop()

    def stop(self) -> None:
        """Stop microphone capture"""
        with self._state_lock:
            self._finish_capture_stream("worker stopped")
        self._running = False
        if self._ptt_monitor:
            self._ptt_monitor.stop()
        self.logger.info("Microphone capture stopped")
