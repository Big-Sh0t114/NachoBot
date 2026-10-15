"""
Audio Output Module - Plays Core-produced audio to a virtual audio cable device.

Uses sounddevice to output WAV audio files to a specified virtual audio
cable (e.g., VB-Audio Virtual Cable), allowing the bot's voice to be
piped into any application's microphone input.
"""

import asyncio
import base64
import binascii
import logging
import wave
import os
import queue
import threading
from collections import deque
from typing import Optional

import numpy as np

from config import AudioOutputConfig


_STREAM_STOP = object()


class _StreamingResampler:
    """Small stateful linear PCM resampler used only by the stream worker."""

    def __init__(self, source_rate: int, target_rate: int, channels: int):
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.channels = channels
        self.step = source_rate / target_rate
        self._buffer = np.empty((0, channels), dtype=np.float32)
        self._buffer_start = 0
        self._input_frames = 0
        self._next_position = 0.0

    def process(self, samples: np.ndarray) -> np.ndarray:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1, self.channels)
        if not len(samples):
            return np.empty((0, self.channels), dtype=np.float32)
        if self.source_rate == self.target_rate:
            return samples

        if len(self._buffer):
            self._buffer = np.concatenate((self._buffer, samples), axis=0)
        else:
            self._buffer = samples.copy()
        self._input_frames += len(samples)

        # Keep one source frame to the right of every output sample so linear
        # interpolation stays continuous across incoming chunk boundaries.
        last_position = self._input_frames - 2
        if self._next_position > last_position:
            self._trim_old_frames()
            return np.empty((0, self.channels), dtype=np.float32)

        count = int((last_position - self._next_position) / self.step) + 1
        positions = self._next_position + np.arange(count, dtype=np.float64) * self.step
        left_global = np.floor(positions).astype(np.int64)
        right_global = left_global + 1
        left = left_global - self._buffer_start
        right = right_global - self._buffer_start
        fraction = (positions - left_global).astype(np.float32)[:, None]
        output = self._buffer[left] + (self._buffer[right] - self._buffer[left]) * fraction
        self._next_position += count * self.step
        self._trim_old_frames()
        return np.asarray(output, dtype=np.float32)

    def flush(self) -> np.ndarray:
        if self.source_rate == self.target_rate or not self._input_frames:
            return np.empty((0, self.channels), dtype=np.float32)

        positions = []
        current = self._next_position
        while current < self._input_frames:
            positions.append(current)
            current += self.step
        if not positions:
            return np.empty((0, self.channels), dtype=np.float32)

        positions_array = np.asarray(positions, dtype=np.float64)
        left_global = np.floor(positions_array).astype(np.int64)
        right_global = np.minimum(left_global + 1, self._input_frames - 1)
        left = left_global - self._buffer_start
        right = right_global - self._buffer_start
        fraction = (positions_array - left_global).astype(np.float32)[:, None]
        output = self._buffer[left] + (self._buffer[right] - self._buffer[left]) * fraction
        self._next_position = current
        self._buffer = np.empty((0, self.channels), dtype=np.float32)
        self._buffer_start = self._input_frames
        return np.asarray(output, dtype=np.float32)

    def _trim_old_frames(self):
        keep_from = max(self._buffer_start, int(self._next_position))
        trim = min(keep_from - self._buffer_start, len(self._buffer))
        if trim > 0:
            self._buffer = self._buffer[trim:].copy()
            self._buffer_start += trim


