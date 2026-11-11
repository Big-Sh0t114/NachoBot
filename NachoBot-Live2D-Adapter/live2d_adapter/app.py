"""Lifecycle for the independent Live2D window and optional WebSocket server."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger as default_logger

from .config import StandaloneConfig
from .runtime import AvatarRuntime
from .server import AvatarWebSocketServer


class StandaloneApplication:
    """Run a native Live2D renderer with no DesktopPet or Core startup path."""

    def __init__(
        self,
        config: StandaloneConfig,
        *,
        duration: float | None = None,
        no_server: bool = False,
        logger: Any = default_logger,
        runtime_factory=AvatarRuntime,
        server_factory=AvatarWebSocketServer,
    ) -> None:
        self.config = config
        self.duration = duration
        self.no_server = no_server
        self.logger = logger
        self.runtime = runtime_factory(config, logger)
        self.server = server_factory(config, self.runtime, logger) if not no_server else None
        self._window_closed = asyncio.Event()

    async def run(self) -> int:
        if self.server is not None:
            await self.server.run(duration=self.duration)
            return 0

        self.runtime.set_shutdown_sink(self._signal_window_closed)
        try:
            await self.runtime.start()
            if self.duration is None:
                await self._window_closed.wait()
            else:
                try:
                    await asyncio.wait_for(self._window_closed.wait(), timeout=self.duration)
                except TimeoutError:
                    pass
        finally:
            await self.runtime.stop()
        return 0

    async def _signal_window_closed(self) -> None:
        self._window_closed.set()

