from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, ClientWebSocketResponse, WSMsgType, WSServerHandshakeError
from ncnk_message.offline_recovery import build_recovery_metadata

from .config import global_config
from .log_safety import safe_exception, sanitize_text, short_echo, summarize_payload
from .logger import logger

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
StatusCallback = Callable[[bool, str], Awaitable[None]]
ProgressCallback = Callable[[dict[str, Any], bool], Awaitable[None]]


class SnowLumaClient:
    """SnowLuma OneBot-style WebSocket client.

    Protocol details intentionally mirror the upstream SnowLuma adapter:
    actions are ``{action, params, echo}``; action results are matched by echo;
    events are normal OneBot-ish payloads carrying post_type/message/notice.
    """

    def __init__(
        self,
        event_callback: EventCallback,
        status_callback: StatusCallback,
        progress_callback: ProgressCallback | None = None,
    ):
        self.event_callback = event_callback
        self.status_callback = status_callback
        self.progress_callback = progress_callback
        self._session: ClientSession | None = None
        self._ws: ClientWebSocketResponse | None = None
        self._response_pool: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._event_recovery_contexts: dict[asyncio.Task[None], tuple[dict[str, Any], dict[str, Any]]] = {}
        self._progress_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._stop_event = asyncio.Event()
        self._connected_account_id = ""
        self._identity_verified = False
        self._connection_generation = 0
        self._identity_changed = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self.connection_epoch_started_at: float | None = None
        self.last_connection_epoch_started_at: float | None = None
        self.source_instance = uuid4().hex
        self._recovery_contexts: dict[int, dict[str, Any]] = {}

    @property
    def connected_account_id(self) -> str:
        return self._connected_account_id

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    def connection_snapshot(self) -> tuple[ClientWebSocketResponse, int, str] | None:
        """Return an immutable identity for actions tied to the verified socket."""
        ws = self._ws
        if ws is None or ws.closed or not self._identity_verified or not self._connected_account_id:
            return None
        return ws, self._connection_generation, self._connected_account_id

    def connection_is_current(self, snapshot: tuple[Any, ...]) -> bool:
        if len(snapshot) < 2:
            return False
        ws, generation = snapshot[0], snapshot[1]
        account_id = str(snapshot[2]) if len(snapshot) > 2 else ""
        return (
            self._ws is ws
            and self._connection_generation == generation
            and ws is not None
            and not ws.closed
            and (not account_id or self._connected_account_id == account_id)
        )

    async def wait_for_verified_connection(self, snapshot: tuple[Any, ...] | None) -> str | None:
        """Wait off-reader for login verification, rejecting replaced sockets."""
        if snapshot is None or len(snapshot) < 2:
            return None
        while True:
            if not self.connection_is_current(snapshot):
                return None
            if self._identity_verified and self._connected_account_id:
                return self._connected_account_id
            changed = self._identity_changed
            if not self.connection_is_current(snapshot):
                return None
            if self._identity_verified and self._connected_account_id:
                return self._connected_account_id
            await changed.wait()

    def _signal_identity_change(self) -> None:
        previous = self._identity_changed
        self._identity_changed = asyncio.Event()
        previous.set()

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
                # Establish the replay boundary before the listener can receive
                # its first packet from SnowLuma.
                self.connection_epoch_started_at = time.time()
                self.last_connection_epoch_started_at = self.connection_epoch_started_at
                captured_ws = self._ws
                captured_generation = self._connection_generation
                listen_task = asyncio.create_task(self._listen(), name="snowluma-listen")
                verify_task = asyncio.create_task(
                    self.call_action(
                        "get_login_info",
                        {},
                        expected_connection=(captured_ws, captured_generation),
                    ),
                    name="snowluma-verify",
                )
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
                if not account_id.isdecimal() or int(account_id) <= 0:
                    raise RuntimeError("SnowLuma login verification returned an invalid account ID")
                if not self.connection_is_current((captured_ws, captured_generation)):
                    raise RuntimeError("SnowLuma socket generation changed during login verification")
                self._connected_account_id = str(int(account_id))
                self._identity_verified = True
                self._signal_identity_change()
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
        self._connection_generation += 1
        self._signal_identity_change()
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
        self._identity_verified = False
        self._connection_generation += 1
        self._signal_identity_change()
        self.connection_epoch_started_at = None
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
        captured_connection = (ws, self._connection_generation)
        async for ws_message in ws:
            if ws_message.type == WSMsgType.TEXT:
                received_at = time.time()
                progress_id = uuid4().hex
                logger.debug("SnowLuma inbound frame type=text bytes={}", len(ws_message.data.encode("utf-8", "replace")))
                await self._handle_text_payload(
                    ws_message.data,
                    received_at=received_at,
                    progress_id=progress_id,
                    captured_connection=captured_connection,
                )
            elif ws_message.type == WSMsgType.BINARY:
                logger.warning("SnowLuma inbound frame ignored type=binary bytes={}", len(ws_message.data or b""))
            elif ws_message.type in {WSMsgType.CLOSED, WSMsgType.ERROR, WSMsgType.CLOSE}:
                logger.warning("SnowLuma inbound frame ended type={} close_code={}", ws_message.type.name, getattr(ws, "close_code", None))
                break
            else:
                logger.debug("SnowLuma inbound frame ignored type={}", ws_message.type.name)

    async def _handle_text_payload(
        self,
        raw_payload: str,
        *,
        received_at: float | None = None,
        progress_id: str | None = None,
        captured_connection: tuple[Any, ...] | None = None,
    ) -> None:
        received_at = time.time() if received_at is None else received_at
        progress_id = progress_id or uuid4().hex
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
        if self_id and self._identity_verified and self_id != self._connected_account_id:
            logger.warning(
                "SnowLuma event rejected reason=account_mismatch verified_account_id={}",
                self._connected_account_id,
            )
            return
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
            ordinary_message = post_type == "message" or (not post_type and "message" in payload)
            recovery_context = None
            if ordinary_message and self.progress_callback is not None:
                recovery_context = self.capture_offline_recovery_context(
                    payload,
                    received_at=received_at,
                    progress_id=progress_id,
                    captured_connection=captured_connection,
                )
                self._recovery_contexts[id(payload)] = recovery_context
                try:
                    # The progress start must reach Core before parallel event
                    # conversion is scheduled, so a slow media lookup cannot
                    # let the Core batch flush early.
                    await self.send_offline_recovery_progress(recovery_context, active=True)
                    self._track_event_callback(payload, post_type or "message", recovery_context)
                except BaseException:
                    self._recovery_contexts.pop(id(payload), None)
                    await self.finish_offline_recovery_progress(recovery_context)
                    raise
            else:
                self._track_event_callback(payload, post_type or "message")
        else:
            logger.debug("SnowLuma inbound event ignored reason=unsupported_post_type post_type={}", post_type or "missing")

    def capture_offline_recovery_context(
        self,
        payload: dict[str, Any],
        *,
        received_at: float | None = None,
        progress_id: str | None = None,
        captured_connection: tuple[Any, ...] | None = None,
    ) -> dict[str, Any]:
        """Capture the transport timestamp and routing identity outside raw event data."""
        captured_at = time.time() if received_at is None else received_at
        epoch = self.connection_epoch_started_at or captured_at
        progress_id = progress_id or uuid4().hex
        metadata = build_recovery_metadata(payload, epoch, captured_at, progress_id)
        metadata["source_instance"] = self.source_instance
        sender = payload.get("sender")
        sender = sender if isinstance(sender, Mapping) else {}
        raw_user_id = sender.get("user_id") or payload.get("user_id")
        user_id = self._canonical_route_id(raw_user_id)
        raw_group_id = payload.get("group_id") if payload.get("message_type") == "group" else None
        group_id = self._canonical_route_id(raw_group_id)
        return {
            "metadata": metadata,
            "platform": global_config.nachobot.platform_name,
            "group_id": group_id,
            "user_id": user_id,
            "connection": captured_connection,
        }

    @staticmethod
    def _canonical_route_id(value: Any) -> str | None:
        try:
            normalized = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return str(normalized) if normalized != 0 else None

    def take_offline_recovery_context(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Read adapter-owned capture data without altering raw event or lifecycle state."""
        return self._recovery_contexts.get(id(payload))

    async def send_offline_recovery_progress(self, context: dict[str, Any], *, active: bool) -> None:
        if self.progress_callback is None:
            return
        try:
            await self.progress_callback(context, active)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "SnowLuma offline recovery progress handoff failed progress_id={} active={} error={}",
                context["metadata"]["progress_id"],
                active,
                safe_exception(exc),
            )

    async def finish_offline_recovery_progress(self, context: dict[str, Any]) -> None:
        finish_task = asyncio.create_task(self.send_offline_recovery_progress(context, active=False))
        try:
            await asyncio.shield(finish_task)
        except asyncio.CancelledError:
            try:
                await finish_task
            except Exception as exc:
                logger.warning(
                    "SnowLuma offline recovery progress cleanup failed progress_id={} error={}",
                    context["metadata"]["progress_id"],
                    safe_exception(exc),
                )
            raise

    def _track_event_callback(
        self,
        payload: dict[str, Any],
        post_type: str,
        recovery_context: dict[str, Any] | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._run_event_callback(payload, recovery_context),
            name=f"snowluma-event-{post_type}",
        )
        self._event_tasks.add(task)
        if recovery_context is not None:
            self._event_recovery_contexts[task] = (payload, recovery_context)
        task.add_done_callback(self._event_task_done)
        logger.debug("SnowLuma event callback scheduled post_type={} pending_callbacks={}", post_type, len(self._event_tasks))

    async def _run_event_callback(
        self,
        payload: dict[str, Any],
        recovery_context: dict[str, Any] | None,
    ) -> None:
        try:
            await self.event_callback(payload)
        finally:
            if recovery_context is not None:
                await self._complete_recovery_callback(payload, recovery_context)

    async def _complete_recovery_callback(
        self,
        payload: dict[str, Any],
        recovery_context: dict[str, Any],
    ) -> None:
        # The done callback also handles tasks cancelled before their coroutine
        # gets its first execution step. Pop synchronously so the two paths can
        # never emit duplicate inactive markers.
        if self._recovery_contexts.pop(id(payload), None) is None:
            return
        current_task = asyncio.current_task()
        if current_task is not None:
            self._event_recovery_contexts.pop(current_task, None)
        await self.finish_offline_recovery_progress(recovery_context)

    def _schedule_recovery_finish(self, recovery_context: dict[str, Any]) -> None:
        cleanup_task = asyncio.create_task(self.finish_offline_recovery_progress(recovery_context))
        self._progress_cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._progress_cleanup_done)

    def _progress_cleanup_done(self, task: asyncio.Task[None]) -> None:
        self._progress_cleanup_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.warning("SnowLuma cancelled callback progress cleanup failed error={}", safe_exception(error))

    def _event_task_done(self, task: asyncio.Task[None]) -> None:
        self._event_tasks.discard(task)
        recovery = self._event_recovery_contexts.pop(task, None)
        if task.cancelled():
            logger.warning("SnowLuma event callback cancelled pending_callbacks={}", len(self._event_tasks))
            if recovery is not None:
                payload, recovery_context = recovery
                if self._recovery_contexts.pop(id(payload), None) is not None:
                    self._schedule_recovery_finish(recovery_context)
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
        if tasks:
            logger.debug("SnowLuma draining event callbacks count={}", len(tasks))
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("SnowLuma event callback drain timed out count={}", len(tasks))
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        # A callback cancelled before its first step is closed by its task's
        # done callback, which schedules the final inactive marker here.
        await asyncio.sleep(0)
        cleanup_tasks = tuple(self._progress_cleanup_tasks)
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    async def call_action(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout_sec: float | None = None,
        expected_connection: tuple[Any, ...] | None = None,
        pre_send_check: Callable[[], Awaitable[bool] | bool] | None = None,
    ) -> dict[str, Any]:
        ws = expected_connection[0] if expected_connection else self._ws
        generation = (
            expected_connection[1]
            if expected_connection and len(expected_connection) > 1
            else self._connection_generation
        )
        expected_account = str(expected_connection[2]) if expected_connection and len(expected_connection) > 2 else ""
        if ws is None or ws.closed:
            logger.warning("SnowLuma action rejected action={} reason=websocket_not_connected", action)
            raise RuntimeError("SnowLuma WebSocket 尚未连接")
        if self._ws is not ws or self._connection_generation != generation:
            raise RuntimeError("SnowLuma action connection generation is stale")
        if expected_account and self._connected_account_id != expected_account:
            raise RuntimeError("SnowLuma action account identity is stale")
        echo = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._response_pool[echo] = future
        payload = {"action": action, "params": params, "echo": echo}
        started = time.monotonic()
        timeout_value = max(
            1.0,
            float(global_config.snowluma.action_timeout_sec if timeout_sec is None else timeout_sec),
        )
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
                if self._ws is not ws or self._connection_generation != generation or ws.closed:
                    raise RuntimeError("SnowLuma action connection changed before send")
                if expected_account and self._connected_account_id != expected_account:
                    raise RuntimeError("SnowLuma action account identity changed before send")
                if pre_send_check is not None:
                    try:
                        checked = pre_send_check()
                        if inspect.isawaitable(checked):
                            checked = await asyncio.wait_for(checked, timeout=min(timeout_value, 4.0))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise RuntimeError("SnowLuma action pre-send check failed") from None
                    if checked is not True:
                        raise RuntimeError("SnowLuma action pre-send check refused")
                    # The check may await Control HTTP and adapter admission;
                    # make socket/account identity the final fence before send.
                    if self._ws is not ws or self._connection_generation != generation or ws.closed:
                        raise RuntimeError("SnowLuma action connection changed before send")
                    if expected_account and self._connected_account_id != expected_account:
                        raise RuntimeError("SnowLuma action account identity changed before send")
                await ws.send_str(json.dumps(payload, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=timeout_value)
            if self._ws is not ws or self._connection_generation != generation or ws.closed:
                raise RuntimeError("SnowLuma action connection changed before response")
            if expected_account and self._connected_account_id != expected_account:
                raise RuntimeError("SnowLuma action account identity changed before response")
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
                timeout_value,
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
            if not future.done():
                future.cancel()

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
