# ruff: noqa: E402
import asyncio
import sys
import json
import http
import time
from pathlib import Path

_NACHOBOT_PATH = Path(__file__).resolve().parents[1] / "NachoBot"
if (_NACHOBOT_PATH / "ncnk_message").is_dir():
    nachobot_path = str(_NACHOBOT_PATH)
    if nachobot_path not in sys.path:
        sys.path.insert(1, nachobot_path)

import websockets as Server
from src import version
from src.logger import logger
from src.recv_handler.message_handler import message_handler
from src.recv_handler.meta_event_handler import meta_event_handler
from src.recv_handler.notice_handler import notice_handler
from src.recv_handler.message_sending import message_send_instance
from src.send_handler.nc_sending import nc_message_sender
from src.config import global_config
from src.mmc_com_layer import mmc_start_com, mmc_stop_com, router
from src.offline_history import get_offline_history_runtime
from src.response_pool import put_response, check_timeout_response
from src.listen_address import resolve_listen_address
from qqcall_client.qqcall_service import QQCallPlatformBindingHeartbeat

logger.info(f"版本\n\nNachobot-Napcat-Adapter 版本: {version}\n喜欢的话点个star喵~\n")

message_queue = asyncio.Queue()
_connection_epoch_started_at = time.time()
_active_server_connection = None

nc_message_sender.configure_outbound_echo_tracking(
    lambda: (
        global_config.nachobot_server.platform_name,
        message_handler.connection_generation,
        message_handler.verified_account_id,
    )
)


def _connection_is_open(connection) -> bool:
    if connection is None:
        return False
    closed = getattr(connection, "closed", None)
    if isinstance(closed, bool):
        return not closed
    state = getattr(connection, "state", None)
    if state is not None:
        state_name = getattr(state, "name", None)
        if state_name is not None:
            return str(state_name).upper() == "OPEN"
        value = getattr(state, "value", state)
        if isinstance(value, int):
            return value == 1  # websockets.protocol.State.OPEN
        return str(state).lower().endswith("open")
    return True


def _offline_history_platform_state() -> dict:
    connection = _active_server_connection
    account_id = message_handler.verified_account_id
    return {
        "connected": bool(account_id and _connection_is_open(connection)),
        "account_id": account_id,
        "identity": (id(connection), _connection_epoch_started_at) if connection is not None else (),
        "connection": connection,
        "connection_epoch_started_at": _connection_epoch_started_at,
    }


def _qqcall_platform_state() -> dict:
    connection = _active_server_connection
    account_id = message_handler.verified_account_id
    generation = message_handler.connection_generation
    return {
        "connected": bool(
            connection is not None
            and connection is message_handler.server_connection
            and account_id
            and _connection_is_open(connection)
        ),
        "account_id": account_id if isinstance(account_id, str) else "",
        "identity": (id(connection), generation, account_id) if connection is not None else (),
        "connection_generation": generation,
    }


async def _verify_napcat_login(connection, epoch_started_at: float) -> None:
    """Verify account identity without blocking the reverse-WebSocket reader."""
    runtime = get_offline_history_runtime()
    connection_generation = message_handler.connection_generation
    while (
        _active_server_connection is connection
        and _connection_epoch_started_at == epoch_started_at
        and message_handler.connection_generation == connection_generation
    ):
        try:
            response = await nc_message_sender.send_message_to_napcat(
                "get_login_info", {}, timeout_sec=5.0, expected_connection=connection
            )
            data = response.get("data") if isinstance(response, dict) else None
            account_id = str(data.get("user_id") or "").strip() if isinstance(data, dict) else ""
            if response.get("status") == "ok" and account_id.isdecimal() and int(account_id) > 0:
                if (
                    _active_server_connection is not connection
                    or _connection_epoch_started_at != epoch_started_at
                    or message_handler.connection_generation != connection_generation
                ):
                    return
                # Persist the first-enable baseline now, before waiting for Core.
                if runtime.enabled:
                    await runtime.prepare_account(account_id)
                if (
                    _active_server_connection is not connection
                    or _connection_epoch_started_at != epoch_started_at
                    or message_handler.connection_generation != connection_generation
                ):
                    return
                if not message_handler.publish_verified_account_id(
                    account_id,
                    connection_generation=connection_generation,
                    expected_connection=connection,
                ):
                    return
                notify_platform_status(online=True)
                logger.info("NapCat登录身份已校验")
                return
            logger.warning("NapCat登录身份校验未成功，稍后在同一连接重试")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("NapCat登录身份校验失败 error={}", str(exc)[:240])
        await asyncio.sleep(2.0)


