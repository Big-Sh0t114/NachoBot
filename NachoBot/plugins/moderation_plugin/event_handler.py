import re
import os
import io
import base64
import json
from typing import Optional, Tuple, Dict, Any

from PIL import Image

from src.plugin_system.base import BaseEventHandler, EventType, NachoMessages, CustomEventHandlerResult
from src.common.logger import get_logger

logger = get_logger("moderation_plugin")

# ---------------------------------------------------------------------------
# 感知哈希 (dHash) —— 对 JPEG 重压缩 / WebP 转码 / 轻微缩放 有鲁棒性
# ---------------------------------------------------------------------------


def _compute_dhash(image_bytes: bytes, hash_size: int = 16) -> int:
    """计算 dHash (差异哈希)。

    将图片缩放到 (hash_size+1) x hash_size 的灰度图，
    比较每行相邻像素的亮度差来生成一个整型指纹。

    Args:
        image_bytes: 图片原始字节
        hash_size: 哈希尺寸，越大越精确但也越慢。默认 16 -> 256-bit 指纹。

    Returns:
        int: 哈希指纹整数
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    img = img.resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())

    hash_val = 0
    for row in range(hash_size):
        for col in range(hash_size):
            offset = row * (hash_size + 1) + col
            if pixels[offset] < pixels[offset + 1]:
                hash_val |= 1 << (row * hash_size + col)
    return hash_val


def _hamming_distance(h1: int, h2: int) -> int:
    """计算两个整型哈希的汉明距离（不同的 bit 数）"""
    return bin(h1 ^ h2).count("1")


# ---------------------------------------------------------------------------
# 事件处理器
# ---------------------------------------------------------------------------


class ModerationEventHandler(BaseEventHandler):
    event_type = EventType.ON_MESSAGE
    handler_name = "moderation_event_handler"
    handler_description = "处理消息的违规词正则匹配及图片感知哈希比对"
    weight = 999  # 设置高权重，在其他处理前拦截
    intercept_message = True

    # dHash 汉明距离阈值：<=threshold 视为同一张图
    # 256-bit dHash 下，<=10 基本就是同源图
    DHASH_THRESHOLD = 15

    def __init__(self):
        super().__init__()
        # dHash -> filename
        self._banned_image_hashes: Dict[int, str] = {}
        self._hashes_loaded = False
        
        self.stats_file = "data/moderation_stats.json"
        self._stats: Dict[str, Dict[str, Any]] = {
            "text_patterns": {},
            "images": {}
        }
        self._load_stats()

    def _load_stats(self):
        try:
            if os.path.exists(self.stats_file):
                with open(self.stats_file, "r", encoding="utf-8") as f:
                    self._stats = json.load(f)
        except Exception as e:
            logger.error(f"读取撤回统计数据失败: {e}")

    def _save_stats(self):
        try:
            os.makedirs(os.path.dirname(self.stats_file), exist_ok=True)
            with open(self.stats_file, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存撤回统计数据失败: {e}")

    def _update_stats(self, category: str, item_key: str, user_id: str):
        """更新统计信息
        category: 'text_patterns' 或 'images'
        item_key: 命中的正则 或 图片文件名
        user_id: 触发者的 QQ 号
        """
        if category not in self._stats:
            self._stats[category] = {}
            
        if item_key not in self._stats[category]:
            self._stats[category][item_key] = {
                "count": 0,
                "triggered_by": {}
            }
            
        record = self._stats[category][item_key]
        record["count"] += 1
        
        user_id_str = str(user_id) if user_id else "unknown"
        if user_id_str not in record["triggered_by"]:
            record["triggered_by"][user_id_str] = 0
        record["triggered_by"][user_id_str] += 1
        
        self._save_stats()

    def _load_banned_image_hashes(self, directory: str):
        """从目录加载所有违规图片的 dHash 指纹"""
        if not os.path.isdir(directory):
            logger.warning(f"违规图片目录 {directory} 不存在，已跳过图片哈希比对初始化")
            self._hashes_loaded = True
            return

        count = 0
        for root, _, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "rb") as f:
                        file_bytes = f.read()
                    h = _compute_dhash(file_bytes)
                    self._banned_image_hashes[h] = file
                    count += 1
                except Exception as e:
                    logger.error(f"读取违规图片文件 {file_path} 失败: {e}")

        self._hashes_loaded = True
        logger.info(f"已加载 {count} 个违规图片感知哈希 (dHash)")

    def _is_banned_image(self, image_bytes: bytes) -> Tuple[bool, Optional[str]]:
        """判断图片是否命中违规图片库，返回 (是否命中, 命中文件名)"""
        try:
            h = _compute_dhash(image_bytes)
        except Exception as e:
            logger.error(f"计算 dHash 时出错: {e}")
            return False, None

        for banned_h, filename in self._banned_image_hashes.items():
            dist = _hamming_distance(h, banned_h)
            if dist <= self.DHASH_THRESHOLD:
                logger.debug(f"图片 dHash 匹配成功，汉明距离 = {dist}，匹配文件: {filename}")
                return True, filename
        return False, None

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
                    self._update_stats("text_patterns", pattern, user_id)
                    await self._do_recall(message)
                    return True, False, "违规拦截(正则)", None, None
            except re.error as e:
                logger.error(f"违规正则 [{pattern}] 无效: {e}")

        # 3. 检查图片哈希（感知哈希）
        if not self._hashes_loaded:
            banned_images_dir = self.get_config("moderation.banned_images_dir", "data/banned_images")
            self._load_banned_image_hashes(banned_images_dir)

        if self._banned_image_hashes:
            for seg in message.message_segments:
                seg_type = getattr(seg, "type", "")
                seg_data = getattr(seg, "data", None)
                if seg_type in ("image", "emoji") and isinstance(seg_data, str):
                    try:
                        image_bytes = base64.b64decode(seg_data)
                        is_banned, matched_filename = self._is_banned_image(image_bytes)
                        if is_banned:
                            logger.info(f"消息中的图片/表情命中违规感知哈希 ({matched_filename})，触发撤回")
                            self._update_stats("images", matched_filename, user_id)
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
