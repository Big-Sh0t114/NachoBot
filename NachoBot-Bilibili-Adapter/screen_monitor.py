"""Screen monitor with VLM analysis for Bilibili Adapter."""

import asyncio
import base64
import logging
import time
from io import BytesIO
from typing import Optional

import aiohttp
from PIL import ImageGrab

from config import VlmModelConfig
from utils import _normalize_text


class ScreenMonitor:
    def __init__(
        self,
        config: VlmModelConfig,
        logger: logging.Logger,
        min_interval_seconds: int = 15,
    ):
        self.config = config
        self.logger = logger
        self.min_interval_seconds = max(1, int(min_interval_seconds))
        self._last_attempt = 0.0
        self._last_summary: Optional[str] = None
        self._lock = asyncio.Lock()

    async def maybe_analyze(self, message_text: str = "") -> Optional[str]:
        now = time.time()
        if now - self._last_attempt < self.min_interval_seconds:
            return self._last_summary
        if self._lock.locked():
            return self._last_summary
        async with self._lock:
            now = time.time()
            if now - self._last_attempt < self.min_interval_seconds:
                return self._last_summary
            self._last_attempt = now
            summary = await self._analyze_current_screen(message_text)
            if summary:
                self._last_summary = summary
                return summary
            if self._last_summary:
                self.logger.warning("Screen analysis failed, fallback to previous summary")
                return self._last_summary
            return None

    async def _analyze_current_screen(self, message_text: str = "") -> Optional[str]:
        image_bytes = await asyncio.to_thread(self._grab_screen_png)
        if not image_bytes:
            return None
        return await self._call_vlm(image_bytes, message_text)

    def _grab_screen_png(self) -> Optional[bytes]:
        try:
            image = ImageGrab.grab(all_screens=False)
        except Exception as exc:
            self.logger.warning("Screen capture failed: %s", exc)
            return None
        with BytesIO() as buffer:
            image.save(buffer, format="PNG")
            return buffer.getvalue()

    async def _call_vlm(self, image_bytes: bytes, message_text: str = "") -> Optional[str]:
        if self.config.client_type != "openai":
            self.logger.warning("Unsupported VLM client_type: %s", self.config.client_type)
            return None
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        danmu_text = _normalize_text(message_text or "")
        prompt_text = (
            "你将收到一张直播截图和当前观众弹幕。先判断截图中是否存在与弹幕相关或需要特别注意的部分，"
            "再用300到500字尽可能详细描述屏幕内容，包含主窗口细节与活动窗口标题，并简要概括其他区域。"
            "只输出纯文本，不要使用markdown。"
        )
        if danmu_text:
            prompt_text += f" 弹幕内容：{danmu_text}"
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个专业的图像分析助手。",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                        {"type": "text", "text": prompt_text},
                    ],
                },
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        timeout = aiohttp.ClientTimeout(total=max(5, self.config.timeout))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        self.logger.warning("VLM request failed: status=%s body=%s", resp.status, body)
                        return None
                    data = await resp.json(content_type=None)
        except Exception as exc:
            self.logger.warning("VLM request error: %s", exc)
            return None
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            self.logger.warning("VLM response missing choices")
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not content:
            self.logger.warning("VLM response missing content")
            return None
        return str(content).strip()
