import asyncio
import json
import uuid
import random
import websockets as Server
from ncnk_message import MessageBase

from src.response_pool import get_response
from src.logger import logger
from src.recv_handler.message_sending import message_send_instance


class NCMessageSender:
    def __init__(self):
        self.server_connection: Server.ServerConnection = None
        self._send_queue = asyncio.Queue()  # 全局消息队列
        self._worker_task = None

    async def set_server_connection(self, connection: Server.ServerConnection):
        self.server_connection = connection
        # 当连接建立时，启动或重启发送队列的消费者任务
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker())
            logger.info("已启动 NapCat 消息发送全局队列 Worker")

    async def _queue_worker(self):
        """后台任务：不断从队列取出消息发送，强制进行安全间隔休眠"""
        while True:
            try:
                payload, request_uuid, send_future, expected_connection, pre_send_check, identity_check = await self._send_queue.get()
                try:
                    if send_future.cancelled():
                        continue
                    if expected_connection is None or self.server_connection is not expected_connection:
                        logger.debug("跳过已过期NapCat请求 UUID={}", request_uuid)
                        if not send_future.done():
                            send_future.set_result({"status": "error", "message": "stale_connection"})
                        continue
                    if pre_send_check is not None:
                        try:
                            allowed = await asyncio.wait_for(pre_send_check(), timeout=5.0)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            allowed = False
                        if allowed is not True:
                            if not send_future.done():
                                send_future.set_result({"status": "error", "message": "pre_send_check_failed"})
                            continue
                        if send_future.cancelled():
                            continue
                        try:
                            identity_current = identity_check is None or identity_check() is True
                        except Exception:
                            identity_current = False
                        if not identity_current or self.server_connection is not expected_connection:
                            if not send_future.done():
                                send_future.set_result({"status": "error", "message": "stale_connection"})
                            continue
                    logger.debug(f"[Queue] 执行发送任务: {request_uuid}")
                    await expected_connection.send(payload)
                    if not send_future.done():
                        send_future.set_result({"status": "ok"})
                    delay = 1.5 + random.uniform(0.5, 1.5)
                    logger.debug(f"[Queue] 发送完毕，休眠 {delay:.2f} 秒以规避风控...")
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    if not send_future.done():
                        send_future.set_result({"status": "error", "message": "cancelled"})
                    raise
                except Exception as e:
                    if not send_future.done():
                        send_future.set_result({"status": "error", "message": str(e)})
                    logger.error(f"[Queue] 发送 Worker 发生异常: {e}")
                    await asyncio.sleep(1)
                finally:
                    self._send_queue.task_done()
            except Exception as e:
                logger.error(f"[Queue] 发送 Worker 发生异常: {e}")
                await asyncio.sleep(1) # 出错也稍微等一下，避免死循环爆日志

    async def send_message_to_napcat(
        self,
        action: str,
        params: dict,
        *,
        timeout_sec: float | None = None,
        expected_connection: Server.ServerConnection | None = None,
        pre_send_check=None,
        identity_check=None,
    ) -> dict:
        request_uuid = str(uuid.uuid4())
        payload = json.dumps({"action": action, "params": params, "echo": request_uuid})
        connection = expected_connection or self.server_connection
        if connection is None:
            return {"status": "error", "message": "no_connection"}

        # 创建一个 Future 用于等待 NapCat 的 echo 返回
        loop = asyncio.get_running_loop()
        send_future = loop.create_future()

        # 将任务塞入队列
        await self._send_queue.put((payload, request_uuid, send_future, connection, pre_send_check, identity_check))

        timeout_value = 10.0 if timeout_sec is None else max(1.0, float(timeout_sec))

        # 等待 get_response 的返回（由底层的 ws 接收循环触发 response_pool 里的事件）
        try:
            send_result = await asyncio.wait_for(asyncio.shield(send_future), timeout=timeout_value)
            if not isinstance(send_result, dict) or send_result.get("status") != "ok":
                return send_result if isinstance(send_result, dict) else {"status": "error", "message": "send_failed"}
            # Worker 只负责推送并卡住节奏，接收回执仍由 src.response_pool 处理。
            response = await get_response(request_uuid, timeout=timeout_value)
        except TimeoutError:
            if not send_future.done():
                send_future.cancel()
            logger.error(
                f"发送消息超时，未收到响应 UUID: {request_uuid}, timeout_sec={timeout_value}"
            )
            return {"status": "error", "message": "timeout"}
        except asyncio.CancelledError:
            if not send_future.done():
                send_future.cancel()
            raise
        except Exception as e:
            logger.error(f"等待消息回执失败: {e}")
            return {"status": "error", "message": str(e)}

        return response

    async def message_sent_back(self, message_base: MessageBase, qq_message_id: str) -> None:
        # # 修改 additional_config，添加 echo 字段
        # if message_base.message_info.additional_config is None:
        #     message_base.message_info.additional_config = {}

        # message_base.message_info.additional_config["echo"] = True

        # # 获取原始的 mmc_message_id
        # mmc_message_id = message_base.message_info.message_id

        # # 修改 message_segment 为 notify 类型
        # message_base.message_segment = Seg(
        #     type="notify", data={"sub_type": "echo", "echo": mmc_message_id, "actual_id": qq_message_id}
        # )
        # await message_send_instance.message_send(message_base)
        # logger.debug("已回送消息ID")
        # return
        platform = message_base.message_info.platform
        mmc_message_id = message_base.message_info.message_id
        echo_data = {
            "type": "echo",
            "echo": mmc_message_id,
            "actual_id": qq_message_id,
        }
        success = await message_send_instance.send_custom_message(echo_data, platform, "message_id_echo")
        if success:
            logger.debug("已回送消息ID")
        else:
            logger.error("回送消息ID失败")


nc_message_sender = NCMessageSender()
