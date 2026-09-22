"""Thin Live2D lifecycle and normalized-control facade for Bilibili."""

from __future__ import annotations

from typing import Any

from bili_src.live2d.remote_controller import (
    PreparedReplyResult,
    RemoteLive2DController,
)


class Live2DManager:
    """Own the remote connection without parsing platform reply metadata."""

    def __init__(
        self,
        config: Any,
        logger,
        adapter_ref: Any = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.adapter = adapter_ref
        self.controller: RemoteLive2DController | None = None
        self._fallback_logged = False

        if self.config.live_live2d_enable:
            try:
                self.controller = RemoteLive2DController(adapter_ref, logger)
            except Exception as exc:
                self.logger.error("Failed to initialize remote Live2D controller: {}", exc)

    async def start(self) -> None:
        if self.controller:
            await self.controller.start()

    async def stop(self) -> None:
        if self.controller:
            await self.controller.stop()

    async def prepare_reply(self, raw_reply: str) -> PreparedReplyResult:
        """Return one normalized result; avatar metadata stays remote/opaque."""

        if self.controller is None:
            if not self._fallback_logged:
                self._fallback_logged = True
                self.logger.warning(
                    "Live2D prepare capability unavailable; using plain-text fallback"
                )
            return RemoteLive2DController._fallback_prepare_reply(raw_reply)
        return await self.controller.prepare_reply(raw_reply)

    async def apply_control(self, control_id: str | None) -> bool:
        if self.controller is None:
            return False
        return await self.controller.apply_control(control_id)


__all__ = ["Live2DManager"]
