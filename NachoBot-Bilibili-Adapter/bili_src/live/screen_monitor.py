"""Screen monitor with VLM analysis for Bilibili Adapter."""

import asyncio
import base64
import time
from io import BytesIO
from typing import Iterable, List, Optional

import aiohttp
from PIL import ImageGrab

from bili_src.core.config import ScreenVlmConfig, VlmModelConfig
from bili_src.core.utils import _normalize_text
from bili_src.live.active_window import (
    ActiveWindowInfo,
    WINDOWS_ACTIVE_WINDOW_SUPPORTED,
    get_active_window_info,
    normalise_executables,
)
from bili_src.visual_policy import (
    BILIBILI_SCREEN_PROMPT,
    BILIBILI_SCREEN_SYSTEM_PROMPT,
)


class ScreenMonitor:
    def __init__(
        self,
        configs: List[VlmModelConfig],
        logger,
        profile: Optional[ScreenVlmConfig] = None,
        min_interval_seconds: int = 30,
        capture_active_window: bool = True,
        excluded_exes: Optional[Iterable[str]] = None,
    ):
        self.configs = configs
        self.logger = logger
        self.profile = profile or ScreenVlmConfig()
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
        profile: Optional[ScreenVlmConfig] = None,
        min_interval_seconds: int = 15,
        capture_active_window: bool = True,
        excluded_exes: Optional[Iterable[str]] = None,
    ) -> "ScreenMonitor":
        return cls(
            [config],
            logger,
            profile=profile,
            min_interval_seconds=min_interval_seconds,
            capture_active_window=capture_active_window,
            excluded_exes=excluded_exes,
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
                self.logger.warning("Screen capture failed: {}", exc)
                return None
        except Exception as exc:
            self.logger.warning(
                "mss capture failed: {}, falling back to ImageGrab", exc
            )
            try:
                bbox = window_info.rect if window_info else None
                image = ImageGrab.grab(bbox=bbox, all_screens=bool(bbox))
            except Exception as inner_exc:
                self.logger.warning(
                    "Fallback screen capture failed: {}",
                    inner_exc,
                )
                return None

        max_dim = self.profile.max_image_dimension
        width, height = image.size
        if max(width, height) > max_dim:
            scale = max_dim / max(width, height)
            image = image.resize(
                (int(width * scale), int(height * scale))
            )

        with BytesIO() as buffer:
            image = image.convert("RGB")
            image.save(
                buffer,
                format="JPEG",
                quality=self.profile.jpeg_quality,
                optimize=True,
            )
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
        if self.profile.message_max_chars:
            danmu_text = danmu_text[: self.profile.message_max_chars]
        prompt_text = self._render_prompt(danmu_text, window_info)

        last_error: Optional[str] = None

        for idx, config in enumerate(self.configs):
            if config.model in getattr(self, "_disabled_models", set()):
                continue

            if config.client_type != "openai":
                self.logger.warning(
                    "Unsupported VLM client_type: {} (model: {}), skipping",
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
            "All {} VLM model(s) failed. Last failed model: {}",
            len(self.configs),
            last_error,
        )
        return None

    def _render_prompt(
        self,
        message_text: str,
        window_info: Optional[ActiveWindowInfo],
    ) -> str:
        """Render only the placeholders owned by this adapter use case."""
        values = {
            "window_title": window_info.title if window_info else "未知",
            "window_executable": (
                window_info.executable if window_info else "未知"
            ),
            "window_class": (
                window_info.window_class if window_info else "未知"
            ),
            "message_text": message_text or "（无）",
        }
        prompt = BILIBILI_SCREEN_PROMPT
        for key, value in values.items():
            prompt = prompt.replace(f"{{{key}}}", value)
        return prompt.strip()

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
                    "content": BILIBILI_SCREEN_SYSTEM_PROMPT,
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
        reserved_keys = {"model", "messages", "max_tokens", "temperature"}
        for key, value in config.extra_params.items():
            if key not in reserved_keys:
                payload[key] = value
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        timeout = aiohttp.ClientTimeout(total=max(1, config.timeout))

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
                                "VLM model [{}] '{}' transient error: "
                                "status={} body={}. Retries left: {}",
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
                                "VLM model [{}] '{}' hard error: "
                                "status={} body={}. Failing over.",
                                model_index,
                                config.model,
                                resp.status,
                                body[:200],
                            )
                            return None  # Hard error → try next model
                        data = await resp.json(content_type=None)
            except getattr(aiohttp, "ClientConnectorError", Exception) as exc:
                self.logger.warning(
                    "VLM model [{}] '{}' connection failed: {}. "
                    "Blacklisting model.",
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
                    "VLM model [{}] '{}' request error: {}. Retries left: {}",
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
                    "VLM model [{}] '{}' unexpected error: {}. Failing over.",
                    model_index,
                    config.model,
                    exc,
                )
                return None

            # Parse response
            choices = data.get("choices") if isinstance(data, dict) else None
            if not choices:
                self.logger.warning(
                    "VLM model [{}] '{}' response missing choices",
                    model_index,
                    config.model,
                )
                return None
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not content:
                self.logger.warning(
                    "VLM model [{}] '{}' response missing content",
                    model_index,
                    config.model,
                )
                return None

            self.logger.info(
                "VLM model [{}] '{}' succeeded",
                model_index,
                config.model,
            )
            return str(content).strip()

        # All retries exhausted for this model
        self.logger.warning(
            "VLM model [{}] '{}' exhausted all {} retries. Failing over.",
            model_index,
            config.model,
            config.max_retry,
        )
        return None
