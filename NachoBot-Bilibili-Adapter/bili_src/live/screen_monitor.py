"""Screen capture and Core-owned visual perception for Bilibili."""

from __future__ import annotations

import asyncio
import time
from io import BytesIO
from typing import Any, Iterable, List, Optional

from PIL import ImageGrab

from bili_src.core.config import ScreenVlmConfig
from bili_src.core.multimodal_client import CoreMultimodalClient
from bili_src.core.utils import _normalize_text
from bili_src.live.active_window import (
    ActiveWindowInfo,
    WINDOWS_ACTIVE_WINDOW_SUPPORTED,
    get_active_window_info,
    normalise_executables,
)
from bili_src.visual_policy import BILIBILI_SCREEN_PROMPT


class ScreenMonitor:
    """Capture a bounded image and ask Core for ``image.describe.v1``."""

    def __init__(
        self,
        configs: Optional[List[Any]] = None,
        logger=None,
        profile: Optional[ScreenVlmConfig] = None,
        min_interval_seconds: int = 30,
        capture_active_window: bool = True,
        excluded_exes: Optional[Iterable[str]] = None,
        multimodal_client: Optional[CoreMultimodalClient] = None,
    ):
        # ``configs`` is retained only for constructor compatibility.  The
        # adapter no longer resolves model/provider credentials locally.
        self.configs = list(configs or [])
        self.logger = logger
        self.profile = profile or ScreenVlmConfig()
        self.min_interval_seconds = max(1, int(min_interval_seconds))
        self.capture_active_window = bool(capture_active_window)
        self.excluded_exes = normalise_executables(excluded_exes)
        self.multimodal_client = multimodal_client or CoreMultimodalClient()
        self._last_attempt = 0.0
        self._last_summary: Optional[str] = None
        self._last_window_info: Optional[ActiveWindowInfo] = None
        self._warned_non_windows = False
        self._lock = asyncio.Lock()

    @classmethod
    def from_single_config(
        cls,
        config: Any,
        logger,
        profile: Optional[ScreenVlmConfig] = None,
        min_interval_seconds: int = 15,
        capture_active_window: bool = True,
        excluded_exes: Optional[Iterable[str]] = None,
        multimodal_client: Optional[CoreMultimodalClient] = None,
    ) -> "ScreenMonitor":
        return cls(
            [config],
            logger,
            profile=profile,
            min_interval_seconds=min_interval_seconds,
            capture_active_window=capture_active_window,
            excluded_exes=excluded_exes,
            multimodal_client=multimodal_client,
        )

    def get_cached_summary(self) -> Optional[str]:
        return self._last_summary

    def get_cached_window_info(self) -> Optional[ActiveWindowInfo]:
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
                self.logger.warning("Screen analysis failed, fallback to previous summary")
                return self._last_summary
            return None

    async def _analyze_current_screen(self, message_text: str = "") -> Optional[str]:
        image_bytes = await asyncio.to_thread(self._grab_screen_image)
        if not image_bytes:
            return None
        return await self._call_vlm(image_bytes, message_text, self._last_window_info)

    def _grab_screen_image(self) -> Optional[bytes]:
        window_info = None
        if self.capture_active_window:
            window_info = get_active_window_info(self.excluded_exes)
            self._last_window_info = window_info
            if WINDOWS_ACTIVE_WINDOW_SUPPORTED and window_info is None:
                self.logger.debug("No capturable foreground window; skipping screen capture")
                return None
            if not WINDOWS_ACTIVE_WINDOW_SUPPORTED and not self._warned_non_windows:
                self.logger.warning(
                    "Active-window capture is only available on Windows; using the primary screen"
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
                self.logger.warning("Screen capture failed: {}", type(exc).__name__)
                return None
        except Exception:
            try:
                bbox = window_info.rect if window_info else None
                image = ImageGrab.grab(bbox=bbox, all_screens=bool(bbox))
            except Exception as exc:
                self.logger.warning(
                    "Fallback screen capture failed: %s", type(exc).__name__
                )
                return None

        max_dim = self.profile.max_image_dimension
        width, height = image.size
        if max(width, height) > max_dim:
            scale = max_dim / max(width, height)
            image = image.resize((int(width * scale), int(height * scale)))
        with BytesIO() as buffer:
            image.convert("RGB").save(
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
        """Delegate visual inference to Core's profile-aware facade."""
        danmu_text = _normalize_text(message_text or "")
        if self.profile.message_max_chars:
            danmu_text = danmu_text[: self.profile.message_max_chars]
        prompt_text = self._render_prompt(danmu_text, window_info)
        metadata = {
            "window_title": str(window_info.title if window_info else "未知")[:300],
            "window_executable": str(
                window_info.executable if window_info else "未知"
            )[:128],
            "window_class": str(window_info.window_class if window_info else "未知")[:128],
        }
        try:
            response = await self.multimodal_client.describe_image(
                image_bytes,
                prompt=prompt_text,
                metadata=metadata,
            )
            text = str(response.get("text") or "").strip()
            return text or None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                "Core screen perception failed: error_type={}", type(exc).__name__
            )
            return None

    def _render_prompt(
        self,
        message_text: str,
        window_info: Optional[ActiveWindowInfo],
    ) -> str:
        values = {
            "window_title": window_info.title if window_info else "未知",
            "window_executable": window_info.executable if window_info else "未知",
            "window_class": window_info.window_class if window_info else "未知",
            "message_text": message_text or "（无）",
        }
        prompt = BILIBILI_SCREEN_PROMPT
        for key, value in values.items():
            prompt = prompt.replace(f"{{{key}}}", value)
        return prompt.strip()
