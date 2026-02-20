import base64
import os
import time
import hashlib
from typing import Optional

from src.common.logger import get_logger
from src.common.database.database import db
from src.common.database.database_model import ImageDescriptions
from src.config.config import model_config
from src.llm_models.utils_model import LLMRequest

logger = get_logger("chat_video")


class VideoManager:
    _instance = None
    VIDEO_DIR = "data"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            os.makedirs(os.path.join(self.VIDEO_DIR, "video"), exist_ok=True)
            self.vlm = LLMRequest(model_set=model_config.model_task_config.video, request_type="video")
            try:
                db.connect(reuse_if_open=True)
                db.create_tables([ImageDescriptions], safe=True)
            except Exception as e:
                logger.error(f"数据库连接或表创建失败: {e}")
            self._initialized = True

    async def _download_or_read_video(self, video_info: dict) -> Optional[bytes]:
        """下载或读取视频文件到内存字节串"""
        path = video_info.get("path")
        url = video_info.get("url")

        # 优先读取本地文件
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    return f.read()
            except Exception as e:
                logger.error(f"读取本地视频失败: {e}")

        # 然后再尝试下载
        if url:
            try:
                import httpx

                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, timeout=60)
                    if resp.status_code == 200:
                        return resp.content
            except Exception as e:
                logger.error(f"下载视频失败: {e}")
        return None

    async def process_video(self, video_info: dict) -> str:
        """从Napcat接收的video段结构进行处理获取视频内容描述"""
        try:
            video_bytes = await self._download_or_read_video(video_info)
            if not video_bytes:
                return "[视频(获取原始文件失败)]"

            # 使用 MD5 哈希去重缓存
            video_hash = hashlib.md5(video_bytes).hexdigest()

            # 检查是否有缓存
            record = ImageDescriptions.get_or_none(
                (ImageDescriptions.image_description_hash == video_hash) & (ImageDescriptions.type == "video")
            )
            if record and record.description:
                logger.info(f"[缓存命中] 使用ImageDescriptions表中的视频描述: {record.description[:50]}...")
                return f"[视频：{record.description}]"

            logger.info("向视频解析大模型请求理解视频...")
            video_base64 = base64.b64encode(video_bytes).decode("utf-8")

            # 请注意：由于不支持任意视频格式，一律先标注为 mp4 交由远端处理
            prompt = "这是用户发送的一段视频，请仔细观看并详细清晰地描述视频的内容、场景、人物动作以及想要表达的意思。要求直接输出描述结果。"

            description, _ = await self.vlm.generate_response_for_video(
                prompt=prompt, video_base64=video_base64, video_format="mp4", temperature=0.4
            )

            if not description:
                logger.warning("VLM未能生成视频描述")
                return "[视频(大模型解析失败)]"

            # 保存视频文件，后续可能有其他作用，用 hash 命名
            current_timestamp = time.time()
            video_dir = os.path.join(self.VIDEO_DIR, "video")
            file_path = os.path.join(video_dir, f"{int(current_timestamp)}_{video_hash[:8]}.mp4")
            try:
                with open(file_path, "wb") as f:
                    f.write(video_bytes)
            except Exception as e:
                logger.error(f"保存视频文件备份失败: {e}")

            # 存入数据库留作缓存
            try:
                ImageDescriptions.create(
                    image_description_hash=video_hash,
                    type="video",
                    description=description,
                    timestamp=current_timestamp,
                )
            except Exception as e:
                logger.error(f"保存视频描述数据库失败: {e}")

            logger.info(f"视频解析产生新结果: {description[:50]}...")
            return f"[视频：{description}]"

        except Exception as e:
            logger.error(f"处理视频流程中发生异常: {e}")
            return "[视频(处理异常)]"


video_manager = None


def get_video_manager() -> VideoManager:
    """获取全局视频管理器单例"""
    global video_manager
    if video_manager is None:
        video_manager = VideoManager()
    return video_manager
