"""
Audio Capture Module — Per-process and system-wide audio capture.

Enhanced Version: Uses an asynchronous frame buffer queue to completely decouple
WASAPI/sounddevice capture from heavy CPU pipeline processing, ensuring true real-time execution.
"""

import asyncio
import logging
import math
import time
from typing import Callable, Optional

import numpy as np

from .config import AudioCaptureConfig, MicrophoneConfig

# ProcTap output format: 48kHz, 2ch, float32
PROCTAP_SAMPLE_RATE = 48000
PROCTAP_CHANNELS = 2
PROCTAP_DTYPE = np.float32


class AudioCapture:
    """Captures audio from a target process or system-wide loopback without blocking the asyncio loop."""

    def __init__(
        self,
        capture_config: AudioCaptureConfig,
        logger: logging.Logger,
        on_frame: Optional[Callable] = None,
        *,
        strict_device_selection: bool = False,
        queue_size: int = 500,
        on_error: Optional[Callable[[str], object]] = None,
    ):
        self.capture_config = capture_config
        self.logger = logger
        self.on_frame = on_frame
        self.strict_device_selection = bool(strict_device_selection)
        self.queue_size = max(1, int(queue_size))
        self.on_error = on_error
        self._error_notified = False

        self._tap = None
        self._sd_stream = None
        self._system_mode = False
        self._running = False
        self._loop = None
        self._capture_task = None
        self._worker_task = None
        self._queue = None

        # Active capture format
        self._active_sample_rate = PROCTAP_SAMPLE_RATE
        self._active_channels = PROCTAP_CHANNELS

    def _resolve_pid(self) -> Optional[int]:
        """Resolve PID from config (explicit PID or process name)."""
        if self.capture_config.target_pid:
            return self.capture_config.target_pid

        if not self.capture_config.target_process_name:
            return None

        try:
            import psutil
        except ImportError:
            self.logger.error("psutil is required for process name resolution.")
            return None

        target_name = self.capture_config.target_process_name.lower()
        candidates = []
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            proc_name = (proc.info["name"] or "").lower()
            if proc_name == target_name or proc_name.replace(".exe", "") == target_name.replace(".exe", ""):
                mem = 0
                try:
                    mem = proc.info["memory_info"].rss if proc.info.get("memory_info") else 0
                except Exception:
                    pass
                candidates.append((proc.info["pid"], proc.info["name"], mem))

        if not candidates:
            self.logger.error(f"Process '{self.capture_config.target_process_name}' not found!")
            return None

        if len(candidates) == 1:
            pid, name, _ = candidates[0]
            self.logger.info(f"Found process '{name}' with PID {pid}")
            return pid

        self.logger.info(f"Found {len(candidates)} processes, probing for active audio...")
        audio_pid = self._probe_audio_pid(candidates)
        if audio_pid is not None:
            return audio_pid

        candidates.sort(key=lambda c: c[2], reverse=True)
        pid, name, mem_bytes = candidates[0]
        self.logger.warning(f"No audio detected, falling back to PID {pid} ({mem_bytes/1024/1024:.0f} MB)")
        return pid

    def _probe_audio_pid(self, candidates: list, probe_seconds: float = 1.5) -> Optional[int]:
        """Try each candidate PID with ProcTap for active audio."""
        try:
            from proctap import ProcessAudioCapture
        except ImportError:
            return None

        for pid, name, _ in candidates:
            try:
                tap = ProcessAudioCapture(pid=pid)
                tap.start()
                has_audio = False
                deadline = time.time() + probe_seconds
                while time.time() < deadline:
                    chunk = tap.read(timeout=0.2)
                    if chunk:
                        samples = np.frombuffer(chunk, dtype=PROCTAP_DTYPE)
                        if len(samples) > 0:
                            rms = float(np.sqrt(np.mean(samples ** 2)))
                            if int(rms * 32767) > 100:
                                has_audio = True
                                break
                tap.close()
                if has_audio:
                    self.logger.info(f"✓ PID {pid} ({name}) producing audio — selected!")
                    return pid
            except Exception as e:
                self.logger.debug(f"  PID {pid} probe failed: {e}")
                try:
                    tap.close()
                except Exception:
                    pass
        return None

    async def start(self, loop: asyncio.AbstractEventLoop):
        """Start capturing audio and initialize queue orchestration."""
        if self.strict_device_selection:
            has_process = bool(self.capture_config.target_pid or self.capture_config.target_process_name)
            has_device = bool(self.capture_config.system_capture_device)
            if has_process == has_device:
                raise ValueError("select exactly one process or input device")
            if has_process and self._resolve_pid() is None:
                raise RuntimeError("selected capture process was not found")
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=self.queue_size)
        self._running = True

        self._worker_task = asyncio.create_task(self._queue_worker_loop())

        pid = self._resolve_pid()
        if pid is not None:
            self._system_mode = False
            try:
                from proctap import ProcessAudioCapture
                self._tap = ProcessAudioCapture(pid=pid)
                self._tap.start()
                self._capture_task = asyncio.create_task(self._capture_loop())
                self.logger.info(f"Audio capture started for PID {pid}")
            except ImportError:
                self.logger.error("proc-tap is required! pip install proc-tap")
                if self.strict_device_selection:
                    raise RuntimeError("selected process capture backend is unavailable")
            except Exception as e:
                self.logger.error(f"Failed to start capture: {e}")
                if self.strict_device_selection:
                    raise RuntimeError("selected process capture could not start") from e
        else:
            self._system_mode = True
            await self._start_system_capture()

    def _resolve_system_device(self) -> Optional[int]:
        """Find the system loopback input device."""
        try:
            import sounddevice as sd
        except ImportError:
            self.logger.error("sounddevice is required for system capture")
            return None

        devices = sd.query_devices()
        target_name = (self.capture_config.system_capture_device or "").lower()

        if self.strict_device_selection:
            if not target_name:
                return None
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and target_name == dev["name"].lower():
                    self.logger.info(f"System capture device: [{i}] {dev['name']}")
                    return i
            self.logger.error("Selected system capture device was not found")
            return None

        if target_name:
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and target_name in dev["name"].lower():
                    self.logger.info(f"System capture device: [{i}] {dev['name']}")
                    return i

        stereo_mix_keywords = ["stereo mix", "立体声混音", "ステレオ ミキサー", "loopback"]
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                if any(kw in dev["name"].lower() for kw in stereo_mix_keywords):
                    self.logger.info(f"Auto-detected loopback: [{i}] {dev['name']}")
                    return i

        try:
            default_input = sd.default.device[0]
            if default_input is not None and default_input >= 0:
                return int(default_input)
        except Exception:
            pass
        self.logger.error("No suitable input device found!")
        return None

    async def _start_system_capture(self):
        """Start system-wide capture using sounddevice."""
        device_id = self._resolve_system_device()
        if device_id is None:
            if self.strict_device_selection:
                raise RuntimeError("selected system capture device was not found")
            return

        try:
            import sounddevice as sd
            dev_info = sd.query_devices(device_id)
            channels = min(int(dev_info["max_input_channels"]), 2)
            samplerate = int(dev_info["default_samplerate"])

            self._sys_samplerate = samplerate
            self._sys_channels = channels

            self._sd_stream = sd.InputStream(
                device=device_id,
                samplerate=samplerate,
                channels=channels,
                dtype="float32",
                blocksize=int(samplerate * 0.02),
                callback=self._system_audio_callback,
            )
            self._sd_stream.start()
            self.logger.info(f"System capture started: {samplerate}Hz, {channels}ch")
        except Exception as e:
            self.logger.exception(f"Failed to start system capture: {e}")
            if self.strict_device_selection:
                raise RuntimeError("selected system capture device could not start") from e

    def _system_audio_callback(self, indata, frames, time_info, status):
        """sounddevice callback — thread-safe injection into asyncio queue."""
        if not self._running:
            return
        if self._loop and self._queue:
            self._loop.call_soon_threadsafe(
                self._put_nowait_safe,
                (indata.tobytes(), self._sys_samplerate, self._sys_channels)
            )

    def _put_nowait_safe(self, item):
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            if self.strict_device_selection:
                self._signal_failure("capture frame queue overflowed; audio continuity was lost")

    async def stop(self):
        """Stop capturing audio and clean up threads."""
        self._running = False
        if self._capture_task:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if self._tap:
            try:
                self._tap.close()
            except Exception as e:
                self.logger.error(f"Error closing ProcTap: {e}")
            self._tap = None
        if self._sd_stream:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception as e:
                self.logger.error(f"Error closing system capture: {e}")
            self._sd_stream = None
        self.logger.info("Audio capture stopped")

    async def _capture_loop(self):
        """⚡ 极致性能优化：捕获循环只管秒推队列，绝不参与同步计算 ⚡"""
        self.logger.info("Capture loop started")
        try:
            async for chunk in self._tap.iter_chunks():
                if not self._running:
                    break
                try:
                    self._queue.put_nowait((chunk, PROCTAP_SAMPLE_RATE, PROCTAP_CHANNELS))
                except asyncio.QueueFull:
                    if self.strict_device_selection:
                        self._signal_failure("capture frame queue overflowed; audio continuity was lost")
                        return
                    try:
                        self._queue.get_nowait()
                        self._queue.put_nowait((chunk, PROCTAP_SAMPLE_RATE, PROCTAP_CHANNELS))
                    except Exception:
                        pass
        except asyncio.CancelledError:
            self.logger.info("Capture loop cancelled")
        except Exception as e:
            self.logger.exception(f"Capture loop error: {e}")
            if self.strict_device_selection:
                self._signal_failure("process capture worker failed")

    async def _queue_worker_loop(self):
        """后台顺序工作线程：逐帧在线程池中处理音频，释放主事件循环，保持严格时序"""
        self.logger.info("Audio pipeline sequential worker loop active")
        while self._running:
            try:
                item = await self._queue.get()
                if item is None:
                    continue
                pcm_bytes, sr, ch = item

                if self.on_frame:
                    await self._loop.run_in_executor(None, self.on_frame, pcm_bytes, sr, ch)

                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.exception(f"Pipeline worker sequence exception: {e}")
                if self.strict_device_selection:
                    self._signal_failure("capture pipeline worker failed")
                    break

    def _signal_failure(self, reason: str) -> None:
        if self._error_notified or not self.strict_device_selection:
            return
        self._error_notified = True
        self._running = False
        self.logger.error(reason)
        loop = self._loop
        callback = self.on_error
        if loop is None or callback is None or loop.is_closed():
            return

        def deliver():
            try:
                result = callback(reason)
                if asyncio.iscoroutine(result):
                    loop.create_task(result)
            except Exception:
                self.logger.exception("capture failure callback failed")

        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass

    def get_application_name(self) -> str:
        """Get the friendly name of the captured application."""
        if not self.capture_config.target_process_name and not self.capture_config.target_pid:
            return "系统音频"

        name = ""
        if self.capture_config.target_process_name:
            name = self.capture_config.target_process_name
        elif self.capture_config.target_pid:
            try:
                import psutil
                proc = psutil.Process(self.capture_config.target_pid)
                name = proc.name()
            except Exception:
                name = f"PID {self.capture_config.target_pid}"

        if not name:
            return "未知应用"

        name_lower = name.lower()
        if name_lower.endswith(".exe"):
            name = name[:-4]
            name_lower = name.lower()

        mapping = {
            "qq": "QQ", "wechat": "微信", "discord": "Discord",
            "vrchat": "VRChat", "dingtalk": "钉钉", "feishu": "飞书",
            "lark": "飞书", "tencentmeeting": "腾讯会议",
        }
        return mapping.get(name_lower, name)


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

    def __init__(self, key_name: str, logger: logging.Logger):
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
                # Single character key
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