class _VoiceStreamPlayback:
    """One OutputStream with a bounded producer/worker PCM queue."""

    def __init__(
        self,
        *,
        sounddevice,
        device_id,
        stream_id: str,
        sample_rate: int,
        channels: int,
        sample_width: int,
        codec: str,
        parent_message_id: str,
        device_rate: int,
        output_channels: int,
        logger: logging.Logger,
        queue_chunks: int,
    ):
        self.sounddevice = sounddevice
        self.device_id = device_id
        self.stream_id = stream_id
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.codec = codec
        self.parent_message_id = parent_message_id
        self.device_rate = device_rate
        self.output_channels = output_channels
        self.logger = logger
        self.queue: queue.Queue = queue.Queue(maxsize=queue_chunks)
        self.output_stream = None
        self.worker: Optional[threading.Thread] = None
        self.worker_error: Optional[BaseException] = None
        self.finished = threading.Event()
        self.cancelled = threading.Event()
        self.expected_seq = 0
        self.ending = False

    def open(self):
        """Open the device stream away from the asyncio receive loop."""
        output_stream = self.sounddevice.OutputStream(
            samplerate=self.device_rate,
            channels=self.output_channels,
            dtype="float32",
            device=self.device_id,
        )
        try:
            output_stream.start()
        except Exception:
            try:
                output_stream.close()
            except Exception:
                pass
            raise
        self.output_stream = output_stream
        self.worker = threading.Thread(
            target=self._write_worker,
            name=f"UniversalVC-TTS-{self.stream_id[:24]}",
            daemon=True,
        )
        self.worker.start()

    def enqueue(self, pcm: bytes):
        if self.cancelled.is_set() or self.ending or self.finished.is_set():
            raise ValueError("voice stream is no longer accepting chunks")
        if self.worker_error is not None:
            raise RuntimeError("voice stream playback worker failed") from self.worker_error
        self.queue.put_nowait(pcm)
        self.expected_seq += 1

    def finish(self):
        if self.worker is None:
            return
        if self.worker.is_alive():
            self.queue.put(_STREAM_STOP)
            self.worker.join()
        if self.worker_error is not None:
            raise RuntimeError("voice stream playback worker failed") from self.worker_error

    def abort(self):
        self.cancelled.set()
        output_stream = self.output_stream
        if output_stream is not None:
            try:
                # PortAudio abort discards queued device audio and wakes a
                # writer blocked in OutputStream.write.
                output_stream.abort()
            except Exception:
                try:
                    output_stream.stop()
                except Exception:
                    pass
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
        if self.worker is not None:
            try:
                self.queue.put_nowait(_STREAM_STOP)
            except queue.Full:
                pass
            self.worker.join(timeout=2.0)
            if self.worker.is_alive() and output_stream is not None:
                try:
                    output_stream.close()
                except Exception:
                    pass
                self.worker.join(timeout=1.0)

    def matches(self, metadata: dict) -> bool:
        return (
            metadata["sample_rate"] == self.sample_rate
            and metadata["channels"] == self.channels
            and metadata["sample_width"] == self.sample_width
            and metadata["codec"] == self.codec
            and metadata["parent_message_id"] == self.parent_message_id
        )

    def _map_channels(self, samples: np.ndarray) -> np.ndarray:
        if self.output_channels == self.channels:
            return samples
        if self.output_channels == 2 and self.channels == 1:
            return np.repeat(samples, 2, axis=1)
        if self.output_channels == 1 and self.channels == 2:
            return np.mean(samples, axis=1, keepdims=True, dtype=np.float32)
        raise ValueError("unsupported voice stream output channel mapping")

    def _write_samples(self, samples: np.ndarray, resampler: _StreamingResampler):
        if not len(samples) or self.cancelled.is_set():
            return
        mapped = self._map_channels(samples)
        converted = resampler.process(mapped)
        if len(converted) and not self.cancelled.is_set():
            self.output_stream.write(converted)

    def _write_worker(self):
        resampler = _StreamingResampler(
            self.sample_rate, self.device_rate, self.output_channels
        )
        try:
            while True:
                item = self.queue.get()
                if item is _STREAM_STOP:
                    break
                if self.cancelled.is_set():
                    continue
                pcm = np.frombuffer(item, dtype="<i2")
                samples = pcm.reshape(-1, self.channels).astype(np.float32) / 32768.0
                self._write_samples(samples, resampler)

            if not self.cancelled.is_set():
                tail = resampler.flush()
                if len(tail):
                    self.output_stream.write(tail)
        except BaseException as exc:
            self.worker_error = exc
        finally:
            output_stream = self.output_stream
            if output_stream is not None:
                try:
                    if self.cancelled.is_set():
                        output_stream.abort()
                    else:
                        output_stream.stop()
                except Exception:
                    pass
                try:
                    output_stream.close()
                except Exception:
                    pass
            self.finished.set()