def notify_platform_status(online: bool):
    """向核心发送平台连接状态通知（轻量通知，不含监控逻辑）"""
    platform = global_config.nachobot_server.platform_name
    try:
        msg = {
            "platform": platform,
            "online": online,
            "timestamp": time.time(),
            "connection_epoch_started_at": _connection_epoch_started_at,
        }
        asyncio.ensure_future(
            message_send_instance.send_custom_message(
                custom_message=msg, platform=platform, message_type="platform_status"
            )
        )
        logger.info(f"已通知核心: 平台 {platform} {'在线' if online else '离线'}")
    except Exception as e:
        logger.debug(f"通知核心平台状态失败（非致命）: {e}")


def configure_offline_history_runtime():
    """Bind history to the production action, router, and ordinary ingress paths."""
    runtime = get_offline_history_runtime()

    async def send_custom(*, platform, message_type_name, message):
        return await message_send_instance.send_custom_message(
            custom_message=message,
            platform=platform,
            message_type=message_type_name,
        )

    async def emit_message(raw, context):
        return await message_handler.handle_raw_message(
            raw,
            recovery_context=context,
            manage_progress=False,
        )

    async def admission_check(user_id, group_id, **kwargs):
        return await message_handler.check_allow_to_chat(user_id, group_id, **kwargs)

    runtime.bind(
        call_action=nc_message_sender.send_message_to_napcat,
        send_custom=send_custom,
        emit_message=emit_message,
        admission_check=admission_check,
    )
    return runtime
last_message_time = time.time()


async def message_recv(server_connection: Server.ServerConnection):
    global last_message_time, _connection_epoch_started_at, _active_server_connection
    connection_epoch_started_at = time.time()
    _connection_epoch_started_at = connection_epoch_started_at
    _active_server_connection = server_connection
    message_handler.connection_epoch_started_at = connection_epoch_started_at
    message_handler.verified_account_id = None
    await message_handler.set_server_connection(
        server_connection,
        epoch_started_at=connection_epoch_started_at,
    )
    transport_connection_generation = message_handler.connection_generation
    asyncio.create_task(notice_handler.set_server_connection(server_connection))
    await nc_message_sender.set_server_connection(server_connection)
    asyncio.create_task(connection_watchdog(server_connection))
    asyncio.create_task(_verify_napcat_login(server_connection, connection_epoch_started_at))
    self_message_tasks: set[asyncio.Task] = set()

    def _self_message_done(task: asyncio.Task) -> None:
        self_message_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("NapCat message_sent callback failed error={}", str(error)[:240])

    try:
        async for raw_message in server_connection:
            last_message_time = time.time()
            received_at = last_message_time
            try:
                logger.debug(f"{raw_message[:1500]}..." if (len(raw_message) > 1500) else raw_message)
                decoded_raw_message: dict = json.loads(raw_message)
                post_type = decoded_raw_message.get("post_type")
                if post_type in ["meta_event", "message", "message_sent", "notice"]:
                    if post_type == "message":
                        recovery_context = message_handler.capture_offline_recovery_context(
                            decoded_raw_message,
                            connection_epoch_started_at=connection_epoch_started_at,
                            received_at=received_at,
                            transport_owned=True,
                        )
                        try:
                            await message_handler.send_offline_recovery_progress(recovery_context, active=True)
                            await message_queue.put((decoded_raw_message, recovery_context))
                        except BaseException:
                            await message_handler.finish_offline_recovery_progress(recovery_context)
                            raise
                    elif post_type == "message_sent":
                        context = {
                            "transport_owned": True,
                            "expected_connection": server_connection,
                            "metadata": {
                                "transport_connection_generation": transport_connection_generation,
                            },
                        }
                        # Self echoes can wait for the action response to publish
                        # its exact platform ID. Keep that wait off the websocket reader.
                        task = asyncio.create_task(
                            message_handler.handle_external_self_message(
                                decoded_raw_message,
                                transport_context=context,
                            ),
                            name="napcat-message-sent",
                        )
                        self_message_tasks.add(task)
                        task.add_done_callback(_self_message_done)
                    else:
                        await message_queue.put(decoded_raw_message)
                elif post_type is None:
                    await put_response(decoded_raw_message)
                else:
                    logger.warning(f"未知的 post_type: {post_type}")
            except json.JSONDecodeError as exc:
                logger.error(f"JSON 解码失败，跳过本条消息: {exc}")
            except Exception as exc:
                logger.exception(f"接收消息处理异常，跳过本条消息: {exc}")
    except Server.exceptions.ConnectionClosedError:
        logger.warning("与 Napcat 的 WebSocket 连接异常断开 (通常是 Napcat 超时重置)。已捕获异常，等待客户端重新连接...")
        notify_platform_status(online=False)
    except Server.exceptions.ConnectionClosedOK:
        logger.info("与 Napcat 的 WebSocket 连接已正常关闭。")
        notify_platform_status(online=False)
    except Exception as e:
        logger.error(f"message_recv 发生严重未处理异常: {e}")
        notify_platform_status(online=False)
    finally:
        for task in tuple(self_message_tasks):
            task.cancel()
        if self_message_tasks:
            await asyncio.gather(*tuple(self_message_tasks), return_exceptions=True)
        if _active_server_connection is server_connection:
            _active_server_connection = None
            message_handler.invalidate_server_connection(
                server_connection,
                connection_generation=transport_connection_generation,
            )


