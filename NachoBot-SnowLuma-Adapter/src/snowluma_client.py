from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, ClientWebSocketResponse, WSMsgType, WSServerHandshakeError

from .config import global_config
from .log_safety import safe_exception, sanitize_text, short_echo, summarize_payload
from .logger import logger

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
StatusCallback = Callable[[bool, str], Awaitable[None]]


class SnowLumaClient:
    """SnowLuma OneBot-style WebSocket client.

    Protocol details intentionally mirror the upstream SnowLuma adapter:
    actions are ``{action, params, echo}``; action results are matched by echo;
    events are normal OneBot-ish payloads carrying post_type/message/notice.
    """

    def __init__(self, event_callback: EventCallback, status_callback: StatusCallback):
        self.event_callback = event_callback
        self.status_callback = status_callback
        self._session: ClientSession | None = None
        self._ws: ClientWebSocketResponse | None = None
        self._response_pool: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._stop_event = asyncio.Event()
        self._connected_account_id = ""
        self._send_lock = asyncio.Lock()

    @property
    def connected_account_id(self) -> str:
        return self._connected_account_id

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def run(self) -> None:
        cfg = global_config.snowluma
        reconnect_attempt = 0
        logger.info(
            "SnowLuma client starting endpoint={} token_configured={} heartbeat_sec={} action_timeout_sec={}",
            cfg.safe_ws_url(),
            bool(cfg.token),
            cfg.heartbeat_sec,
            cfg.action_timeout_sec,
        )
        while not self._stop_event.is_set():
            listen_task: asyncio.Task[None] | None = None
            verify_task: asyncio.Task[dict[str, Any]] | None = None
            try:
                await self._connect()
                listen_task = asyncio.create_task(self._listen(), name="snowluma-listen")
                verify_task = asyncio.create_task(self.call_action("get_login_info", {}), name="snowluma-verify")
                done, _ = await asyncio.wait({listen_task, verify_task}, return_when=asyncio.FIRST_COMPLETED)
                if listen_task in done and not verify_task.done():
                    verify_task.cancel()
                    raise RuntimeError("SnowLuma connection closed during verification")
                response = await verify_task
                error = self.action_error(response)
                if error:
                    raise RuntimeError(f"SnowLuma verification failed: {error}")
                data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
                account_id = str(data.get("user_id") or data.get("self_id") or "").strip()
                if account_id:
                    self._connected_account_id = account_id
                reconnect_attempt = 0
                logger.info(
                    "SnowLuma WebSocket connected endpoint={} account_id={} verified=true",
                    cfg.safe_ws_url(),
                    self._connected_account_id or "unknown",
                )
                await self._status(True)
                await listen_task
                logger.warning(
                    "SnowLuma WebSocket listen ended close_code={} account_id={}",
                    getattr(self._ws, "close_code", None),
                    self._connected_account_id or "unknown",
                )
            except asyncio.CancelledError:
                raise
            except WSServerHandshakeError as exc:
                if exc.status in {401, 403}:
                    logger.error("SnowLuma WebSocket handshake rejected status={} reason=authentication", exc.status)
                else:
                    logger.warning(
                        "SnowLuma WebSocket handshake failed status={} error={}",
                        exc.status,
                        safe_exception(exc),
                    )
            except Exception as exc:
                logger.warning("SnowLuma connection cycle failed error={}", safe_exception(exc))
            finally:
                if verify_task is not None and not verify_task.done():
                    verify_task.cancel()
                    try:
                        await verify_task
                    except asyncio.CancelledError:
                        pass
                if listen_task is not None and not listen_task.done():
                    listen_task.cancel()
                    try:
                        await listen_task
                    except asyncio.CancelledError:
                        pass
                await self._status(False)
                await self._disconnect(reason="connection_cycle_end")

            if not self._stop_event.is_set():
                reconnect_attempt += 1
                delay = max(1.0, cfg.reconnect_delay_sec)
                logger.warning(
                    "SnowLuma reconnect scheduled attempt={} backoff_sec={}",
                    reconnect_attempt,
                    delay,
                )
                await asyncio.sleep(delay)
        logger.info("SnowLuma client stopped")

    async def stop(self) -> None:
        logger.info("SnowLuma stop requested connected={} account_id={}", self.connected, self._connected_account_id or "unknown")
        self._stop_event.set()
        await self._disconnect(reason="stop")
        await self._drain_event_tasks()

    async def _connect(self) -> None:
        cfg = global_config.snowluma
        logger.info(
            "SnowLuma WebSocket connect attempt endpoint={} heartbeat_sec={}",
            cfg.safe_ws_url(),
            cfg.heartbeat_sec,
        )
        timeout = ClientTimeout(total=10)
        self._session = ClientSession(timeout=timeout)
        try:
            self._ws = await self._session.ws_connect(
                cfg.ws_url(),
                heartbeat=max(0.0, cfg.heartbeat_sec) or None,
                max_msg_size=2**26,
            )
        except Exception:
            logger.warning("SnowLuma WebSocket connect failed endpoint={}", cfg.safe_ws_url())
            raise
        logger.debug("SnowLuma WebSocket handshake succeeded endpoint={}", cfg.safe_ws_url())

    async def _disconnect(self, *, reason: str = "manual") -> None:
        ws, session = self._ws, self._session
        self._ws = None
        self._session = None
        if ws is not None and not ws.closed:
            logger.info("SnowLuma WebSocket disconnect reason={} close_code={}", reason, getattr(ws, "close_code", None))
            await ws.close()
        if session is not None and not session.closed:
            await session.close()
        for future in self._response_pool.values():
            if not future.done():
                future.cancel()
        pending_actions = len(self._response_pool)
        self._response_pool.clear()
        self._connected_account_id = ""
        if pending_actions:
            logger.warning("SnowLuma disconnected with pending_actions={} reason={}", pending_actions, reason)

    async def _status(self, online: bool) -> None:
        try:
            await self.status_callback(online, self._connected_account_id)
            logger.debug(
                "SnowLuma platform status reported online={} account_id={}",
                online,
                self._connected_account_id or "unknown",
            )
        except Exception as exc:
            logger.warning("SnowLuma platform status callback failed online={} error={}", online, safe_exception(exc))

    async def _listen(self) -> None:
        ws = self._ws
        if ws is None:
            logger.warning("SnowLuma listen skipped reason=websocket_missing")
            return
        async for ws_message in ws:
            if ws_message.type == WSMsgType.TEXT:
                logger.debug("SnowLuma inbound frame type=text bytes={}", len(ws_message.data.encode("utf-8", "replace")))
                await self._handle_text_payload(ws_message.data)
            elif ws_message.type == WSMsgType.BINARY:
                logger.warning("SnowLuma inbound frame ignored type=binary bytes={}", len(ws_message.data or b""))
            elif ws_message.type in {WSMsgType.CLOSED, WSMsgType.ERROR, WSMsgType.CLOSE}:
                logger.warning("SnowLuma inbound frame ended type={} close_code={}", ws_message.type.name, getattr(ws, "close_code", None))
                break
            else:
                logger.debug("SnowLuma inbound frame ignored type={}", ws_message.type.name)

    async def _handle_text_payload(self, raw_payload: str) -> None:
        if global_config.debug.raw_payload:
            logger.trace("SnowLuma <= {}", summarize_payload(raw_payload))
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            logger.warning("SnowLuma inbound frame rejected reason=invalid_json error={}", safe_exception(exc))
            return
        if not isinstance(payload, dict):
            logger.warning("SnowLuma inbound frame ignored reason=top_level_not_object type={}", type(payload).__name__)
            return

        echo = str(payload.get("echo") or "").strip()
        if echo:
            future = self._response_pool.pop(echo, None)
            status = str(payload.get("status") or "")
            retcode = payload.get("retcode")
            if future is not None and not future.done():
                future.set_result(payload)
                logger.debug(
                    "SnowLuma action response matched echo={} status={} retcode={}",
                    short_echo(echo),
                    status or "unknown",
                    retcode,
                )
            else:
                logger.warning("SnowLuma action response ignored echo={} reason=unknown_echo", short_echo(echo))
            return

        self_id = str(payload.get("self_id") or "").strip()
        if self_id:
            self._connected_account_id = self_id
        post_type = str(payload.get("post_type") or "").strip()
        message_type = str(payload.get("message_type") or "").strip()
        notice_type = str(payload.get("notice_type") or "").strip()
        notice_sub_type = str(payload.get("sub_type") or "").strip()
        sender = payload.get("sender")
        sender_id = sender.get("user_id") if isinstance(sender, Mapping) else payload.get("user_id")
        logger.debug(
            "SnowLuma inbound event post_type={} message_type={} notice_type={} sub_type={} user_id={} group_id={} message_id={} segments={}",
            post_type or "missing",
            message_type or "-",
            notice_type or "-",
            notice_sub_type or "-",
            sender_id or "-",
            payload.get("group_id") or "-",
            payload.get("message_id") or "-",
            len(payload.get("message")) if isinstance(payload.get("message"), list) else (1 if isinstance(payload.get("message"), str) else 0),
        )
        if post_type in {"message", "notice", "request", "meta_event"} or "message" in payload:
            self._track_event_callback(payload, post_type or "message")
        else:
            logger.debug("SnowLuma inbound event ignored reason=unsupported_post_type post_type={}", post_type or "missing")

    def _track_event_callback(self, payload: dict[str, Any], post_type: str) -> None:
        task = asyncio.create_task(self.event_callback(payload), name=f"snowluma-event-{post_type}")
        self._event_tasks.add(task)
        task.add_done_callback(self._event_task_done)
        logger.debug("SnowLuma event callback scheduled post_type={} pending_callbacks={}", post_type, len(self._event_tasks))

    def _event_task_done(self, task: asyncio.Task[None]) -> None:
        self._event_tasks.discard(task)
        if task.cancelled():
            logger.warning("SnowLuma event callback cancelled pending_callbacks={}", len(self._event_tasks))
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("SnowLuma event callback failed error={}", safe_exception(error))
        else:
            logger.debug("SnowLuma event callback completed pending_callbacks={}", len(self._event_tasks))

    async def _drain_event_tasks(self) -> None:
        tasks = tuple(self._event_tasks)
        if not tasks:
            return
        logger.debug("SnowLuma draining event callbacks count={}", len(tasks))
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("SnowLuma event callback drain timed out count={}", len(tasks))
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def call_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        ws = self._ws
        if ws is None or ws.closed:
            logger.warning("SnowLuma action rejected action={} reason=websocket_not_connected", action)
            raise RuntimeError("SnowLuma WebSocket 尚未连接")
        echo = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._response_pool[echo] = future
        payload = {"action": action, "params": params, "echo": echo}
        started = time.monotonic()
        logger.debug(
            "SnowLuma action start action={} echo={} param_keys={}",
            action,
            short_echo(echo),
            ",".join(sorted(str(key) for key in params)) or "-",
        )
        if global_config.debug.raw_outbound:
            logger.trace("SnowLuma => {}", summarize_payload(payload))
        try:
            async with self._send_lock:
                await ws.send_str(json.dumps(payload, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=max(1.0, global_config.snowluma.action_timeout_sec))
            error = self.action_error(response)
            duration_ms = int((time.monotonic() - started) * 1000)
            if error:
                logger.warning(
                    "SnowLuma action response action={} echo={} result=error status={} retcode={} duration_ms={} error={}",
                    action,
                    short_echo(echo),
                    response.get("status") or "unknown",
                    response.get("retcode") if isinstance(response, Mapping) else "-",
                    duration_ms,
                    error,
                )
            else:
                logger.debug(
                    "SnowLuma action response action={} echo={} result=ok status={} retcode={} duration_ms={}",
                    action,
                    short_echo(echo),
                    response.get("status") or "unknown",
                    response.get("retcode") if isinstance(response, Mapping) else "-",
                    duration_ms,
                )
            return response
        except asyncio.TimeoutError as exc:
            logger.warning(
                "SnowLuma action timeout action={} echo={} timeout_sec={}",
                action,
                short_echo(echo),
                max(1.0, global_config.snowluma.action_timeout_sec),
            )
            raise TimeoutError(f"SnowLuma action {action} 响应超时") from exc
        except asyncio.CancelledError:
            logger.warning("SnowLuma action cancelled action={} echo={}", action, short_echo(echo))
            raise
        except Exception as exc:
            logger.error(
                "SnowLuma action failed action={} echo={} error={}",
                action,
                short_echo(echo),
                safe_exception(exc),
            )
            raise
        finally:
            self._response_pool.pop(echo, None)

    @staticmethod
    def action_error(response: Mapping[str, Any]) -> str:
        status = str(response.get("status") or "").strip().lower()
        retcode = response.get("retcode")
        if status and status != "ok":
            return sanitize_text(response.get("wording") or response.get("message") or status)
        if isinstance(retcode, int) and retcode not in {0, 1}:
            return sanitize_text(response.get("wording") or response.get("message") or f"retcode={retcode}")
        return ""

    @staticmethod
    def _compact(text: str, limit: int = 1800) -> str:
        """Backward-compatible bounded raw formatter for local callers."""

        return summarize_payload(text, max_length=limit)
