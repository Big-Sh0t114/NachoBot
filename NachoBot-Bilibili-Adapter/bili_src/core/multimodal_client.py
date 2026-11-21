"""Small HTTP facade for the Core multimodal API.

The Bilibili adapter deliberately knows only the wire contract here.  Model
selection, local-runtime routing, and perception fallbacks remain Core-owned.
This client is also used for non-chat idle speech so that idle playback never
has to manufacture a chat turn.
"""

from __future__ import annotations

import asyncio
import base64
import os
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
        token = self.token or os.environ.get("NACHOBOT_CORE_TOKEN", "")
        token = token.strip()
        if token[:7].casefold() == "bearer ":
            token = token[7:].strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
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

    async def _get(self, path: str, *, timeout_seconds: float = 5.0) -> Mapping[str, Any]:
        timeout = aiohttp.ClientTimeout(total=min(self.timeout, max(0.1, timeout_seconds)))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.base_url}{path}",
                    headers=self._headers,
                ) as response:
                    if response.status >= 400:
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
        task: str = "vlm",
    ) -> Mapping[str, Any]:
        return await self._post(
            "/api/multimodal/perception",
            {
                "operation": "image.describe.v1",
                "task": task,
                "data": _encode_bytes(image_bytes, max_bytes=MAX_IMAGE_BYTES),
                "media_format": "jpeg",
                "mime_type": "image/jpeg",
                "prompt": str(prompt or "")[:MAX_TEXT_CHARS],
                "metadata": dict(metadata or {}),
            },
        )

    async def describe_image_fast(
        self,
        image_bytes: bytes,
        *,
        prompt: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        return await self.describe_image(image_bytes, prompt=prompt, metadata=metadata, task="vlm_fast")

    async def synthesize_tts(
        self,
        text: str,
        *,
        platform: str = "bilibili.live",
        text_lang: Optional[str] = None,
        ambient_noise: bool = False,
        prepare_text: bool = False,
    ) -> Mapping[str, Any]:
        text = str(text or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            raise ValueError("TTS text is empty")
        payload = {
            "text": text,
            "platform": str(platform or "bilibili.live")[:64],
            "text_lang": text_lang,
        }
        if ambient_noise is True:
            payload["ambient_noise"] = True
        if type(prepare_text) is not bool:
            raise ValueError("prepare_text must be a bool")
        if prepare_text:
            payload["prepare_text"] = True
        return await self._post("/api/multimodal/tts", payload)

    async def get_tts_policy(self) -> Mapping[str, Any]:
        """Read Core's active TTS backend policy with a short bounded request."""
        return await self._get("/api/multimodal/tts/policy", timeout_seconds=5.0)


__all__ = ["CoreMultimodalClient", "CoreMultimodalError"]
