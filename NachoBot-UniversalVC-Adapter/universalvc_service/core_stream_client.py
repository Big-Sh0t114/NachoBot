"""Namespaced Core-owned streaming ASR transport used by the service engine."""

from __future__ import annotations

import base64
import json

import aiohttp


class CoreAudioStreamClient:
    """Persistent HTTP transport for Core-owned real-time audio ASR.

    ``platform`` and ``scope`` are optional to preserve the legacy adapter API.
    Service sessions pass both explicitly for every utterance.
    """

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
                timeout=aiohttp.ClientTimeout(total=15.0, connect=3.0, sock_read=10.0),
                headers=headers,
                trust_env=False,
                connector=aiohttp.TCPConnector(limit=4),
            )
        return self._session

    async def _post(self, operation: str, payload: dict, *, expect_json: bool = True):
        session = await self._ensure_session()
        url = f"{self.base_url}/api/multimodal/audio/stream/{operation}"
        async with session.post(url, json=payload, allow_redirects=False) as response:
            if 300 <= response.status < 400 or response.status >= 400:
                raise RuntimeError(f"Core audio stream HTTP status {response.status}")
            if not expect_json:
                return None
            body = await response.content.read(self.MAX_RESPONSE_BYTES + 1)
            if len(body) > self.MAX_RESPONSE_BYTES:
                raise ValueError("Core audio stream response exceeded size limit")
            try:
                result = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Core audio stream response was not valid JSON") from exc
            if not isinstance(result, dict):
                raise ValueError("Core audio stream response was not an object")
            return result

    async def start_stream(
        self,
        *,
        sample_rate: int,
        channels: int,
        platform: str | None = None,
        scope: str | None = None,
    ) -> str:
        payload = {"sample_rate": sample_rate, "channels": channels}
        if platform is not None:
            if not isinstance(platform, str) or not platform.strip() or len(platform) > 64:
                raise ValueError("Core audio stream platform is invalid")
            payload["platform"] = platform
        if scope is not None:
            if not isinstance(scope, str) or len(scope) > 256:
                raise ValueError("Core audio stream scope is invalid")
            payload["scope"] = scope
        result = await self._post("start", payload)
        stream_id = result.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id.strip() or len(stream_id) > 256:
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
        response_seq = result.get("seq")
        if isinstance(response_seq, bool) or not isinstance(response_seq, int) or response_seq != seq:
            raise ValueError("Core audio stream returned a mismatched sequence")

    async def finish_stream(self, stream_id: str) -> dict:
        result = await self._post("finish", {"stream_id": stream_id})
        if not isinstance(result.get("text"), str):
            raise ValueError("Core audio stream finish response omitted text")
        result_id = result.get("result_id")
        if result_id is not None and (not isinstance(result_id, str) or len(result_id) > 256):
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
