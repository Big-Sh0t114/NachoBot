"""Local microphone capture and VAD worker for the Bilibili adapter.

The adapter owns capture, VAD, and push-to-talk lifecycle only.  Each bounded
utterance is encoded as WAV and handed to Core as a ``voice`` segment; no ASR
model or local multimodal runtime is imported here.
"""

import asyncio
import io
import queue
import struct
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from math import ceil
from typing import Awaitable, Callable, Optional

try:
    import sounddevice as sd

    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False
    sd = None


class PTTKeyMonitor:
    """Global keyboard monitor for Push-to-Talk functionality."""

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
                self._target_key = getattr(
                    keyboard.Key, self._SPECIAL_KEYS[key_lower], None
                )
                if self._target_key is None:
                    self.logger.error(
                        "PTT: Unknown special key '%s', falling back to 'v'",
                        key_name,
                    )
                    self._target_char = "v"
            else:
                self._target_char = key_lower[0] if key_lower else "v"
        except ImportError:
            self.logger.error("pynput is required for push-to-talk")
            raise

    @property
    def is_held(self) -> bool:
        return self._key_held

    def start(self):
        try:
            from pynput import keyboard

            def on_press(key):
                if self._match_key(key):
                    self._key_held = True

            def on_release(key):
                if self._match_key(key):
                    self._key_held = False

            self._listener = keyboard.Listener(
                on_press=on_press, on_release=on_release
            )
            self._listener.daemon = True
            self._listener.start()
            key_display = self._target_char or str(self._target_key).replace("Key.", "")
            self.logger.info("PTT keyboard listener started — hold [{}] to talk", key_display)
        except Exception as exc:
            self.logger.error("Failed to start PTT keyboard listener: {}", exc)
            raise

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self._key_held = False

    def _match_key(self, key) -> bool:
        if self._target_key is not None:
            return key == self._target_key
        if self._target_char is None:
            return False
        try:
            if getattr(key, "char", None) and key.char.lower() == self._target_char:
                return True
        except AttributeError:
            pass
        try:
            return getattr(key, "vk", None) == ord(self._target_char.upper())
        except (AttributeError, TypeError):
            return False


@dataclass
class MicConfig:
    """Microphone capture configuration."""

    enable: bool = False
    room_id: int = 0
    # Kept for configuration compatibility; Core now owns transcript output.
    subtitle_path: str = "subtitles1.txt"
    silence_threshold: float = 0.01
    silence_duration: float = 0.5
    sample_rate: int = 16000
    channels: int = 1
    on_speech_start: Optional[Callable[[], Awaitable[None]]] = None
    on_speech_end: Optional[Callable[[], Awaitable[None]]] = None
    push_to_talk: bool = False
    ptt_key: str = "v"


