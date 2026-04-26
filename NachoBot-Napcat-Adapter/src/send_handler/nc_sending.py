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
                payload, request_uuid, future = await self._send_queue.get()
                
                if self.server_connection is None:
                    logger.error("尝试发送消息但尚未建立 WebSocket 连接")
                    future.set_result({"status": "error", "message": "no_connection"})
                    self._send_queue.task_done()
                    continue

                logger.debug(f"[Queue] 执行发送任务: {request_uuid}")
                await self.server_connection.send(payload)
                
                # 计算动态延迟：基础 1.5 秒 + 随机 0.5~1.5 秒
                # 如果上一个版本里有判断文字长度的逻辑，也可以加在这里
                delay = 1.5 + random.uniform(0.5, 1.5)
                logger.debug(f"[Queue] 发送完毕，休眠 {delay:.2f} 秒以规避风控...")
                await asyncio.sleep(delay)
                
                self._send_queue.task_done()
            except Exception as e:
                logger.error(f"[Queue] 发送 Worker 发生异常: {e}")
                await asyncio.sleep(1) # 出错也稍微等一下，避免死循环爆日志

    async def send_message_to_napcat(self, action: str, params: dict) -> dict:
        request_uuid = str(uuid.uuid4())
        payload = json.dumps({"action": action, "params": params, "echo": request_uuid})
        
        # 创建一个 Future 用于等待 NapCat 的 echo 返回
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        
        # 将任务塞入队列
        await self._send_queue.put((payload, request_uuid, future))
        
        # 等待 get_response 的返回（由底层的 ws 接收循环触发 response_pool 里的事件）
        try:
            # 这里我们依然依赖原有的 get_response 机制，
            # Worker 只负责推送并卡住节奏，接收回执还是靠你写好的 src.response_pool
            response = await get_response(request_uuid)
        except TimeoutError:
            logger.error(f"发送消息超时，未收到响应 UUID: {request_uuid}")
            return {"status": "error", "message": "timeout"}
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
