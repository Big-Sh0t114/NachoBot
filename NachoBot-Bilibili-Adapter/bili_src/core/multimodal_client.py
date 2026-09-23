"""Small HTTP facade for the Core multimodal API.

The Bilibili adapter deliberately knows only the wire contract here.  Model
selection, local-runtime routing, and perception fallbacks remain Core-owned.
This client is also used for non-chat idle speech so that idle playback never
has to manufacture a chat turn.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Mapping, Optional

import aiohttp


MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_TEXT_CHARS = 10_000


class CoreMultimodalError(RuntimeError):
    """A Core multimodal request could not be completed."""


def _encode_bytes(raw: bytes, *, max_bytes: int) -> str:
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ValueError("media payload is empty")
    if len(raw) > max_bytes:
        raise ValueError("media payload exceeds the Core multimodal limit")
    return base64.b64encode(raw).decode("ascii")


class CoreMultimodalClient:
    """Call Core's typed multimodal endpoints without importing model code."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        *,
        token: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        host = str(host or "127.0.0.1").strip()
        if host.startswith("http://") or host.startswith("https://"):
            self.base_url = host.rstrip("/")
        else:
            self.base_url = f"http://{host}:{int(port)}"
        self.token = str(token or "").strip()
        self.timeout = max(0.1, min(float(timeout), 300.0))

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}{path}",
                    json=dict(payload),
                    headers=self._headers,
                ) as response:
                    if response.status >= 400:
                        # Do not copy response bodies into logs or exceptions:
                        # Core responses can contain provider-specific content.
                        raise CoreMultimodalError(
                            f"Core multimodal request failed with status {response.status}"
                        )
                    body = await response.json(content_type=None)
        except CoreMultimodalError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise CoreMultimodalError(
                f"Core multimodal request unavailable: {type(exc).__name__}"
            ) from exc
        if not isinstance(body, Mapping):
            raise CoreMultimodalError("Core multimodal response was not an object")
        return body

    async def transcribe(
        self,
        wav_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
    ) -> Mapping[str, Any]:
        return await self._post(
            "/api/multimodal/perception",
            {
                "operation": "audio.transcribe.v1",
                "data": _encode_bytes(wav_bytes, max_bytes=MAX_AUDIO_BYTES),
                "media_format": "wav",
                "mime_type": "audio/wav",
                "metadata": {
                    "sample_rate": max(1, int(sample_rate)),
                    "channels": max(1, int(channels)),
                },
            },
        )

    async def describe_image(
        self,
        image_bytes: bytes,
        *,
        prompt: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        return await self._post(
            "/api/multimodal/perception",
            {
                "operation": "image.describe.v1",
                "data": _encode_bytes(image_bytes, max_bytes=MAX_IMAGE_BYTES),
                "media_format": "jpeg",
                "mime_type": "image/jpeg",
                "prompt": str(prompt or "")[:MAX_TEXT_CHARS],
                "metadata": dict(metadata or {}),
            },
        )

    async def synthesize_tts(
        self,
        text: str,
        *,
        platform: str = "bilibili.live",
        text_lang: Optional[str] = None,
    ) -> Mapping[str, Any]:
        text = str(text or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            raise ValueError("TTS text is empty")
        return await self._post(
            "/api/multimodal/tts",
            {
                "text": text,
                "platform": str(platform or "bilibili.live")[:64],
                "text_lang": text_lang,
            },
        )


__all__ = ["CoreMultimodalClient", "CoreMultimodalError"]
