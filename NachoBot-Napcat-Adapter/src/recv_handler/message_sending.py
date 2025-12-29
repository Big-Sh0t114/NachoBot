from typing import Dict
from src.logger import logger
from maim_message import MessageBase, Router


class MessageSending:
    """
    负责把消息发送到麦麦
    """

    maibot_router: Router = None

    def __init__(self):
        pass

    async def message_send(self, message_base: MessageBase) -> bool:
        """
        发送消息
        Parameters:
            message_base: MessageBase: 消息基类，包含发送目标和消息内容等信息
        """
        try:
            send_status = await self.maibot_router.send_message(message_base)
            if not send_status:
                raise RuntimeError("可能是路由未正确配置或连接异常")
            return send_status
        except Exception as e:
            logger.error(f"发送消息失败: {str(e)}")
            logger.error("请检查与MaiBot之间的连接")
        
    async def send_custom_message(self, custom_message: Dict, platform: str, message_type: str) -> bool:
        """
        发送自定义消息
        """
        try:
            await self.maibot_router.send_custom_message(platform=platform, message_type_name=message_type, message=custom_message)
            return True
        except Exception as e:
            logger.error(f"发送自定义消息失败: {str(e)}")
            logger.error("请检查与MaiBot之间的连接")
            return False


message_send_instance = MessageSending()
