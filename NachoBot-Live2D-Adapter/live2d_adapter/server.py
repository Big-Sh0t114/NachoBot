"""WebSocket transport for the standalone Live2D adapter."""

from __future__ import annotations

import asyncio
from loguru import logger
from collections.abc import Iterable
from urllib.parse import parse_qs, urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import WebSocketServerProtocol, serve

from .config import AdapterConfig
from .protocol import (
    AvatarCommand,
    AvatarEvent,
    AvatarInteraction,
    InteractionEvent,
    ProtocolError,
    error_event,
)
from .runtime import AvatarRuntime


MAX_WEBSOCKET_MESSAGE_BYTES = 8 * 1024 * 1024
class AvatarWebSocketServer:
    """Expose :class:`AvatarRuntime` through the versioned avatar protocol."""

    def __init__(
        self,
        config: AdapterConfig,
        runtime: AvatarRuntime,
        logger,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.logger = logger
        self._clients: set[WebSocketServerProtocol] = set()
        self._clients_lock = asyncio.Lock()
        self._server = None

    async def run(self) -> None:
        self.runtime.set_interaction_sink(self.broadcast)
        await self.runtime.start()

        host = self.config.server.host
        port = self.config.server.port
        self.logger.info("Live2D WebSocket server listening on ws://%s:%s", host, port)

        try:
            async with serve(
                self._handle_client,
                host,
                port,
                ping_interval=20,
                ping_timeout=20,
                max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
            ) as server:
                self._server = server
                await server.wait_closed()
        finally:
            self._server = None
            self.runtime.set_interaction_sink(None)
            await self._close_clients()
            await self.runtime.stop()

    async def stop(self) -> None:
        server = self._server
        if server is not None:
            server.close()
            await server.wait_closed()

    async def broadcast(self, event: InteractionEvent) -> None:
        message = event.to_json()
        async with self._clients_lock:
            clients = tuple(self._clients)

        if not clients:
            self.logger.debug(
                "No Live2D clients connected; interaction dropped: %s",
                event.event.value,
            )
            return

        results = await asyncio.gather(
            *(self._safe_send(client, message) for client in clients),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                self.logger.debug("Live2D interaction send failed: %s", result)

    async def _handle_client(
        self,
        websocket: WebSocketServerProtocol,
        path: str | None = None,
    ) -> None:
        request_path = self._resolve_request_path(websocket, path)
        if not self._is_authorized(request_path):
            self.logger.warning("Rejected unauthorized Live2D WebSocket client")
            await websocket.close(code=4401, reason="unauthorized")
            return

        async with self._clients_lock:
            self._clients.add(websocket)

        remote_address = getattr(websocket, "remote_address", None)
        self.logger.info("Live2D client connected: %s", remote_address)

        try:
            await websocket.send(
                InteractionEvent(
                    event=AvatarInteraction.READY,
                    payload={"running": self.runtime.is_running},
                ).to_json()
            )

            async for raw_message in websocket:
                if not isinstance(raw_message, str):
                    await websocket.send(error_event("binary messages are unsupported").to_json())
                    continue

                request_id: str | None = None
                try:
                    command = AvatarCommand.from_json(raw_message)
                    request_id = command.request_id
                    response = await self.runtime.dispatch(command)
                    if response is not None:
                        await websocket.send(response.to_json())
                    if command.event is AvatarEvent.SHUTDOWN:
                        asyncio.create_task(self.stop())
                        return
                except ProtocolError as exc:
                    await websocket.send(error_event(str(exc), request_id).to_json())
                except Exception as exc:
                    self.logger.exception("Unhandled Live2D command error")
                    await websocket.send(
                        error_event(f"internal adapter error: {exc}", request_id).to_json()
                    )
        except ConnectionClosed:
            pass
        finally:
            async with self._clients_lock:
                self._clients.discard(websocket)
            self.logger.info("Live2D client disconnected: %s", remote_address)

    def _is_authorized(self, request_path: str) -> bool:
        expected_token = self.config.server.token
        if not expected_token:
            return True
        query = parse_qs(urlsplit(request_path).query)
        supplied_token = query.get("token", [""])[0]
        return supplied_token == expected_token

    @staticmethod
    def _resolve_request_path(
        websocket: WebSocketServerProtocol,
        path: str | None,
    ) -> str:
        if path:
            return path

        legacy_path = getattr(websocket, "path", None)
        if isinstance(legacy_path, str):
            return legacy_path

        request = getattr(websocket, "request", None)
        request_path = getattr(request, "path", None)
        if isinstance(request_path, str):
            return request_path

        return "/"

    async def _safe_send(
        self,
        websocket: WebSocketServerProtocol,
        message: str,
    ) -> None:
        try:
            await websocket.send(message)
        except ConnectionClosed:
            async with self._clients_lock:
                self._clients.discard(websocket)

    async def _close_clients(self) -> None:
        async with self._clients_lock:
            clients = tuple(self._clients)
            self._clients.clear()

        await asyncio.gather(
            *(client.close(code=1001, reason="adapter shutting down") for client in clients),
            return_exceptions=True,
        )
