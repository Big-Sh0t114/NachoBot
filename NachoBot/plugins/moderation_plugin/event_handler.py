import re
import os
import hashlib
import base64
from typing import List, Optional, Tuple

from src.plugin_system.base import BaseEventHandler, EventType, NachoMessages, CustomEventHandlerResult
from src.common.logger import get_logger

logger = get_logger("moderation_plugin")


class ModerationEventHandler(BaseEventHandler):
    event_type = EventType.ON_MESSAGE
    handler_name = "moderation_event_handler"
    handler_description = "处理消息的违规词正则匹配及图片哈希比对"
    weight = 999  # 设置高权重，在其他处理前拦截
    intercept_message = True

    def __init__(self):
        super().__init__()
        self._banned_image_hashes = set()
        self._hashes_loaded = False

    def _load_banned_image_hashes(self, directory: str):
        if not os.path.isdir(directory):
            logger.warning(f"违规图片目录 {directory} 不存在，已跳过图片哈希比对初始化")
            self._hashes_loaded = True
            return

        for root, _, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "rb") as f:
                        file_bytes = f.read()
                        file_hash = hashlib.md5(file_bytes).hexdigest()
                        self._banned_image_hashes.add(file_hash)
                except Exception as e:
                    logger.error(f"读取违规图片文件 {file_path} 失败: {e}")
        self._hashes_loaded = True
        logger.info(f"已加载 {len(self._banned_image_hashes)} 个违规图片哈希")

    async def execute(
        self, message: Optional[NachoMessages]
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[NachoMessages]]:
        if not message:
            return True, True, None, None, None

        base_info = message.message_base_info or {}

        # 1. 检查白名单
        whitelist_qq = self.get_config("moderation.whitelist_qq", [])
        user_id = base_info.get("user_id")
        if user_id is not None and str(user_id) in [str(qq) for qq in whitelist_qq]:
            return True, True, "白名单用户，放行", None, None

        # 2. 检查违规词（正则）
        ban_regex_list = self.get_config("moderation.ban_regex_list", [])
        text_to_check = message.plain_text or message.raw_message or ""

        for pattern in ban_regex_list:
            try:
                if pattern and re.search(pattern, text_to_check):
                    logger.info(f"消息命中违规正则 [{pattern}]，触发撤回")
                    await self._do_recall(message)
                    return True, False, "违规拦截(正则)", None, None
            except re.error as e:
                logger.error(f"违规正则 [{pattern}] 无效: {e}")

        # 3. 检查图片哈希
        if not self._hashes_loaded:
            banned_images_dir = self.get_config("moderation.banned_images_dir", "data/banned_images")
            self._load_banned_image_hashes(banned_images_dir)

        if self._banned_image_hashes:
            for seg in message.message_segments:
                seg_type = getattr(seg, "type", "")
                seg_data = getattr(seg, "data", None)
                if seg_type in ("image", "emoji") and isinstance(seg_data, str):
                    try:
                        # segment.data 是 base64 编码的图片数据（由 NachoBot-Napcat-Adapter 提供）
                        image_bytes = base64.b64decode(seg_data)
                        image_hash = hashlib.md5(image_bytes).hexdigest()
                        if image_hash in self._banned_image_hashes:
                            logger.info(f"消息中的图片/表情命中违规哈希 [{image_hash}]，触发撤回")
                            await self._do_recall(message)
                            return True, False, "违规拦截(图片)", None, None
                    except Exception as e:
                        logger.error(f"处理图片哈希时出错: {e}")

        return True, True, None, None, None

    async def _do_recall(self, message: NachoMessages):
        base_info = message.message_base_info or {}
        stream_id = message.stream_id
        message_id = base_info.get("message_id")

        if not stream_id:
            logger.warning("缺少 stream_id，无法发送撤回指令")
            return
        if message_id is None:
            logger.warning("缺少 message_id，无法发送撤回指令")
            return

        logger.info(f"发送撤回指令: message_id={message_id}")
        await self.send_command(
            stream_id=stream_id,
            command_name="DELETE_MSG",
            command_args={"message_id": message_id},
            storage_message=False,
        )
        recall_msg = self.get_config("moderation.recall_message", "")
        if recall_msg:
            await self.send_text(stream_id, recall_msg, storage_message=False)
