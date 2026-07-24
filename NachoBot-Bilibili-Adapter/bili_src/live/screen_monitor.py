"""Screen monitor with VLM analysis for Bilibili Adapter."""

import asyncio
import base64
from loguru import logger
import time
from io import BytesIO
from typing import Iterable, List, Optional

import aiohttp
from PIL import ImageGrab

from bili_src.core.config import VlmModelConfig
from bili_src.core.utils import _normalize_text
from bili_src.live.active_window import (
    ActiveWindowInfo,
    WINDOWS_ACTIVE_WINDOW_SUPPORTED,
    get_active_window_info,
    normalise_executables,
)


class ScreenMonitor:
    def __init__(
        self,
        configs: List[VlmModelConfig],
        logger,
        min_interval_seconds: int = 30,
        capture_active_window: bool = True,
        excluded_exes: Optional[Iterable[str]] = None,
    ):
        self.configs = configs
        self.logger = logger
        self.min_interval_seconds = max(1, int(min_interval_seconds))
        self.capture_active_window = bool(capture_active_window)
        self.excluded_exes = normalise_executables(excluded_exes)
        self._last_attempt = 0.0
        self._last_summary: Optional[str] = None
        self._last_window_info: Optional[ActiveWindowInfo] = None
        self._warned_non_windows = False
        self._lock = asyncio.Lock()
        self._disabled_models = set()

    # Backward-compatible: accept single config as well
    @classmethod
    def from_single_config(
        cls,
        config: VlmModelConfig,
        logger,
        min_interval_seconds: int = 15,
        capture_active_window: bool = True,
        excluded_exes: Optional[Iterable[str]] = None,
    ) -> "ScreenMonitor":
        return cls(
            [config],
            logger,
            min_interval_seconds,
            capture_active_window,
            excluded_exes,
        )

    def get_cached_summary(self) -> Optional[str]:
        """Return the last cached screen summary without triggering VLM analysis."""
        return self._last_summary

    def get_cached_window_info(self) -> Optional[ActiveWindowInfo]:
        """Return metadata for the window used by the most recent capture."""
        return self._last_window_info

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
        return await self._call_vlm(
            image_bytes,
            message_text,
            self._last_window_info,
        )

    def _grab_screen_image(self) -> Optional[bytes]:
        window_info = None
        if self.capture_active_window:
            window_info = get_active_window_info(self.excluded_exes)
            self._last_window_info = window_info
            if WINDOWS_ACTIVE_WINDOW_SUPPORTED and window_info is None:
                self.logger.debug(
                    "No capturable foreground window; skipping this screen capture"
                )
                return None
            if not WINDOWS_ACTIVE_WINDOW_SUPPORTED and not self._warned_non_windows:
                self.logger.warning(
                    "Active-window capture is only available on Windows; "
                    "using the primary screen"
                )
                self._warned_non_windows = True

        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                if window_info is not None:
                    left, top, right, bottom = window_info.rect
                    monitor = {
                        "left": left,
                        "top": top,
                        "width": right - left,
                        "height": bottom - top,
                    }
                else:
                    monitor = sct.monitors[1]

                sct_img = sct.grab(monitor)
                image = Image.frombytes(
                    "RGB", sct_img.size, sct_img.bgra, "raw", "BGRX"
                )
        except ImportError:
            self.logger.warning("mss not found, falling back to ImageGrab")
            try:
                bbox = window_info.rect if window_info else None
                image = ImageGrab.grab(bbox=bbox, all_screens=bool(bbox))
            except Exception as exc:
                self.logger.warning("Screen capture failed: %s", exc)
                return None
        except Exception as exc:
            self.logger.warning(
                "mss capture failed: %s, falling back to ImageGrab", exc
            )
            try:
                bbox = window_info.rect if window_info else None
                image = ImageGrab.grab(bbox=bbox, all_screens=bool(bbox))
            except Exception as inner_exc:
                self.logger.warning("Fallback screen capture failed: %s", inner_exc)
                return None

        max_dim = 1280
        width, height = image.size
        if max(width, height) > max_dim:
            scale = max_dim / max(width, height)
            image = image.resize(
                (int(width * scale), int(height * scale))
            )

        with BytesIO() as buffer:
            image = image.convert("RGB")
            image.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()

    async def _call_vlm(
        self,
        image_bytes: bytes,
        message_text: str = "",
        window_info: Optional[ActiveWindowInfo] = None,
    ) -> Optional[str]:
        """Try each VLM config in order with per-model retry, return first success."""
        if not self.configs:
            self.logger.warning("No VLM configs available")
            return None

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        danmu_text = _normalize_text(message_text or "")
        prompt_text = (
            "你将收到当前活动窗口的截图和当前观众弹幕。先判断截图中是否存在与弹幕相关或需要特别注意的部分，"
            "再用300到500字尽可能详细描述活动窗口内容、文字和界面状态。"
            "只输出纯文本，不要使用markdown。"
        )
        if window_info:
            prompt_text += (
                f" 当前活动窗口标题：{window_info.title}；"
                f"进程：{window_info.executable}；窗口类：{window_info.window_class}。"
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
