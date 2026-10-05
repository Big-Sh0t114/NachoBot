"""Core-owned streaming ASR transport for Discord voice utterances.

The adapter only forwards audio. Core remains responsible for model selection,
recognition, and issuing the result receipt attached to the final WAV segment.
"""

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field

import aiohttp
import numpy as np


CORE_STREAM_SAMPLE_RATE = 16_000
CORE_STREAM_CHANNELS = 1
DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2
DISCORD_FRAME_BYTES = DISCORD_CHANNELS * 2
MAX_STREAM_SECONDS = 60.0
STREAM_CHUNK_SECONDS = 0.16
STREAM_CHUNK_BYTES = int(CORE_STREAM_SAMPLE_RATE * STREAM_CHUNK_SECONDS) * 2


class CoreAudioStreamClient:
    """Persistent authenticated HTTP client for Core's audio stream API."""

    MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(self, host: str, port: int, token: str = ""):
        host = str(host).strip()
        if host.startswith("[") and host.endswith("]"):
            authority = host
        elif ":" in host:
            authority = f"[{host}]"
        else:
            authority = host
        self.base_url = f"http://{authority}:{int(port)}"
        self.token = str(token or "").strip()
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=15.0, connect=3.0, sock_read=10.0
                ),
                headers=headers,
                trust_env=False,
                connector=aiohttp.TCPConnector(limit=4),
            )
        return self._session

    async def _post(
        self, operation: str, payload: dict, *, expect_json: bool = True
    ) -> dict | None:
        session = await self._ensure_session()
        url = f"{self.base_url}/api/multimodal/audio/stream/{operation}"
        async with session.post(url, json=payload, allow_redirects=False) as response:
            if 300 <= response.status < 400 or response.status >= 400:
                raise RuntimeError(
                    f"Core audio stream HTTP status {response.status}"
                )
            if not expect_json:
                return None
            body = await response.content.read(self.MAX_RESPONSE_BYTES + 1)
            if len(body) > self.MAX_RESPONSE_BYTES:
                raise ValueError("Core audio stream response exceeded size limit")
            try:
                result = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "Core audio stream response was not valid JSON"
                ) from exc
            if not isinstance(result, dict):
                raise ValueError("Core audio stream response was not an object")
            return result

    async def start_stream(self, scope: str) -> str:
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 256:
            raise ValueError("Discord audio stream scope is required")
        result = await self._post(
            "start",
            {
                "sample_rate": CORE_STREAM_SAMPLE_RATE,
                "channels": CORE_STREAM_CHANNELS,
                "platform": "discord",
                "scope": scope,
            },
        )
        stream_id = result.get("stream_id") if result else None
        if (
            not isinstance(stream_id, str)
            or not stream_id.strip()
            or len(stream_id) > 256
        ):
            raise ValueError("Core returned an invalid stream ID")
        return stream_id

    async def send_chunk(self, stream_id: str, seq: int, pcm: bytes) -> None:
        result = await self._post(
            "chunk",
            {
                "stream_id": stream_id,
                "seq": seq,
                "pcm_base64": base64.b64encode(pcm).decode("ascii"),
            },
        )
        response_seq = result.get("seq") if result else None
        if (
            isinstance(response_seq, bool)
            or not isinstance(response_seq, int)
            or response_seq != seq
        ):
            raise ValueError("Core audio stream returned a mismatched sequence")

    async def finish_stream(self, stream_id: str) -> dict:
        result = await self._post("finish", {"stream_id": stream_id})
        if not result or not isinstance(result.get("text"), str):
            raise ValueError("Core audio stream finish response omitted text")
        result_id = result.get("result_id")
        if result_id is not None and (
            not isinstance(result_id, str) or len(result_id) > 256
        ):
            raise ValueError("Core audio stream returned an invalid result ID")
        return result

    async def abort_stream(self, stream_id: str) -> None:
        if self._session is None or self._session.closed:
            return
        await self._post("abort", {"stream_id": stream_id}, expect_json=False)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()


@dataclass
class _StreamState:
    stream_id: str
    scope: str
    total_input_bytes: int = 0
    seq: int = 0
    sample_remainder: list[int] = field(default_factory=list)
    output_buffer: bytearray = field(default_factory=bytearray)