class MicrophoneCapture:
    """Captures audio from the local microphone for owner voice input.

    Supports Push-to-Talk (PTT) mode: when enabled, audio frames are only
    forwarded to the pipeline while the configured key is held down.
    """

    def __init__(
        self,
        config: MicrophoneConfig,
        logger: logging.Logger,
        on_frame: Optional[Callable] = None,
        *,
        strict_device_selection: bool = False,
        queue_size: int = 500,
        on_error: Optional[Callable[[str], object]] = None,
        ptt_release_silence_duration: float | None = None,
    ):
        self.config = config
        self.logger = logger
        self.on_frame = on_frame
        self.strict_device_selection = bool(strict_device_selection)
        self.queue_size = max(1, int(queue_size))
        self.on_error = on_error
        self._error_notified = False
        self.ptt_release_silence_duration = (
            float(ptt_release_silence_duration)
            if ptt_release_silence_duration is not None
            else None
        )
        if self.ptt_release_silence_duration is not None and not 0 < self.ptt_release_silence_duration <= 5:
            raise ValueError("PTT release silence duration must be in (0, 5] seconds")
        self._ptt_was_held = False

        self._sd_stream = None
        self._running = False
        self._loop = None
        self._worker_task = None
        self._queue = None
        self._samplerate = 16000
        self._channels = 1

        # Push-to-Talk state
        self._ptt_monitor: Optional[PTTKeyMonitor] = None
        if config.push_to_talk:
            try:
                self._ptt_monitor = PTTKeyMonitor(config.ptt_key, logger)
            except Exception:
                if self.strict_device_selection:
                    raise RuntimeError("requested microphone push-to-talk is unavailable")
                logger.warning("PTT monitor failed to initialize, mic will use continuous capture mode")
                self._ptt_monitor = None

    def _resolve_device(self) -> Optional[int]:
        """Find the microphone input device."""
        try:
            import sounddevice as sd
        except ImportError:
            self.logger.error("sounddevice is required for microphone capture")
            return None

        devices = sd.query_devices()
        target_name = (self.config.device_name or "").lower()

        if self.strict_device_selection:
            if not target_name:
                return None
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and target_name == dev["name"].lower():
                    self.logger.info(f"Microphone device: [{i}] {dev['name']}")
                    return i
            self.logger.error("Selected microphone device was not found")
            return None

        if target_name:
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and target_name in dev["name"].lower():
                    self.logger.info(f"Microphone device: [{i}] {dev['name']}")
                    return i

        try:
            default_input = sd.default.device[0]
            if default_input is not None and default_input >= 0:
                self.logger.info(f"Using default microphone: [{default_input}] {devices[default_input]['name']}")
                return int(default_input)
        except Exception:
            pass
        self.logger.error("No suitable microphone device found!")
        return None

    async def start(self, loop: asyncio.AbstractEventLoop):
        """Start capturing microphone audio."""
        if not self.config.enabled:
            return

        device_id = self._resolve_device()
        if self.strict_device_selection and device_id is None:
            raise RuntimeError("selected microphone device was not found")

        self._loop = loop
        self._queue = asyncio.Queue(maxsize=self.queue_size)
        self._running = True

        # Start PTT keyboard listener if configured
        if self._ptt_monitor:
            self._ptt_monitor.start()
            self.logger.info("Microphone in Push-to-Talk mode")
        else:
            self.logger.info("Microphone in continuous capture mode")

        self._worker_task = asyncio.create_task(self._queue_worker_loop())

        if device_id is None:
            self._running = False
            return

        try:
            import sounddevice as sd
            dev_info = sd.query_devices(device_id)
            channels = min(int(dev_info["max_input_channels"]), 2)
            samplerate = int(dev_info["default_samplerate"])

            self._samplerate = samplerate
            self._channels = channels

            self._sd_stream = sd.InputStream(
                device=device_id,
                samplerate=samplerate,
                channels=channels,
                dtype="float32",
                blocksize=int(samplerate * 0.05),
                callback=self._audio_callback,
            )
            self._sd_stream.start()
            self.logger.info(f"Microphone capture started: {samplerate}Hz, {channels}ch")
        except Exception as e:
            self.logger.exception(f"Failed to start microphone capture: {e}")
            self._running = False
            if self.strict_device_selection:
                self._signal_failure("microphone capture backend failed to start")
                raise RuntimeError("selected microphone device could not start") from e

    def _audio_callback(self, indata, frames, time_info, status):
        """sounddevice callback — gated by PTT key state."""
        if not self._running:
            return
        # Never forward microphone contents while PTT is released. In service
        # mode, a single synthetic silence frame at the configured VAD
        # boundary flushes the held utterance before a rapid next press.
        if self._ptt_monitor:
            held = bool(self._ptt_monitor.is_held)
            if not held:
                if self.strict_device_selection and self._ptt_was_held and self.ptt_release_silence_duration:
                    if self._loop and self._queue:
                        self._loop.call_soon_threadsafe(self._queue_ptt_release_silence)
                self._ptt_was_held = False
                return
            self._ptt_was_held = True
        if self._loop and self._queue:
            self._loop.call_soon_threadsafe(
                self._put_nowait_safe,
                (indata.tobytes(), self._samplerate, self._channels)
            )

    def _queue_ptt_release_silence(self):
        if not self._running or self._queue is None or not self.ptt_release_silence_duration:
            return
        # Silero's VAD consumes fixed windows; include two 32ms windows so a
        # configured silence interval is not lost to its internal rounding.
        vad_window_margin = math.ceil((2 * 512) * self._samplerate / 16_000)
        frame_count = math.ceil(self._samplerate * self.ptt_release_silence_duration) + vad_window_margin
        silence = bytes(frame_count * self._channels * 4)  # float32 input format
        self._put_nowait_safe((silence, self._samplerate, self._channels))

    def _put_nowait_safe(self, item):
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            if self.strict_device_selection:
                self._signal_failure("microphone frame queue overflowed; audio continuity was lost")

    async def stop(self):
        """Stop capturing audio."""
        self._running = False
        # Stop PTT listener
        if self._ptt_monitor:
            self._ptt_monitor.stop()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if self._sd_stream:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception as e:
                self.logger.error(f"Error closing microphone capture: {e}")
            self._sd_stream = None
        self.logger.info("Microphone capture stopped")

    async def _queue_worker_loop(self):
        """Background worker to process microphone frames."""
        self.logger.info("Microphone worker loop active")
        while self._running:
            try:
                item = await self._queue.get()
                if item is None:
                    continue
                pcm_bytes, sr, ch = item

                if self.on_frame:
                    await self._loop.run_in_executor(None, self.on_frame, pcm_bytes, sr, ch)

                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.exception(f"Microphone worker exception: {e}")
                if self.strict_device_selection:
                    self._signal_failure("microphone capture worker failed")
                    break

    def _signal_failure(self, reason: str) -> None:
        if self._error_notified or not self.strict_device_selection:
            return
        self._error_notified = True
        self._running = False
        self.logger.error(reason)
        loop = self._loop
        callback = self.on_error
        if loop is None or callback is None or loop.is_closed():
            return

        def deliver():
            try:
                result = callback(reason)
                if asyncio.iscoroutine(result):
                    loop.create_task(result)
            except Exception:
                self.logger.exception("microphone failure callback failed")

        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass
