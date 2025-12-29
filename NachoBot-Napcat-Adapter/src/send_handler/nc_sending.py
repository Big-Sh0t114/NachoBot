import json
import uuid
import websockets as Server
from maim_message import MessageBase

from src.response_pool import get_response
from src.logger import logger
from src.recv_handler.message_sending import message_send_instance

class NCMessageSender:
    def __init__(self):
        self.server_connection: Server.ServerConnection = None
    
    async def set_server_connection(self, connection: Server.ServerConnection):
        self.server_connection = connection
    
    async def send_message_to_napcat(self, action: str, params: dict) -> dict:
        request_uuid = str(uuid.uuid4())
        payload = json.dumps({"action": action, "params": params, "echo": request_uuid})
        await self.server_connection.send(payload)
        try:
            response = await get_response(request_uuid)
        except TimeoutError:
            logger.error("发送消息超时，未收到响应")
            return {"status": "error", "message": "timeout"}
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
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