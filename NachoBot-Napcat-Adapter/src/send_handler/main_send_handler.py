from typing import Any, Dict
from maim_message import (
    UserInfo,
    GroupInfo,
    Seg,
    BaseMessageInfo,
    MessageBase,
)
from src.logger import logger
from .send_command_handler import SendCommandHandleClass
from .send_message_handler import SendMessageHandleClass
from .nc_sending import nc_message_sender


class SendHandler:
    def __init__(self):
        pass

    async def handle_message(self, raw_message_base_dict: dict) -> None:
        raw_message_base: MessageBase = MessageBase.from_dict(raw_message_base_dict)
        message_segment: Seg = raw_message_base.message_segment
        logger.info("接收到来自MaiBot的消息，处理中")
        if message_segment.type == "command":
            return await self.send_command(raw_message_base)
        else:
            return await self.send_normal_message(raw_message_base)

    async def send_command(self, raw_message_base: MessageBase) -> None:
        """
        处理命令类
        """
        logger.info("处理命令中")
        message_info: BaseMessageInfo = raw_message_base.message_info
        message_segment: Seg = raw_message_base.message_segment
        group_info: GroupInfo = message_info.group_info
        seg_data: Dict[str, Any] = message_segment.data
        try:
            command, args_dict = SendCommandHandleClass.handle_command(seg_data, group_info)
        except Exception as e:
            logger.error(f"处理命令时出错: {str(e)}")
            return

        if not command or not args_dict:
            logger.error("命令或参数缺失")
            return None

        response = await nc_message_sender.send_message_to_napcat(command, args_dict)
        if response.get("status") == "ok":
            logger.info(f"命令 {seg_data.get('name')} 执行成功")
        else:
            logger.warning(f"命令 {seg_data.get('name')} 执行失败，napcat返回：{str(response)}")

    async def send_normal_message(self, raw_message_base: MessageBase) -> None:
        """
        处理普通消息发送
        """
        logger.info("处理普通信息中")
        message_info: BaseMessageInfo = raw_message_base.message_info
        message_segment: Seg = raw_message_base.message_segment
        group_info: GroupInfo = message_info.group_info
        user_info: UserInfo = message_info.user_info
        target_id: int = None
        action: str = None
        id_name: str = None
        processed_message: list = []
        try:
            processed_message = SendMessageHandleClass.process_seg_recursive(message_segment)
        except Exception as e:
            logger.error(f"处理消息时发生错误: {e}")
            return

        if not processed_message:
            logger.critical("现在暂时不支持解析此回复！")
            return None

        if group_info and user_info:
            logger.debug("发送群聊消息")
            target_id = group_info.group_id
            action = "send_group_msg"
            id_name = "group_id"
        elif user_info:
            logger.debug("发送私聊消息")
            target_id = user_info.user_id
            action = "send_private_msg"
            id_name = "user_id"
        else:
            logger.error("无法识别的消息类型")
            return
        logger.info("尝试发送到napcat")
        response = await nc_message_sender.send_message_to_napcat(
            action,
            {
                id_name: target_id,
                "message": processed_message,
            },
        )
        if response.get("status") == "ok":
            logger.info("消息发送成功")
            qq_message_id = response.get("data", {}).get("message_id")
            await nc_message_sender.message_sent_back(raw_message_base, qq_message_id)
        else:
            logger.warning(f"消息发送失败，napcat返回：{str(response)}")

send_handler = SendHandler()