class DiscordCoreAudioStreamBridge:
    """Convert Discord PCM and manage independent Core streams per utterance."""

    def __init__(self, client: CoreAudioStreamClient, logger: logging.Logger):
        self.client = client
        self.logger = logger
        self._streams: dict[str, _StreamState] = {}

    async def start(self, capture_id: str, scope: str) -> bool:
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 256:
            return False
        await self.abort(capture_id)
        request = asyncio.create_task(self.client.start_stream(scope))
        try:
            # Keep the request alive briefly when the caller is canceled so a
            # stream created by Core can still be identified and aborted.
            stream_id = await asyncio.shield(request)
        except asyncio.CancelledError:
            stream_id = None
            try:
                stream_id = await asyncio.wait_for(asyncio.shield(request), 3.0)
            except (asyncio.TimeoutError, Exception):
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            if isinstance(stream_id, str) and stream_id:
                await self._abort_core_stream(stream_id)
            raise
        except Exception as exc:
            self.logger.warning(
                "Core Discord audio stream start failed (%s); retaining WAV fallback",
                type(exc).__name__,
            )
            return False
        self._streams[capture_id] = _StreamState(stream_id=stream_id, scope=scope)
        return True

    async def send_pcm(self, capture_id: str, pcm_48k_stereo: bytes) -> bool:
        state = self._streams.get(capture_id)
        if state is None:
            return False
        try:
            state.total_input_bytes += len(pcm_48k_stereo)
            if state.total_input_bytes > int(
                DISCORD_SAMPLE_RATE
                * DISCORD_FRAME_BYTES
                * MAX_STREAM_SECONDS
            ):
                raise ValueError("Discord audio stream exceeded the duration limit")

            self._convert_to_16k_mono(state, pcm_48k_stereo, final=False)
            await self._send_ready_chunks(state)
            return True
        except Exception as exc:
            self.logger.warning(
                "Core Discord audio stream chunk failed (%s); retaining WAV fallback",
                type(exc).__name__,
            )
            await self._drop_stream(capture_id, state)
            return False
        except asyncio.CancelledError:
            await self._drop_stream(capture_id, state)
            raise

    async def finish(self, capture_id: str) -> str | None:
        state = self._streams.pop(capture_id, None)
        if state is None:
            return None
        try:
            self._convert_to_16k_mono(state, b"", final=True)
            await self._send_ready_chunks(state, final=True)
            result = await self.client.finish_stream(state.stream_id)
            result_id = result.get("result_id")
            if isinstance(result_id, str) and result_id.strip():
                return result_id
            return None
        except Exception as exc:
            self.logger.warning(
                "Core Discord audio stream finish failed (%s); retaining WAV fallback",
                type(exc).__name__,
            )
            await self._abort_core_stream(state.stream_id)
            return None
        except asyncio.CancelledError:
            await self._abort_core_stream(state.stream_id)
            raise

    async def abort(self, capture_id: str) -> None:
        state = self._streams.pop(capture_id, None)
        if state is not None:
            await self._abort_core_stream(state.stream_id)

    async def abort_all(self) -> None:
        states, self._streams = list(self._streams.values()), {}
        for state in states:
            await self._abort_core_stream(state.stream_id)

    async def _drop_stream(self, capture_id: str, state: _StreamState) -> None:
        if self._streams.get(capture_id) is state:
            self._streams.pop(capture_id, None)
        await self._abort_core_stream(state.stream_id)

    async def _abort_core_stream(self, stream_id: str) -> None:
        request = asyncio.create_task(self.client.abort_stream(stream_id))
        try:
            await asyncio.wait_for(asyncio.shield(request), 3.0)
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(asyncio.shield(request), 3.0)
            except (asyncio.TimeoutError, Exception):
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            raise
        except asyncio.TimeoutError:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            self.logger.debug("Core Discord audio stream abort timed out")
        except Exception as exc:
            self.logger.debug(
                "Core Discord audio stream abort failed (%s)", type(exc).__name__
            )

    @staticmethod
    def _convert_to_16k_mono(
        state: _StreamState, pcm_48k_stereo: bytes, *, final: bool
    ) -> None:
        """Downmix stereo and average each group of three 48 kHz samples."""
        if len(pcm_48k_stereo) % DISCORD_FRAME_BYTES:
            raise ValueError("Discord PCM ended in a partial stereo sample frame")

        if pcm_48k_stereo:
            stereo = np.frombuffer(pcm_48k_stereo, dtype="<i2").reshape(-1, 2)
            mono = (
                stereo[:, 0].astype(np.int32)
                + stereo[:, 1].astype(np.int32)
            ) // 2
            values = state.sample_remainder + mono.tolist()
        else:
            values = state.sample_remainder

        complete_samples = (len(values) // 3) * 3
        if final:
            process_samples = len(values)
        else:
            process_samples = complete_samples

        if process_samples:
            process = np.asarray(values[:process_samples], dtype=np.int32)
            groups = (process_samples + 2) // 3 if final else process_samples // 3
            if final and process_samples % 3:
                padded = np.zeros(groups * 3, dtype=np.int32)
                padded[:process_samples] = process
                counts = np.full(groups, 3, dtype=np.int32)
                counts[-1] = process_samples % 3
                converted = np.rint(
                    padded.reshape(groups, 3).sum(axis=1) / counts
                ).astype("<i2")
            else:
                converted = np.rint(
                    process.reshape(groups, 3).mean(axis=1)
                ).astype("<i2")
            state.output_buffer.extend(converted.tobytes())

        state.sample_remainder = [] if final else values[process_samples:]

    async def _send_ready_chunks(
        self, state: _StreamState, *, final: bool = False
    ) -> None:
        while len(state.output_buffer) >= STREAM_CHUNK_BYTES:
            chunk = bytes(state.output_buffer[:STREAM_CHUNK_BYTES])
            del state.output_buffer[:STREAM_CHUNK_BYTES]
            await self.client.send_chunk(state.stream_id, state.seq, chunk)
            state.seq += 1

        # Flush the final tail only when Core needs to finish the stream. Normal
        # chunks stay at 160 ms; the last partial chunk can be shorter.
        if final and state.output_buffer:
            chunk = bytes(state.output_buffer)
            state.output_buffer.clear()
            await self.client.send_chunk(state.stream_id, state.seq, chunk)
            state.seq += 1