class MicCaptureWorker:
    """Capture, VAD, and emit bounded WAV utterances for Core perception."""

    PREROLL_SECONDS = 0.3
    MAX_UTTERANCE_SECONDS = 30.0
    MAX_UTTERANCE_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        config: MicConfig,
        on_speech_recognized: Callable[[bytes], Awaitable[None]],
        logger,
    ):
        self.config = config
        self.on_speech_recognized = on_speech_recognized
        self.logger = logger
        self._running = False
        self._is_speaking = False
        self._silence_samples = 0
        self._samples_per_chunk = max(1, int(config.sample_rate * 0.1))
        self._silence_sample_threshold = max(
            1,
            int(
                config.silence_duration
                * config.sample_rate
                / self._samples_per_chunk
            ),
        )
        self._preroll_max_chunks = self._calculate_preroll_chunks()
        self._preroll_buffer: deque[bytes] = deque(maxlen=self._preroll_max_chunks)
        self._last_activity = 0.0
        self._state_lock = threading.RLock()
        self._processing_queue: queue.Queue[tuple[str, Optional[bytes]]] = queue.Queue()
        self._paused = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._utterance_chunks: list[bytes] = []
        self._utterance_bytes = 0
        self._utterance_max_bytes = min(
            self.MAX_UTTERANCE_BYTES,
            max(
                2 * max(1, config.channels),
                int(
                    self.MAX_UTTERANCE_SECONDS
                    * max(1, config.sample_rate)
                    * 2
                    * max(1, config.channels)
                ),
            ),
        )

        self._ptt_monitor: Optional[PTTKeyMonitor] = None
        if config.push_to_talk:
            try:
                self._ptt_monitor = PTTKeyMonitor(config.ptt_key, logger)
            except Exception:
                logger.warning(
                    "PTT monitor failed to initialize, mic will use continuous capture mode"
                )

    def _calculate_preroll_chunks(self) -> int:
        return max(
            1,
            int(
                ceil(
                    self.PREROLL_SECONDS
                    * self.config.sample_rate
                    / max(1, self._samples_per_chunk)
                )
            ),
        )

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            if not self._ptt_monitor:
                with self._state_lock:
                    self._finish_capture_stream("microphone paused")
                    self._preroll_buffer.clear()
            self.logger.info("Microphone capture paused")

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self.logger.info("Microphone capture resumed")

    def is_paused(self) -> bool:
        return self._paused

    def _calculate_rms(self, audio_data: bytes) -> float:
        if len(audio_data) < 2:
            return 0.0
        usable = len(audio_data) - (len(audio_data) % 2)
        samples = struct.unpack(f"<{usable // 2}h", audio_data[:usable])
        if not samples:
            return 0.0
        return (sum(sample * sample for sample in samples) / len(samples)) ** 0.5 / 32767.0

    def _audio_callback(self, indata, frames, time_info, status):
        del frames, time_info
        self._last_activity = time.time()
        if status:
            self.logger.warning("Audio stream status: {}", status)

        if self._paused and not self._ptt_monitor:
            with self._state_lock:
                self._finish_capture_stream("microphone paused")
                self._preroll_buffer.clear()
            return
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
                    self.logger.debug("Speech started (rms=%.4f)", rms)
                else:
                    self._processing_queue.put(("audio", audio_bytes))
                self._silence_samples = 0
            elif self._is_speaking:
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
            asyncio.run_coroutine_threadsafe(self.config.on_speech_start(), self._loop)

    def _finish_capture_stream(self, reason: str) -> None:
        if not self._is_speaking:
            return
        self._is_speaking = False
        self._silence_samples = 0
        self._processing_queue.put(("finish", None))
        self.logger.debug("Speech stream ended ({})", reason)

    def _reset_utterance(self) -> None:
        self._utterance_chunks.clear()
        self._utterance_bytes = 0

    def _append_utterance(self, pcm_data: bytes) -> None:
        if not pcm_data:
            return
        remaining = self._utterance_max_bytes - self._utterance_bytes
        if remaining <= 0:
            return
        usable = min(len(pcm_data), remaining)
        sample_width = max(1, 2 * self.config.channels)
        usable -= usable % sample_width
        if usable <= 0:
            return
        chunk = bytes(pcm_data[:usable])
        self._utterance_chunks.append(chunk)
        self._utterance_bytes += len(chunk)

    def _build_wav_payload(self) -> Optional[bytes]:
        if not self._utterance_chunks or self._utterance_bytes <= 0:
            return None
        with io.BytesIO() as buffer:
            with wave.open(buffer, "wb") as wav_file:
                wav_file.setnchannels(max(1, int(self.config.channels)))
                wav_file.setsampwidth(2)
                wav_file.setframerate(max(1, int(self.config.sample_rate)))
                wav_file.writeframes(b"".join(self._utterance_chunks))
            payload = buffer.getvalue()
        if len(payload) > self.MAX_UTTERANCE_BYTES:
            self.logger.warning(
                "Dropping oversized microphone utterance: bytes=%s", len(payload)
            )
            return None
        return payload

    async def _process_queue_loop(self) -> None:
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
                self.logger.exception("Error in microphone utterance loop")
                self._reset_utterance()

    async def _process_stream_event(
        self,
        event_name: str,
        audio_data: Optional[bytes],
    ) -> None:
        if event_name == "start":
            self._reset_utterance()
        elif event_name == "audio":
            self._append_utterance(audio_data or b"")
        elif event_name == "finish":
            try:
                wav_payload = self._build_wav_payload()
                if wav_payload:
                    await self.on_speech_recognized(wav_payload)
            finally:
                self._reset_utterance()
                if self.config.on_speech_end:
                    await self.config.on_speech_end()
        elif event_name == "abort":
            self._reset_utterance()

    async def start(self) -> None:
        if not SOUNDDEVICE_AVAILABLE:
            self.logger.error("sounddevice not installed. Run: pip install sounddevice")
            return
        if not self.config.enable:
            self.logger.info("Microphone capture disabled")
            return

        self._loop = asyncio.get_running_loop()
        self._running = True
        proc_task: Optional[asyncio.Task] = None
        try:
            self.logger.info(
                "Starting microphone capture with Core voice handoff "
                "(sample_rate=%s, threshold=%s)",
                self.config.sample_rate,
                self.config.silence_threshold,
            )
            try:
                device_info = sd.query_devices(kind="input")
                native_rate = int(device_info.get("default_samplerate", 16000))
                self.logger.info("Device native sample rate: {}", native_rate)
                self.config.sample_rate = native_rate
                self._samples_per_chunk = max(1, int(self.config.sample_rate * 0.1))
                self._silence_sample_threshold = max(
                    1,
                    int(
                        self.config.silence_duration
                        * self.config.sample_rate
                        / self._samples_per_chunk
                    ),
                )
                self._preroll_max_chunks = self._calculate_preroll_chunks()
                self._preroll_buffer = deque(maxlen=self._preroll_max_chunks)
                self._utterance_max_bytes = min(
                    self.MAX_UTTERANCE_BYTES,
                    int(
                        self.MAX_UTTERANCE_SECONDS
                        * self.config.sample_rate
                        * 2
                        * max(1, self.config.channels)
                    ),
                )
            except Exception as exc:
                self.logger.warning(
                    "Failed to query device native rate, using default 16000: %s", exc
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
                        self.logger.error("Audio stream is no longer active")
                        break
                    if time.time() - self._last_activity > 3.0:
                        self.logger.error("Audio stream watchdog timeout")
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
            if self._ptt_monitor:
                self._ptt_monitor.stop()

    def stop(self) -> None:
        with self._state_lock:
            self._finish_capture_stream("worker stopped")
        self._running = False
        if self._ptt_monitor:
            self._ptt_monitor.stop()
        self.logger.info("Microphone capture stopped")