async def message_process():
    while True:
        queued_message = await message_queue.get()
        try:
            recovery_context = None
            if (
                isinstance(queued_message, tuple)
                and len(queued_message) == 2
                and isinstance(queued_message[0], dict)
                and isinstance(queued_message[1], dict)
            ):
                message, recovery_context = queued_message
            else:
                message = queued_message
            post_type = message.get("post_type")
            if post_type == "message":
                await message_handler.handle_raw_message(
                    message,
                    recovery_context=recovery_context,
                    progress_started=recovery_context is not None,
                )
            elif post_type == "meta_event":
                await meta_event_handler.handle_meta_event(message)
            elif post_type == "notice":
                await notice_handler.handle_notice(message)
            else:
                logger.warning(f"未知的post_type: {post_type}")
        except Exception as exc:
            logger.exception(f"消息处理异常: {exc}")
        finally:
            message_queue.task_done()
        await asyncio.sleep(0.05)


async def main():
    message_send_instance.nachobot_router = router
    offline_history = configure_offline_history_runtime()
    qqcall_config = global_config.qqcall.to_runtime_config()
    heartbeat = None
    if qqcall_config.platform_manifest_file:
        heartbeat = QQCallPlatformBindingHeartbeat(
            adapter="napcat",
            manifest_file=qqcall_config.platform_manifest_file,
            account_id=qqcall_config.account_id,
        )
    tasks = [
        napcat_server(),
        mmc_start_com(),
        message_process(),
        check_timeout_response(),
        offline_history.monitor(
            get_platform_state=_offline_history_platform_state,
            router=router,
            on_core_generation=lambda _account_id: notify_platform_status(online=True),
        ),
    ]
    if heartbeat is not None:
        tasks.append(heartbeat.run(_qqcall_platform_state))
    _ = await asyncio.gather(*tasks)


async def connection_watchdog(server_connection: Server.ServerConnection):
    """
    当长时间未收到Napcat消息时，主动断开连接以触发Napcat重连。
    解决静默断线但未触发重连的问题。
    """
    # 半小时内无任何消息则触发重连
    timeout_seconds = 30 * 60
    while True:
        await asyncio.sleep(min(global_config.napcat_server.heartbeat_interval, 60))
        # 兼容不同 websockets 版本的状态检测
        is_closed = False
        closed_attr = getattr(server_connection, "closed", None)
        if isinstance(closed_attr, bool):
            is_closed = closed_attr
        else:
            open_attr = getattr(server_connection, "open", None)
            if isinstance(open_attr, bool):
                is_closed = not open_attr
            else:
                state_attr = getattr(server_connection, "state", None)
                if state_attr is not None and str(state_attr).lower() == "closed":
                    is_closed = True
        if is_closed:
            logger.info("Napcat 连接已关闭，停止连接监控")
            break
        idle_time = time.time() - last_message_time
        if idle_time > timeout_seconds:
            logger.error(f"超过 {timeout_seconds}s 未收到 Napcat 消息，准备断开连接触发重连")
            try:
                await server_connection.close(code=1011, reason="No messages received for a long time")
            except Exception as exc:
                logger.exception(f"关闭 Napcat 连接失败: {exc}")
            break


def check_napcat_server_token(conn, request):
    token = global_config.napcat_server.token
    if not token or token.strip() == "":
        return None
    auth_header = request.headers.get("Authorization")
    if auth_header != f"Bearer {token}":
        return Server.Response(
            status=http.HTTPStatus.UNAUTHORIZED,
            headers=Server.Headers([("Content-Type", "text/plain")]),
            body=b"Unauthorized\n"
        )
    return None


async def napcat_server():
    listen_host, listen_port = resolve_listen_address(
        global_config.napcat_server.host,
        global_config.napcat_server.port,
    )
    logger.info("正在启动adapter...")
    async with Server.serve(message_recv, listen_host, listen_port, max_size=2**26, process_request=check_napcat_server_token) as server:
        logger.info(
            f"Adapter已启动，监听地址: ws://{listen_host}:{listen_port}"
        )
        await server.serve_forever()


async def graceful_shutdown():
    try:
        logger.info("正在关闭adapter...")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
        await get_offline_history_runtime().stop()
        await mmc_stop_com()  # 后置避免神秘exception
        logger.info("Adapter已成功关闭")
    except Exception as e:
        logger.error(f"Adapter关闭中出现错误: {e}")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.warning("收到中断信号，正在优雅关闭...")
        loop.run_until_complete(graceful_shutdown())
    except Exception as e:
        logger.exception(f"主程序异常: {str(e)}")
        sys.exit(1)
    finally:
        if loop and not loop.is_closed():
            loop.close()
        sys.exit(0)
