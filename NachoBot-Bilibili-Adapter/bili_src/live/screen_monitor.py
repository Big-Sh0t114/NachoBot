"""Screen monitor with VLM analysis for Bilibili Adapter."""

import asyncio
import base64
import logging
import time
from io import BytesIO
from typing import List, Optional

import aiohttp
from PIL import ImageGrab

from bili_src.core.config import VlmModelConfig
from bili_src.core.utils import _normalize_text


class ScreenMonitor:
    def __init__(
        self,
        configs: List[VlmModelConfig],
        logger: logging.Logger,
        min_interval_seconds: int = 30,
    ):
        self.configs = configs
        self.logger = logger
        self.min_interval_seconds = max(1, int(min_interval_seconds))
        self._last_attempt = 0.0
        self._last_summary: Optional[str] = None
        self._lock = asyncio.Lock()
        self._disabled_models = set()

    # Backward-compatible: accept single config as well
    @classmethod
    def from_single_config(
        cls,
        config: VlmModelConfig,
        logger: logging.Logger,
        min_interval_seconds: int = 15,
    ) -> "ScreenMonitor":
        return cls([config], logger, min_interval_seconds)

    def get_cached_summary(self) -> Optional[str]:
        """Return the last cached screen summary without triggering VLM analysis."""
        return self._last_summary

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
                self.logger.warning(
                    "Screen analysis failed, fallback to previous summary"
                )
                return self._last_summary
            return None

    async def _analyze_current_screen(self, message_text: str = "") -> Optional[str]:
        image_bytes = await asyncio.to_thread(self._grab_screen_image)
        if not image_bytes:
            return None
        return await self._call_vlm(image_bytes, message_text)

    def _grab_screen_image(self) -> Optional[bytes]:
        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                # Get the primary monitor
                monitor = sct.monitors[1]
                sct_img = sct.grab(monitor)
                # Convert to PIL Image
                image = Image.frombytes(
                    "RGB", sct_img.size, sct_img.bgra, "raw", "BGRX"
                )
        except ImportError:
            self.logger.warning("mss not found, falling back to ImageGrab")
            try:
                image = ImageGrab.grab(all_screens=False)
            except Exception as exc:
                self.logger.warning("Screen capture failed: %s", exc)
                return None
        except Exception as exc:
            self.logger.warning(
                "mss capture failed: %s, falling back to ImageGrab", exc
            )
            try:
                image = ImageGrab.grab(all_screens=False)
            except Exception as inner_exc:
                self.logger.warning("Fallback screen capture failed: %s", inner_exc)
                return None

        # Resize if too large
        max_dim = 1280
        w, h = image.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            new_w = int(w * scale)
            new_h = int(h * scale)
            image = image.resize((new_w, new_h))

        with BytesIO() as buffer:
            image = image.convert("RGB")
            image.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()

    async def _call_vlm(
        self, image_bytes: bytes, message_text: str = ""
    ) -> Optional[str]:
        """Try each VLM config in order with per-model retry, return first success."""
        if not self.configs:
            self.logger.warning("No VLM configs available")
            return None

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        danmu_text = _normalize_text(message_text or "")
        prompt_text = (
            "你将收到一张直播截图和当前观众弹幕。先判断截图中是否存在与弹幕相关或需要特别注意的部分，"
            "再用300到500字尽可能详细描述屏幕内容，包含主窗口细节与活动窗口标题，并简要概括其他区域。"
            "只输出纯文本，不要使用markdown。"
        )
        if danmu_text:
            prompt_text += f" 弹幕内容：{danmu_text}"

        last_error: Optional[str] = None

        for idx, config in enumerate(self.configs):
            if config.model in getattr(self, "_disabled_models", set()):
                continue

            if config.client_type != "openai":
                self.logger.warning(
                    "Unsupported VLM client_type: %s (model: %s), skipping",
                    config.client_type,
                    config.model,
                )
                continue

            result = await self._attempt_single_vlm(
                config, image_b64, prompt_text, idx
            )
            if result is not None:
                return result
            last_error = config.model

        self.logger.warning(
            "All %d VLM model(s) failed. Last failed model: %s",
            len(self.configs),
            last_error,
        )
        return None

    async def _attempt_single_vlm(
        self,
        config: VlmModelConfig,
        image_b64: str,
        prompt_text: str,
        model_index: int,
    ) -> Optional[str]:
        """Attempt a VLM request with retry logic for a single model config."""
        url = f"{config.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": config.model,
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
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                        {"type": "text", "text": prompt_text},
                    ],
                },
            ],
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        timeout = aiohttp.ClientTimeout(total=max(5, config.timeout))

        retries_left = max(1, config.max_retry)

        while retries_left > 0:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload, headers=headers) as resp:
                        if resp.status >= 500 or resp.status == 429:
                            # Transient error, retry
                            body = await resp.text()
                            retries_left -= 1
                            self.logger.warning(
                                "VLM model [%d] '%s' transient error: status=%s body=%s. Retries left: %d",
                                model_index,
                                config.model,
                                resp.status,
                                body[:200],
                                retries_left,
                            )
                            if retries_left > 0:
                                await asyncio.sleep(config.retry_interval)
                            continue
                        if resp.status >= 400:
                            body = await resp.text()
                            self.logger.warning(
                                "VLM model [%d] '%s' hard error: status=%s body=%s. Failing over.",
                                model_index,
                                config.model,
                                resp.status,
                                body[:200],
                            )
                            return None  # Hard error → try next model
                        data = await resp.json(content_type=None)
            except getattr(aiohttp, "ClientConnectorError", Exception) as exc:
                self.logger.warning(
                    "VLM model [%d] '%s' connection failed: %s. Blacklisting model.",
                    model_index,
                    config.model,
                    exc,
                )
                if hasattr(self, "_disabled_models"):
                    self._disabled_models.add(config.model)
                return None
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                retries_left -= 1
                self.logger.warning(
                    "VLM model [%d] '%s' request error: %s. Retries left: %d",
                    model_index,
                    config.model,
                    exc,
                    retries_left,
                )
                if retries_left > 0:
                    await asyncio.sleep(config.retry_interval)
                continue
            except Exception as exc:
                self.logger.warning(
                    "VLM model [%d] '%s' unexpected error: %s. Failing over.",
                    model_index,
                    config.model,
                    exc,
                )
                return None

            # Parse response
            choices = data.get("choices") if isinstance(data, dict) else None
            if not choices:
                self.logger.warning(
                    "VLM model [%d] '%s' response missing choices",
                    model_index,
                    config.model,
                )
                return None
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not content:
                self.logger.warning(
                    "VLM model [%d] '%s' response missing content",
                    model_index,
                    config.model,
                )
                return None

            self.logger.info(
                "VLM model [%d] '%s' succeeded",
                model_index,
                config.model,
            )
            return str(content).strip()

        # All retries exhausted for this model
        self.logger.warning(
            "VLM model [%d] '%s' exhausted all %d retries. Failing over.",
            model_index,
            config.model,
            config.max_retry,
        )
        return None
