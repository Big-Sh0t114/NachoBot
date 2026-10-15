"""Bounded HTTP transport for Core's low-latency audio stream API."""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Mapping, Optional

import aiohttp


class CoreAudioStreamError(RuntimeError):
    """A Core audio streaming request failed or returned an invalid result."""


class CoreAudioStreamClient:
    """Reuse one aiohttp session for ordered, bounded Core audio streams."""

    MAX_CONCURRENT_STREAMS = 8
    MAX_QUEUED_CHUNKS = 64
    MAX_CHUNK_BYTES = 64 * 1024

    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        logger=None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        if not self.base_url:
            raise ValueError("Core base URL is required")
        self.token = str(token or "").strip()
        self.logger = logger
        self.timeout = max(0.1, min(float(timeout), 30.0))
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_slots = asyncio.Semaphore(self.MAX_CONCURRENT_STREAMS)
        self._senders: set[_CoreAudioStreamSender] = set()

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        session = await self._get_session()
        try:
            async with self._request_slots:
                async with session.post(
                    f"{self.base_url}{path}",
                    json=dict(payload),
                    headers=self._headers,
                ) as response:
                    if response.status >= 400:
                        raise CoreAudioStreamError(
                            f"Core audio stream request failed with status {response.status}"
                        )
                    body = await response.json(content_type=None)
        except CoreAudioStreamError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise CoreAudioStreamError(
                f"Core audio stream request unavailable: {type(exc).__name__}"
            ) from exc
        if not isinstance(body, Mapping):
            raise CoreAudioStreamError("Core audio stream response was not an object")
        return body

    async def open_stream(
        self,
        *,
        sample_rate: int,
        channels: int,
        platform: str = "bilibili",
    ) -> "_CoreAudioStreamSender":
        if len(self._senders) >= self.MAX_CONCURRENT_STREAMS:
            raise CoreAudioStreamError("Core audio stream concurrency limit reached")
        sender = _CoreAudioStreamSender(
            self,
            sample_rate=sample_rate,
            channels=channels,
            platform=platform,
        )
        self._senders.add(sender)
        sender._start()
        return sender

    def _forget(self, sender: "_CoreAudioStreamSender") -> None:
        self._senders.discard(sender)

    async def close(self) -> None:
        """Abort any live streams, then close the persistent HTTP session."""

        senders = tuple(self._senders)
        if senders:
            await asyncio.gather(*(sender.abort() for sender in senders), return_exceptions=True)
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()


class _CoreAudioStreamSender:
    """One ordered producer with bounded buffering and a single network task."""

    def __init__(
        self,
        client: CoreAudioStreamClient,
        *,
        sample_rate: int,
        channels: int,
        platform: str,
    ) -> None:
        self._client = client
        self._start_payload = {
            "sample_rate": int(sample_rate),
            "channels": int(channels),
            "platform": str(platform or "bilibili")[:64],
        }
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(
            maxsize=client.MAX_QUEUED_CHUNKS
        )
        self._task: Optional[asyncio.Task] = None
        self._stream_id: Optional[str] = None
        self._seq = 0
        self._failed = False
        self._abort_requested = False
        self._finished = False
        self._result: Optional[Mapping[str, Any]] = None

    def _start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def enqueue_chunk(self, pcm_s16le: bytes) -> bool:
        if self._failed or self._abort_requested or self._finished:
            return False
        if (
            not isinstance(pcm_s16le, (bytes, bytearray))
            or not pcm_s16le
            or len(pcm_s16le) > self._client.MAX_CHUNK_BYTES
            or len(pcm_s16le) % 2
        ):
            self._request_abort("invalid PCM chunk")
            return False
        seq = self._seq
        try:
            self._queue.put_nowait(("chunk", (seq, bytes(pcm_s16le))))
        except asyncio.QueueFull:
            self._request_abort("stream queue backpressure limit reached")
            return False
        self._seq += 1
        return True

    def _request_abort(self, reason: str) -> None:
        if not self._failed and self._client.logger:
            self._client.logger.warning(
                "Bilibili Core audio stream stopped: {}", reason
            )
        self._failed = True
        self._abort_requested = True
        try:
            self._queue.put_nowait(("abort", None))
        except asyncio.QueueFull:
            pass

    async def finish(self) -> Optional[Mapping[str, Any]]:
        if not self._failed and not self._abort_requested and not self._finished:
            try:
                self._queue.put_nowait(("finish", None))
            except asyncio.QueueFull:
                self._request_abort("stream queue backpressure limit reached")
        await self._wait_for_task()
        return self._result

    async def abort(self) -> None:
        if self._finished:
            return
        self._abort_requested = True
        try:
            self._queue.put_nowait(("abort", None))
        except asyncio.QueueFull:
            pass
        await self._wait_for_task()

    async def _wait_for_task(self) -> None:
        task = self._task
        if task is None:
            self._finished = True
            self._client._forget(self)
            return
        try:
            await task
        except asyncio.CancelledError:
            self._abort_requested = True
            raise

    async def _run(self) -> None:
        finalized = False
        try:
            response = await self._client._post(
                "/api/multimodal/audio/stream/start", self._start_payload
            )
            stream_id = response.get("stream_id")
            if not isinstance(stream_id, str) or not stream_id.strip():
                raise CoreAudioStreamError("Core did not return a stream id")
            self._stream_id = stream_id.strip()

            while True:
                if self._abort_requested:
                    break
                operation, payload = await self._queue.get()
                if operation == "abort" or self._abort_requested:
                    break
                if operation == "chunk":
                    seq, pcm_s16le = payload
                    response = await self._client._post(
                        "/api/multimodal/audio/stream/chunk",
                        {
                            "stream_id": self._stream_id,
                            "seq": seq,
                            "pcm_base64": base64.b64encode(pcm_s16le).decode("ascii"),
                        },
                    )
                    if response.get("seq") != seq:
                        raise CoreAudioStreamError("Core audio stream sequence mismatch")
                    continue
                if operation == "finish":
                    self._result = await self._client._post(
                        "/api/multimodal/audio/stream/finish",
                        {"stream_id": self._stream_id},
                    )
                    finalized = True
                    break
        except asyncio.CancelledError:
            self._abort_requested = True
            raise
        except Exception as exc:
            self._failed = True
            if self._client.logger:
                self._client.logger.warning(
                    "Bilibili Core audio stream failed; retaining WAV fallback: {}",
                    type(exc).__name__,
                )
        finally:
            if self._stream_id and not finalized:
                try:
                    await asyncio.shield(
                        self._client._post(
                            "/api/multimodal/audio/stream/abort",
                            {"stream_id": self._stream_id},
                        )
                    )
                except Exception:
                    pass
            self._finished = True
            self._client._forget(self)
