"""
Audio Capture Module - Per-process and system-wide audio capture with VAD.

Supports two capture modes:
  1. Per-process: Uses ProcTap (WASAPI) to capture audio from a specific process.
  2. System-wide: Uses sounddevice InputStream to capture from a loopback device
     (e.g. Stereo Mix) when no target process is configured.

Both modes feed into the same VAD → ASR pipeline.
"""

import asyncio
import io
import logging
import time
import wave
from typing import Callable, Optional

import numpy as np
import aiohttp

from config import AudioCaptureConfig, STTConfig

# ProcTap output format: 48kHz, 2ch, float32
PROCTAP_SAMPLE_RATE = 48000
PROCTAP_CHANNELS = 2
PROCTAP_DTYPE = np.float32

# ASR input: Mono 16-bit PCM at 48kHz
ASR_CHANNELS = 1
ASR_SAMPLE_WIDTH = 2  # 16-bit


class AudioCapture:
    """
    Captures audio from a target process, performs VAD, and sends
    recognized speech text via callback.
    """

    def __init__(
        self,
        capture_config: AudioCaptureConfig,
        stt_config: STTConfig,
        logger: logging.Logger,
        on_speech_text: Optional[Callable] = None,
        on_speech_start: Optional[Callable] = None,
    ):
        self.capture_config = capture_config
        self.stt_config = stt_config
        self.logger = logger
        self.on_speech_text = on_speech_text      # async callback(text: str)
        self.on_speech_start = on_speech_start    # async callback()

        self._tap = None
        self._sd_stream = None       # sounddevice InputStream for system capture
        self._system_mode = False    # True = system-wide capture, False = per-process
        self._running = False
        self._loop = None
        self._capture_task = None

        # VAD state
        self._utterance_buffer = bytearray()
        self._is_speaking = False
        self._last_speech_time = 0.0

        # Active capture format (set when capture starts)
        self._active_sample_rate = PROCTAP_SAMPLE_RATE
        self._active_channels = PROCTAP_CHANNELS

    def _resolve_pid(self) -> Optional[int]:
        """Resolve PID from config (explicit PID or process name).

        For Electron apps (Discord, Slack, etc.) that spawn many sub-processes,
        this method probes each candidate with a short ProcTap capture session
        to find the process that is actually producing audio output.
        """
        if self.capture_config.target_pid:
            return self.capture_config.target_pid

        if not self.capture_config.target_process_name:
            # No process specified → system-wide capture mode
            return None

        try:
            import psutil
        except ImportError:
            self.logger.error("psutil is required for process name resolution. Install with: pip install psutil")
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

        # Multiple candidates — common for Electron apps
        self.logger.info(
            f"Found {len(candidates)} '{self.capture_config.target_process_name}' processes, "
            f"probing for active audio..."
        )
        for pid, name, mem_bytes in candidates:
            mem_mb = mem_bytes / (1024 * 1024)
            self.logger.info(f"  PID {pid} — {name} ({mem_mb:.0f} MB)")

        # Probe each PID with ProcTap for a short duration
        audio_pid = self._probe_audio_pid(candidates)
        if audio_pid is not None:
            return audio_pid

        # Fallback: pick the process with the most memory (renderer is typically largest)
        candidates.sort(key=lambda c: c[2], reverse=True)
        pid, name, mem_bytes = candidates[0]
        mem_mb = mem_bytes / (1024 * 1024)
        self.logger.warning(
            f"No active audio detected during probing. "
            f"Falling back to largest-memory process: PID {pid} ({mem_mb:.0f} MB)"
        )
        return pid

    def _probe_audio_pid(self, candidates: list, probe_seconds: float = 1.5) -> Optional[int]:
        """Try each candidate PID with ProcTap and return the one producing audio.

        Args:
            candidates: List of (pid, name, mem_bytes) tuples.
            probe_seconds: How long to listen on each PID.

        Returns:
            The PID that is actively producing non-silent audio, or None.
        """
        try:
            from proctap import ProcessAudioCapture
        except ImportError:
            self.logger.error("proc-tap not installed, cannot probe audio PIDs")
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
                            rms_int16 = int(rms * 32767)
                            if rms_int16 > 10:  # very low threshold — just non-silence
                                has_audio = True
                                break
                tap.close()

                if has_audio:
                    self.logger.info(
                        f"✓ PID {pid} ({name}) is producing audio — selected!"
                    )
                    return pid
                else:
                    self.logger.debug(f"  PID {pid} ({name}) — silent")

            except Exception as e:
                self.logger.debug(f"  PID {pid} ({name}) — probe failed: {e}")
                try:
                    tap.close()
                except Exception:
                    pass

        return None

    async def start(self, loop: asyncio.AbstractEventLoop):
        """Start capturing audio.

        Automatically selects the capture mode:
          - Per-process (ProcTap) when target_pid or target_process_name is set.
          - System-wide (sounddevice loopback) when neither is configured.
        """
        self._loop = loop

        pid = self._resolve_pid()

        if pid is not None:
            # --- Per-process capture via ProcTap ---
            self._system_mode = False
            try:
                from proctap import ProcessAudioCapture
                self._tap = ProcessAudioCapture(pid=pid)
                self._tap.start()
                self._running = True
                self._capture_task = asyncio.create_task(self._capture_loop())
                self.logger.info(f"Audio capture started for PID {pid}")
            except ImportError:
                self.logger.error(
                    "proc-tap is required! Install with: pip install proc-tap\n"
                    "Or install from local source: pip install -e <path-to-ProcTap-main>"
                )
            except Exception as e:
                self.logger.error(f"Failed to start audio capture: {e}")
        else:
            # --- System-wide capture via sounddevice ---
            self._system_mode = True
            await self._start_system_capture()

    def _resolve_system_device(self) -> Optional[int]:
        """Find the system loopback input device for system-wide capture.

        Priority:
          1. Explicit device name from config (system_capture_device)
          2. Auto-detect: Stereo Mix / 立体声混音
          3. Fall back to system default input
        """
        try:
            import sounddevice as sd
        except ImportError:
            self.logger.error("sounddevice is required for system capture")
            return None

        devices = sd.query_devices()
        target_name = (self.capture_config.system_capture_device or "").lower()

        # 1. User-specified device name
        if target_name:
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and target_name in dev["name"].lower():
                    self.logger.info(f"System capture device: [{i}] {dev['name']}")
                    return i
            self.logger.warning(f"Configured system_capture_device '{self.capture_config.system_capture_device}' not found")

        # 2. Auto-detect Stereo Mix (EN/CN/JP variants)
        stereo_mix_keywords = ["stereo mix", "立体声混音", "ステレオ ミキサー", "loopback"]
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                name_lower = dev["name"].lower()
                if any(kw in name_lower for kw in stereo_mix_keywords):
                    self.logger.info(f"Auto-detected loopback device: [{i}] {dev['name']}")
                    return i

        # 3. Fall back to default input
        try:
            default_input = sd.default.device[0]
            if default_input is not None and default_input >= 0:
                dev = devices[default_input]
                self.logger.warning(
                    f"No loopback device found, using default input: [{default_input}] {dev['name']}"
                )
                return int(default_input)
        except Exception:
            pass

        self.logger.error("No suitable input device found for system capture!")
        return None

    async def _start_system_capture(self):
        """Start system-wide audio capture using sounddevice InputStream."""
        device_id = self._resolve_system_device()
        if device_id is None:
            return

        try:
            import sounddevice as sd

            dev_info = sd.query_devices(device_id)
            channels = min(int(dev_info["max_input_channels"]), 2)
            samplerate = int(dev_info["default_samplerate"])

            self.logger.info(
                f"Starting system capture: [{device_id}] {dev_info['name']} "
                f"({samplerate}Hz, {channels}ch)"
            )

            # Store for resampling in the callback
            self._sys_samplerate = samplerate
            self._sys_channels = channels

            self._sd_stream = sd.InputStream(
                device=device_id,
                samplerate=samplerate,
                channels=channels,
                dtype="float32",
                blocksize=int(samplerate * 0.02),  # 20ms blocks
                callback=self._system_audio_callback,
            )
            self._running = True
            self._sd_stream.start()
            self._capture_task = asyncio.create_task(self._system_silence_monitor())
            self.logger.info("System-wide audio capture started")

        except Exception as e:
            self.logger.error(f"Failed to start system capture: {e}", exc_info=True)

    def _system_audio_callback(self, indata, frames, time_info, status):
        """sounddevice InputStream callback — runs in audio thread."""
        if status:
            self.logger.debug(f"System capture status: {status}")
        if not self._running:
            return

        # Convert to float32 bytes (same format as ProcTap for unified VAD)
        pcm_bytes = indata.tobytes()
        self._process_chunk(pcm_bytes, sample_rate=self._sys_samplerate, channels=self._sys_channels)

    async def _system_silence_monitor(self):
        """Background task to check silence timeouts during system capture."""
        try:
            while self._running:
                await self._check_silence()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """Stop capturing audio."""
        self._running = False
        if self._capture_task:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
        # Stop per-process capture
        if self._tap:
            try:
                self._tap.close()
            except Exception as e:
                self.logger.error(f"Error closing ProcTap: {e}")
            self._tap = None
        # Stop system capture
        if self._sd_stream:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception as e:
                self.logger.error(f"Error closing system capture stream: {e}")
            self._sd_stream = None
        self.logger.info("Audio capture stopped")

    async def _capture_loop(self):
        """Main capture loop: read audio chunks, perform VAD, trigger ASR."""
        self.logger.info("Capture loop started")
        try:
            async for chunk in self._tap.iter_chunks():
                if not self._running:
                    break
                self._process_chunk(chunk)
                # Check for silence timeout
                await self._check_silence()
        except asyncio.CancelledError:
            self.logger.info("Capture loop cancelled")
        except Exception as e:
            self.logger.error(f"Capture loop error: {e}", exc_info=True)

    def _process_chunk(self, pcm_data: bytes, sample_rate: int = 0, channels: int = 0):
        """Process a single audio chunk: VAD detection and buffer management.

        Args:
            pcm_data: Raw float32 PCM bytes.
            sample_rate: Override sample rate (for system capture). 0 = use stored.
            channels: Override channel count (for system capture). 0 = use stored.
        """
        if sample_rate:
            self._active_sample_rate = sample_rate
        if channels:
            self._active_channels = channels
        try:
            # ProcTap delivers float32 stereo at 48kHz
            samples = np.frombuffer(pcm_data, dtype=PROCTAP_DTYPE)
            if len(samples) == 0:
                return

            # Calculate RMS for VAD
            rms = float(np.sqrt(np.mean(samples ** 2)))
            # Convert float32 RMS (0.0~1.0 range) to comparable int16 scale
            rms_int16 = int(rms * 32767)
            is_speech = rms_int16 > self.capture_config.vad_threshold
            now = time.time()

            if is_speech:
                self._last_speech_time = now
                if not self._is_speaking:
                    self._is_speaking = True
                    self.logger.debug(f"Speech started (RMS: {rms_int16})")
                    # Notify speech start (for interruption)
                    if self.on_speech_start and self._loop:
                        asyncio.run_coroutine_threadsafe(
                            self.on_speech_start(), self._loop
                        )

            # Always buffer when speaking
            if self._is_speaking:
                self._utterance_buffer.extend(pcm_data)

        except Exception as e:
            self.logger.error(f"Error processing audio chunk: {e}")

    async def _check_silence(self):
        """Check if silence threshold has been exceeded after speech."""
        if not self._is_speaking:
            return

        now = time.time()
        silence_duration = now - self._last_speech_time

        if silence_duration > self.capture_config.silence_threshold:
            # End of speech detected
            buffer_data = bytes(self._utterance_buffer)
            self._utterance_buffer = bytearray()
            self._is_speaking = False

            # Calculate duration
            bytes_per_frame = 4 * self._active_channels  # float32
            total_frames = len(buffer_data) // bytes_per_frame
            duration = total_frames / self._active_sample_rate

            if duration < self.capture_config.min_speech_duration:
                self.logger.debug(f"Speech too short ({duration:.2f}s), ignoring")
                return

            self.logger.info(f"Speech detected: {duration:.2f}s, sending to ASR...")

            # Process in background
            asyncio.create_task(self._process_speech(buffer_data))

    async def _process_speech(self, pcm_data: bytes):
        """Convert speech PCM to WAV → call ASR → callback with text."""
        try:
            channels = self._active_channels
            sample_rate = self._active_sample_rate

            # Convert float32 multi-channel → int16 mono for ASR
            samples = np.frombuffer(pcm_data, dtype=PROCTAP_DTYPE)
            if channels > 1:
                if len(samples) % channels != 0:
                    samples = samples[:len(samples) - (len(samples) % channels)]
                multi = samples.reshape(-1, channels)
                mono = multi.mean(axis=1)
            else:
                mono = samples
            # Convert float32 to int16
            int16_samples = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)

            # Create in-memory WAV
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, "wb") as wav_file:
                wav_file.setnchannels(ASR_CHANNELS)
                wav_file.setsampwidth(ASR_SAMPLE_WIDTH)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(int16_samples.tobytes())

            wav_buffer.seek(0)
            wav_data = wav_buffer.read()

            # Call ASR
            text = await self._call_asr_api(wav_data)
            if text and self.on_speech_text:
                await self.on_speech_text(text)

        except Exception as e:
            self.logger.error(f"Error processing speech: {e}", exc_info=True)

    async def _call_asr_api(self, wav_data: bytes) -> Optional[str]:
        """Send WAV data to ASR API and return recognized text."""
        if not self.stt_config.enabled:
            self.logger.warning("STT is not enabled")
            return None

        if not self.stt_config.api_key:
            self.logger.warning("No ASR API Key configured")
            return None

        url = f"{self.stt_config.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.stt_config.api_key}"}

        data = aiohttp.FormData()
        data.add_field("file", wav_data, filename="audio.wav", content_type="audio/wav")
        data.add_field("model", self.stt_config.model)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        self.logger.error(f"ASR API error {resp.status}: {text}")
                        return None

                    result = await resp.json()
                    text = result.get("text", "").strip()
                    if text:
                        # Sanitize: remove control characters
                        text = "".join(
                            ch for ch in text if ch.isprintable() or ch in "\n\r\t"
                        )
                        self.logger.info(f"ASR Recognized: {text}")
                        return text
                    return None
        except Exception as e:
            self.logger.error(f"ASR request failed: {e}")
            return None

    def get_application_name(self) -> str:
        """Get the friendly name of the captured application."""
        if not self.capture_config.target_process_name and not self.capture_config.target_pid:
            return "系统音频"

        # If a process name is configured
        name = ""
        if self.capture_config.target_process_name:
            name = self.capture_config.target_process_name
        elif self.capture_config.target_pid:
            # Try to resolve process name from PID
            try:
                import psutil
                proc = psutil.Process(self.capture_config.target_pid)
                name = proc.name()
            except Exception:
                name = f"PID {self.capture_config.target_pid}"

        if not name:
            return "未知应用"

        # Normalize and map to friendly name
        name_lower = name.lower()
        
        # Remove suffix like .exe
        if name_lower.endswith(".exe"):
            name = name[:-4]
            name_lower = name.lower()

        # Friendly mapping
        mapping = {
            "qq": "QQ",
            "wechat": "微信",
            "discord": "Discord",
            "vrchat": "VRChat",
            "dingtalk": "钉钉",
            "feishu": "飞书",
            "lark": "飞书",
            "tencentmeeting": "腾讯会议",
        }

        return mapping.get(name_lower, name)