class AudioOutput:
    """
    Manages audio playback to a virtual audio cable device.
    Supports queuing, sequential playback, and interruption.
    """

    MAX_STREAM_ID_CHARS = 256
    MAX_PARENT_MESSAGE_ID_CHARS = 256
    MAX_STREAM_CHUNK_BYTES = 256 * 1024
    MAX_STREAM_QUEUE_CHUNKS = 8

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
        self._stream_lock = asyncio.Lock()
        self._voice_streams: dict[str, _VoiceStreamPlayback] = {}

    @classmethod
    def _stream_id(cls, value) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > cls.MAX_STREAM_ID_CHARS
        ):
            raise ValueError("voice stream has an invalid stream ID")
        return value

    @classmethod
    def _stream_metadata(cls, data: dict) -> dict:
        if not isinstance(data, dict):
            raise TypeError("voice stream event data must be an object")

        raw_format = data.get("format")
        if raw_format is not None and not isinstance(raw_format, dict):
            raise ValueError("voice stream format must be an object")
        raw_format = raw_format or {}
        metadata = {}
        for name in ("sample_rate", "channels", "sample_width", "codec"):
            top_value = data.get(name)
            format_value = raw_format.get(name)
            if top_value is not None and format_value is not None and top_value != format_value:
                raise ValueError("voice stream format metadata conflicts")
            value = top_value if top_value is not None else format_value
            if value is None:
                raise ValueError(f"voice stream omitted {name}")
            metadata[name] = value

        sample_rate = metadata["sample_rate"]
        channels = metadata["channels"]
        sample_width = metadata["sample_width"]
        codec = metadata["codec"]
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or not 8_000 <= sample_rate <= 192_000
        ):
            raise ValueError("voice stream sample rate is invalid")
        if isinstance(channels, bool) or not isinstance(channels, int) or channels not in (1, 2):
            raise ValueError("voice stream channel count is invalid")
        if isinstance(sample_width, bool) or not isinstance(sample_width, int) or sample_width != 2:
            raise ValueError("voice stream sample width must be 2 bytes")
        if codec != "pcm_s16le":
            raise ValueError("voice stream codec must be pcm_s16le")

        parent_message_id = data.get("parent_message_id")
        if (
            not isinstance(parent_message_id, str)
            or not parent_message_id.strip()
            or len(parent_message_id) > cls.MAX_PARENT_MESSAGE_ID_CHARS
        ):
            raise ValueError("voice stream has an invalid parent message ID")
        metadata["parent_message_id"] = parent_message_id
        return metadata

    def _stream_device_settings(self) -> tuple[int, int]:
        sd = self._ensure_sounddevice()
        info = None
        try:
            info = sd.query_devices(self._device_id) if self._device_id is not None else sd.query_devices(kind="output")
        except (TypeError, ValueError):
            try:
                devices = sd.query_devices()
                if isinstance(devices, list):
                    info = next(
                        (device for device in devices if device.get("max_output_channels", 0) > 0),
                        None,
                    )
            except Exception:
                info = None
        except Exception:
            info = None

        info = info if isinstance(info, dict) else {}
        try:
            device_rate = int(info.get("default_samplerate") or self.config.sample_rate)
        except (TypeError, ValueError):
            device_rate = int(self.config.sample_rate)
        if not 8_000 <= device_rate <= 192_000:
            device_rate = int(self.config.sample_rate)
        try:
            max_channels = int(info.get("max_output_channels", 2))
        except (TypeError, ValueError):
            max_channels = 2
        output_channels = 2 if max_channels >= 2 else 1
        return device_rate, output_channels

    async def start_voice_stream(self, data: dict):
        """Start one Core PCM stream without blocking the websocket receiver."""
        stream_id = self._stream_id(data.get("stream_id") if isinstance(data, dict) else None)
        metadata = self._stream_metadata(data)
        if len(self._voice_streams) >= 1 and stream_id in self._voice_streams:
            await self.abort_voice_stream(stream_id)
            raise ValueError("voice stream ID was started more than once")
        if self._is_paused:
            raise ValueError("voice stream arrived while microphone playback is paused")

        # A stream owns the device while it is active. Retire prior audio so a
        # buffered reply cannot overlap its OutputStream or survive a barge-in.
        await self._discard_buffered_playback()
        device_rate, output_channels = self._stream_device_settings()
        playback = _VoiceStreamPlayback(
            sounddevice=self._ensure_sounddevice(),
            device_id=self._device_id,
            stream_id=stream_id,
            sample_rate=metadata["sample_rate"],
            channels=metadata["channels"],
            sample_width=metadata["sample_width"],
            codec=metadata["codec"],
            parent_message_id=metadata["parent_message_id"],
            device_rate=device_rate,
            output_channels=output_channels,
            logger=self.logger,
            queue_chunks=self.MAX_STREAM_QUEUE_CHUNKS,
        )
        async with self._stream_lock:
            previous = list(self._voice_streams.values())
            self._voice_streams.clear()
            for active in previous:
                await asyncio.to_thread(active.abort)
            await asyncio.to_thread(playback.open)
            self._voice_streams[stream_id] = playback

    async def write_voice_stream_chunk(self, data: dict):
        """Validate and enqueue one base64 PCM chunk without waiting on audio IO."""
        stream_id = self._stream_id(data.get("stream_id") if isinstance(data, dict) else None)
        try:
            metadata = self._stream_metadata(data)
            seq = data.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise ValueError("voice stream sequence is invalid")
            encoded = data.get("audio_base64")
            max_encoded = ((self.MAX_STREAM_CHUNK_BYTES + 2) // 3) * 4
            if not isinstance(encoded, str) or not encoded or len(encoded) > max_encoded:
                raise ValueError("voice stream chunk is missing or too large")
            try:
                pcm = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("voice stream chunk is not valid base64") from exc
            if not pcm or len(pcm) > self.MAX_STREAM_CHUNK_BYTES:
                raise ValueError("voice stream chunk is empty or too large")
            if len(pcm) % (metadata["channels"] * metadata["sample_width"]):
                raise ValueError("voice stream chunk does not contain complete PCM frames")
        except Exception:
            await self.abort_voice_stream(stream_id)
            raise

        failure = None
        async with self._stream_lock:
            playback = self._voice_streams.get(stream_id)
            if playback is None:
                raise ValueError("voice stream chunk arrived without a start event")
            if playback.ending or not playback.matches(metadata):
                failure = ValueError("voice stream format or parent message changed")
                self._voice_streams.pop(stream_id, None)
            elif seq != playback.expected_seq:
                failure = ValueError("voice stream chunk sequence is out of order")
                self._voice_streams.pop(stream_id, None)
            else:
                try:
                    playback.enqueue(pcm)
                except queue.Full:
                    failure = BufferError("voice stream playback queue is full")
                    self._voice_streams.pop(stream_id, None)
                except Exception as exc:
                    failure = exc
                    self._voice_streams.pop(stream_id, None)
        if failure is not None:
            await asyncio.to_thread(playback.abort)
            raise failure

    async def end_voice_stream(self, data: dict):
        """Drain the bounded queue and close the stream on a worker thread."""
        stream_id = self._stream_id(data.get("stream_id") if isinstance(data, dict) else None)
        try:
            metadata = self._stream_metadata(data)
        except Exception:
            await self.abort_voice_stream(stream_id)
            raise

        async with self._stream_lock:
            playback = self._voice_streams.get(stream_id)
            if playback is None:
                raise ValueError("voice stream end arrived without an active stream")
            if playback.ending or not playback.matches(metadata):
                self._voice_streams.pop(stream_id, None)
                invalid = True
            else:
                playback.ending = True
                invalid = False
        if invalid:
            await asyncio.to_thread(playback.abort)
            raise ValueError("voice stream end format or parent message changed")

        try:
            await asyncio.to_thread(playback.finish)
        except Exception:
            await asyncio.to_thread(playback.abort)
            raise
        finally:
            async with self._stream_lock:
                if self._voice_streams.get(stream_id) is playback:
                    self._voice_streams.pop(stream_id, None)

    async def abort_voice_stream(self, data_or_id):
        """Immediately discard queued PCM and abort the device stream."""
        metadata = None
        metadata_error = None
        if isinstance(data_or_id, dict):
            stream_id = self._stream_id(data_or_id.get("stream_id"))
            try:
                metadata = self._stream_metadata(data_or_id)
            except Exception as exc:
                # A malformed abort still has to release the identified stream.
                metadata_error = exc
        else:
            stream_id = self._stream_id(data_or_id)
        async with self._stream_lock:
            playback = self._voice_streams.pop(stream_id, None)
        if playback is not None:
            if metadata is not None and not playback.matches(metadata):
                metadata_error = ValueError("voice stream abort format or parent message changed")
            await asyncio.to_thread(playback.abort)
        if metadata_error is not None:
            raise metadata_error

    async def _abort_all_voice_streams(self):
        async with self._stream_lock:
            streams = list(self._voice_streams.values())
            self._voice_streams.clear()
        if streams:
            await asyncio.gather(
                *(asyncio.to_thread(stream.abort) for stream in streams),
                return_exceptions=True,
            )

    async def _discard_buffered_playback(self):
        if self._current_stop_event:
            self._current_stop_event.set()
        while self._queue:
            self._cleanup_file(self._queue.popleft())
        if self._interrupted_wav:
            self._cleanup_file(self._interrupted_wav)
            self._interrupted_wav = None
        async with self._play_lock:
            pass

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
        await self._abort_all_voice_streams()
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
        await self._abort_all_voice_streams()

    async def stop_and_pause(self):
        """Stop current playback and pause the queue."""
        self._is_paused = True
        self.logger.info("AudioOutput paused")
        if self._current_stop_event:
            self._current_stop_event.set()
        await self._abort_all_voice_streams()

    async def stop(self):
        """Completely stop playback and clean up all remaining files."""
        self.logger.info("Stopping AudioOutput and cleaning up")
        self._is_paused = True
        if self._current_stop_event:
            self._current_stop_event.set()
        
        while self._queue:
            dropped = self._queue.popleft()
            self._cleanup_file(dropped)
            
        if self._interrupted_wav:
            self._cleanup_file(self._interrupted_wav)
            self._interrupted_wav = None

        await self._abort_all_voice_streams()
        async with self._play_lock:
            pass
        if self._interrupted_wav:
            self._cleanup_file(self._interrupted_wav)
            self._interrupted_wav = None
        self._is_paused = False

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
            self.logger.exception(f"Error playing WAV: {e}")
