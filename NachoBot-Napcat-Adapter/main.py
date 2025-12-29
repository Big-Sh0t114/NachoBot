import asyncio
import sys
import json
import http
import time
import websockets as Server
from src.logger import logger
from src.recv_handler.message_handler import message_handler
from src.recv_handler.meta_event_handler import meta_event_handler
from src.recv_handler.notice_handler import notice_handler
from src.recv_handler.message_sending import message_send_instance
from src.send_handler.nc_sending import nc_message_sender
from src.config import global_config
from src.mmc_com_layer import mmc_start_com, mmc_stop_com, router
from src.response_pool import put_response, check_timeout_response

message_queue = asyncio.Queue()
last_message_time = time.time()


async def message_recv(server_connection: Server.ServerConnection):
    global last_message_time
    await message_handler.set_server_connection(server_connection)
    asyncio.create_task(notice_handler.set_server_connection(server_connection))
    await nc_message_sender.set_server_connection(server_connection)
    asyncio.create_task(connection_watchdog(server_connection))
    async for raw_message in server_connection:
        last_message_time = time.time()
        try:
            logger.debug(f"{raw_message[:1500]}..." if (len(raw_message) > 1500) else raw_message)
            decoded_raw_message: dict = json.loads(raw_message)
            post_type = decoded_raw_message.get("post_type")
            if post_type in ["meta_event", "message", "notice"]:
                await message_queue.put(decoded_raw_message)
            elif post_type is None:
                await put_response(decoded_raw_message)
            else:
                logger.warning(f"未知的 post_type: {post_type}")
        except json.JSONDecodeError as exc:
            logger.error(f"JSON 解码失败，跳过本条消息: {exc}")
        except Exception as exc:
            logger.exception(f"接收消息处理异常，跳过本条消息: {exc}")


async def message_process():
    while True:
        message = await message_queue.get()
        try:
            post_type = message.get("post_type")
            if post_type == "message":
                await message_handler.handle_raw_message(message)
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
    message_send_instance.maibot_router = router
    _ = await asyncio.gather(napcat_server(), mmc_start_com(), message_process(), check_timeout_response())


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
    logger.info("正在启动adapter...")
    async with Server.serve(message_recv, global_config.napcat_server.host, global_config.napcat_server.port, max_size=2**26, process_request=check_napcat_server_token) as server:
        logger.info(
            f"Adapter已启动，监听地址: ws://{global_config.napcat_server.host}:{global_config.napcat_server.port}"
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
