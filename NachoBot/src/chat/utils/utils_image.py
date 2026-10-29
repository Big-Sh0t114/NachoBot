import asyncio
import base64
import os
import time
import hashlib
import uuid
import io
import numpy as np

from typing import Any, Optional, Tuple
from PIL import Image
from rich.traceback import install

from src.common.logger import get_logger
from src.common.database.database import db
from src.common.database.database_model import Images, ImageDescriptions
from src.common.media_conversion_cache import MEDIA_FILESYSTEM_LOCK, media_conversion_cache
from src.config.config import model_config
from src.llm_models.utils_model import LLMRequest
from src.chat.utils.visual_policy import (
    CORE_GENERIC_EMOJI_PROMPT,
    CORE_GENERIC_GIF_EMOJI_PROMPT,
    CORE_GENERIC_IMAGE_PROMPT,
    VisualTaskPolicy,
    resolve_visual_task_policy,
    scoped_media_hash,
)

install(extra_lines=3)

logger = get_logger("chat_image")

MAX_DECODE_SOURCE_PIXELS = 16_000_000
MAX_GIF_SCAN_FRAMES = 256
MAX_GIF_OUTPUT_FRAMES = 15
MAX_GIF_OUTPUT_HEIGHT = 200
MAX_GIF_OUTPUT_WIDTH = 8_192


def _legacy_uint8_frame_mse(current: np.ndarray, previous: np.ndarray) -> float:
    """Match the prior uint8 subtraction/square semantics with bounded scratch."""
    current_flat = current.reshape(-1)
    previous_flat = previous.reshape(-1)
    total = 0
    chunk_size = 262_144
    for start in range(0, current_flat.size, chunk_size):
        end = min(start + chunk_size, current_flat.size)
        difference = np.subtract(current_flat[start:end], previous_flat[start:end], dtype=np.uint8)
        np.square(difference, out=difference)
        total += int(np.sum(difference, dtype=np.uint64))
    return total / current_flat.size


class ImageManager:
    _instance = None
    IMAGE_DIR = "data"  # 图像存储根目录

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._ensure_image_dir()

            self._initialized = True
            # Image perception is routed by Core.  Keep the attribute as a
            # compatibility marker for callers that inspect ImageManager, but
            # do not instantiate a provider/model in Core-owned utilities.
            self.vlm = None

            try:
                db.connect(reuse_if_open=True)
                db.create_tables([Images, ImageDescriptions], safe=True)
            except Exception as e:
                logger.error(f"数据库连接或表创建失败: {e}")

            self._initialized = True

    def _ensure_image_dir(self):
        """确保图像存储目录存在"""
        os.makedirs(self.IMAGE_DIR, exist_ok=True)

    @staticmethod
    def _get_description_from_db(image_hash: str, description_type: str) -> Optional[str]:
        """从数据库获取图片描述

        Args:
            image_hash: 图片哈希值
            description_type: 描述类型 ('emoji' 或 'image')

        Returns:
            Optional[str]: 描述文本，如果不存在则返回None
        """
        try:
            with MEDIA_FILESYSTEM_LOCK:
                record = ImageDescriptions.get_or_none(
                    (ImageDescriptions.image_description_hash == image_hash)
                    & (ImageDescriptions.type == description_type)
                )
                if record is not None:
                    record.timestamp = time.time()
                    record.save()
            return record.description if record else None
        except Exception as e:
            logger.error(f"从数据库获取描述失败 (Peewee): {str(e)}")
            return None

    @staticmethod
    def _save_description_to_db(image_hash: str, description: str, description_type: str) -> None:
        """保存图片描述到数据库

        Args:
            image_hash: 图片哈希值
            description: 描述文本
            description_type: 描述类型 ('emoji' 或 'image')
        """
        try:
            current_timestamp = time.time()
            defaults = {"description": description, "timestamp": current_timestamp}
            with MEDIA_FILESYSTEM_LOCK:
                desc_obj, created = ImageDescriptions.get_or_create(
                    image_description_hash=image_hash, type=description_type, defaults=defaults
                )
                if not created:
                    desc_obj.description = description
                    desc_obj.timestamp = current_timestamp
                    desc_obj.save()
        except Exception as e:
            logger.error(f"保存描述到数据库失败 (Peewee): {str(e)}")

    def _restore_and_touch_cached_image(
        self,
        record: Any,
        image_bytes: bytes,
        *,
        preferred_directory: str,
    ) -> bool:
        """Restore a missing managed file, then refresh file-presence/access fields."""
        try:
            from src.chat.emoji_system.media_cache_cleanup import (
                EMOJI_RESTORE_DIRECTORIES,
                IMAGE_EXTENSIONS,
                IMAGE_RESTORE_DIRECTORIES,
                restore_cached_file,
            )

            image_type = str(getattr(record, "type", "image") or "image").casefold()
            allowed_directories = EMOJI_RESTORE_DIRECTORIES if image_type == "emoji" else IMAGE_RESTORE_DIRECTORIES
            image_id = getattr(record, "image_id", None)
            file_path = getattr(record, "path", None)
            if not file_path and image_id:
                extension = ".png"
                if preferred_directory != "images":
                    try:
                        with Image.open(io.BytesIO(image_bytes)) as source_image:
                            extension = f".{(source_image.format or 'png').lower()}"
                    except Exception:
                        extension = ".png"
                file_path = os.path.join(self.IMAGE_DIR, preferred_directory, f"{image_id}{extension}")
                record.path = file_path

            with db.connection_context(), MEDIA_FILESYSTEM_LOCK:
                file_restored_or_present = False
                if file_path:
                    file_restored_or_present = restore_cached_file(
                        file_path,
                        image_bytes,
                        root=self.IMAGE_DIR,
                        allowed_directories=allowed_directories,
                        expected_extensions=IMAGE_EXTENSIONS,
                    )
                    if not file_restored_or_present and os.path.isfile(file_path) and not os.path.islink(file_path):
                        # Existing files outside the managed cache scope are not
                        # rewritten or cleaned, but they are present on disk.
                        file_restored_or_present = True
                if hasattr(record, "last_used_time"):
                    record.last_used_time = time.time()
                if hasattr(record, "no_file_flag"):
                    record.no_file_flag = not file_restored_or_present
                record.save()
            return file_restored_or_present
        except Exception as error:
            logger.warning(f"恢复图片缓存文件失败: {error}")
            return False

    async def get_emoji_tag(self, image_base64: str) -> str:
        from src.chat.emoji_system.emoji_manager import get_emoji_manager

        emoji_manager = get_emoji_manager()
        if isinstance(image_base64, str):
            image_base64 = image_base64.encode("ascii", errors="ignore").decode("ascii")
        image_bytes = base64.b64decode(image_base64)
        image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
        emotion_list = await emoji_manager.get_emoji_tag_by_hash(image_hash, image_bytes=image_bytes)
        if not emotion_list:
            return "[表情包：未知]"
        tag_str = ",".join(emotion_list)
        return f"[表情包：{tag_str}]"

    async def get_emoji_description(
        self,
        image_base64: str,
        additional_config: Any = None,
    ) -> str:
        """获取表情包描述，优先使用Emoji表中的缓存数据"""
        try:
            # 计算图片哈希
            # 确保base64字符串只包含ASCII字符
            if isinstance(image_base64, str):
                image_base64 = image_base64.encode("ascii", errors="ignore").decode("ascii")
            image_bytes = base64.b64decode(image_base64)
            image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
            image_format = Image.open(io.BytesIO(image_bytes)).format.lower()  # type: ignore
            policy = resolve_visual_task_policy(
                additional_config,
                "emoji",
                default_prompt=CORE_GENERIC_EMOJI_PROMPT,
                default_gif_prompt=CORE_GENERIC_GIF_EMOJI_PROMPT,
                default_temperature=0.4,
                default_max_tokens=800,
            )

            # 优先使用EmojiManager查询已注册表情包的描述
            try:
                from src.chat.emoji_system.emoji_manager import get_emoji_manager

                emoji_manager = get_emoji_manager()
                tags = await emoji_manager.get_emoji_tag_by_hash(image_hash, image_bytes=image_bytes)
                if tags:
                    tag_str = ",".join(tags)
                    logger.info(f"[缓存命中] 使用已注册表情包描述: {tag_str}...")
                    return f"[表情包：{tag_str}]"
            except Exception as e:
                logger.debug(f"查询EmojiManager时出错: {e}")

            # 查询ImageDescriptions表的缓存描述
            if cached_description := self._get_description_from_db(
                image_hash,
                policy.cache_type,
            ):
                cached_emoji = Images.get_or_none((Images.emoji_hash == image_hash) & (Images.type == "emoji"))
                if cached_emoji is not None:
                    await asyncio.to_thread(
                        self._restore_and_touch_cached_image,
                        cached_emoji,
                        image_bytes,
                        preferred_directory="emoji",
                    )
                logger.info(f"[缓存命中] 使用ImageDescriptions表中的描述: {cached_description[:50]}...")
                return f"[表情包：{cached_description}]"

            # === 二步走识别流程 ===

            # 第一步：VLM视觉分析 - 生成详细描述
            if image_format in ["gif", "GIF"]:
                image_base64_processed = await asyncio.to_thread(self.transform_gif, image_base64)
                if image_base64_processed is None:
                    logger.warning("GIF转换失败，无法获取描述")
                    return "[表情包(GIF处理失败)]"
                vlm_prompt = policy.gif_prompt or policy.prompt
                from src.multimodal import get_multimodal_router

                perception = await get_multimodal_router().describe_emoji(
                    image_base64_processed,
                    media_format="jpg",
                    prompt=vlm_prompt,
                    metadata={
                        "temperature": policy.temperature,
                        "max_tokens": policy.max_tokens,
                        "extra_params": dict(policy.extra_params),
                    },
                )
                if perception.degraded:
                    return perception.text or "[表情包(VLM描述生成失败)]"
                detailed_description = perception.text
            else:
                from src.multimodal import get_multimodal_router

                perception = await get_multimodal_router().describe_emoji(
                    image_base64,
                    media_format=image_format,
                    prompt=policy.prompt,
                    metadata={
                        "temperature": policy.temperature,
                        "max_tokens": policy.max_tokens,
                        "extra_params": dict(policy.extra_params),
                    },
                )
                if perception.degraded:
                    return perception.text or "[表情包(VLM描述生成失败)]"
                detailed_description = perception.text

            if detailed_description is None:
                logger.warning("VLM未能生成表情包详细描述")
                return "[表情包(VLM描述生成失败)]"

            # 第二步：LLM情感分析 - 基于详细描述生成简短的情感标签
            emotion_prompt = f"""
            请你基于这个表情包的详细描述，提取出最核心的情感含义，用1-2个词概括。
            详细描述：'{detailed_description}'
            
            要求：
            1. 只输出1-2个最核心的情感词汇
            2. 从互联网梗、meme的角度理解
            3. 输出简短精准，不要解释
            4. 如果有多个词用逗号分隔
            """

            # 使用较低温度确保输出稳定
            emotion_llm = LLMRequest(model_set=model_config.model_task_config.utils_small, request_type="emoji")
            emotion_result, _ = await emotion_llm.generate_response_async(emotion_prompt, temperature=0.3)

            if not emotion_result:
                logger.warning("LLM未能生成情感标签，使用详细描述的前几个词")
                # 降级处理：从详细描述中提取关键词
                import jieba

                words = list(jieba.cut(detailed_description))
                emotion_result = "，".join(words[:2]) if len(words) >= 2 else (words[0] if words else "表情")

            # 处理情感结果，取前1-2个最重要的标签
            emotions = [e.strip() for e in emotion_result.replace("，", ",").split(",") if e.strip()]
            final_emotion = emotions[0] if emotions else "表情"

            # 如果有第二个情感且不重复，也包含进来
            if len(emotions) > 1 and emotions[1] != emotions[0]:
                final_emotion = f"{emotions[0]}，{emotions[1]}"

            logger.debug(f"[emoji识别] 详细描述: {detailed_description[:50]}... -> 情感标签: {final_emotion}")

            if cached_description := self._get_description_from_db(
                image_hash,
                policy.cache_type,
            ):
                logger.warning(f"虽然生成了描述，但是找到缓存表情包描述: {cached_description}")
                cached_emoji = Images.get_or_none((Images.emoji_hash == image_hash) & (Images.type == "emoji"))
                if cached_emoji is not None:
                    await asyncio.to_thread(
                        self._restore_and_touch_cached_image,
                        cached_emoji,
                        image_bytes,
                        preferred_directory="emoji",
                    )
                return f"[表情包：{cached_description}]"

            # 保存表情包文件和元数据（用于可能的后续分析）
            logger.debug(f"保存表情包: {image_hash}")
            current_timestamp = time.time()
            filename = f"{int(current_timestamp)}_{image_hash[:8]}.{image_format}"
            emoji_dir = os.path.join(self.IMAGE_DIR, "emoji")
            from src.chat.emoji_system.media_cache_cleanup import ensure_managed_directory, restore_cached_file

            file_path = os.path.join(emoji_dir, filename)

            try:
                def save_emoji() -> None:
                    with db.connection_context(), MEDIA_FILESYSTEM_LOCK:
                        if not ensure_managed_directory(self.IMAGE_DIR, "emoji"):
                            raise OSError(f"表情缓存目录不可安全访问: {emoji_dir}")
                        if not restore_cached_file(
                            file_path,
                            image_bytes,
                            root=self.IMAGE_DIR,
                            allowed_directories={"emoji"},
                        ):
                            raise OSError(f"无法安全保存表情缓存文件: {file_path}")
                        # 保存到数据库 (Images表) - 包含详细描述用于可能的注册流程
                        try:
                            img_obj = Images.get((Images.emoji_hash == image_hash) & (Images.type == "emoji"))
                            img_obj.path = file_path
                            img_obj.description = detailed_description
                            img_obj.timestamp = current_timestamp
                            img_obj.last_used_time = current_timestamp
                            img_obj.no_file_flag = False
                            img_obj.save()
                        except Images.DoesNotExist:  # type: ignore
                            Images.create(
                                image_id=str(uuid.uuid4()),
                                emoji_hash=image_hash,
                                path=file_path,
                                type="emoji",
                                description=detailed_description,
                                timestamp=current_timestamp,
                                vlm_processed=True,
                                last_used_time=current_timestamp,
                                no_file_flag=False,
                            )

                await asyncio.to_thread(save_emoji)
            except Exception as e:
                logger.error(f"保存表情包文件或元数据失败: {str(e)}")

            # 保存最终的情感标签到缓存 (ImageDescriptions表)
            self._save_description_to_db(
                image_hash,
                final_emotion,
                policy.cache_type,
            )

            return f"[表情包：{final_emotion}]"

        except Exception as e:
            logger.error(f"获取表情包描述失败: {str(e)}")
            return "[表情包(处理失败)]"

    async def get_image_description(
        self,
        image_base64: str,
        additional_config: Any = None,
    ) -> str:
        """获取普通图片描述，优先使用Images表中的缓存数据"""
        try:
            # 计算图片哈希
            if isinstance(image_base64, str):
                image_base64 = image_base64.encode("ascii", errors="ignore").decode("ascii")
            image_bytes = base64.b64decode(image_base64)
            raw_image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
            policy = resolve_visual_task_policy(
                additional_config,
                "image",
                default_prompt=CORE_GENERIC_IMAGE_PROMPT,
                default_temperature=0.4,
                default_max_tokens=300,
            )
            image_hash = scoped_media_hash(raw_image_hash, policy)

            # 优先检查Images表中是否已有完整的描述
            existing_image = Images.get_or_none((Images.emoji_hash == image_hash) & (Images.type == "image"))
            if existing_image:
                # 更新计数
                if hasattr(existing_image, "count") and existing_image.count is not None:
                    existing_image.count += 1
                else:
                    existing_image.count = 1
                existing_image.save()
                await asyncio.to_thread(
                    self._restore_and_touch_cached_image,
                    existing_image,
                    image_bytes,
                    preferred_directory="image",
                )

                # 如果已有描述，直接返回
                if existing_image.description:
                    logger.debug(f"[缓存命中] 使用Images表中的图片描述: {existing_image.description[:50]}...")
                    return f"[图片：{existing_image.description}]"

            if cached_description := self._get_description_from_db(
                image_hash,
                policy.cache_type,
            ):
                if existing_image is not None:
                    await asyncio.to_thread(
                        self._restore_and_touch_cached_image,
                        existing_image,
                        image_bytes,
                        preferred_directory="image",
                    )
                logger.debug(f"[缓存命中] 使用ImageDescriptions表中的描述: {cached_description[:50]}...")
                return f"[图片：{cached_description}]"

            # 调用AI获取描述
            image_format = Image.open(io.BytesIO(image_bytes)).format.lower()  # type: ignore
            logger.info(f"[VLM调用] 为图片生成新描述 (Hash: {image_hash[:8]}...)")
            from src.multimodal import get_multimodal_router

            perception = await get_multimodal_router().describe_image(
                image_base64,
                media_format=image_format,
                prompt=policy.prompt,
                metadata={
                    "temperature": policy.temperature,
                    "max_tokens": policy.max_tokens,
                    "extra_params": dict(policy.extra_params),
                },
            )
            if perception.degraded:
                return perception.text or "[图片(描述生成失败)]"
            description = perception.text

            if description is None:
                logger.warning("AI未能生成图片描述")
                return "[图片(描述生成失败)]"

            # 保存图片和描述
            current_timestamp = time.time()
            filename = f"{int(current_timestamp)}_{image_hash[:8]}.{image_format}"
            image_dir = os.path.join(self.IMAGE_DIR, "image")
            from src.chat.emoji_system.media_cache_cleanup import ensure_managed_directory, restore_cached_file

            file_path = os.path.join(image_dir, filename)

            try:
                def save_image() -> None:
                    with db.connection_context(), MEDIA_FILESYSTEM_LOCK:
                        if not ensure_managed_directory(self.IMAGE_DIR, "image"):
                            raise OSError(f"图片缓存目录不可安全访问: {image_dir}")
                        if not restore_cached_file(
                            file_path,
                            image_bytes,
                            root=self.IMAGE_DIR,
                            allowed_directories={"image"},
                        ):
                            raise OSError(f"无法安全保存图片缓存文件: {file_path}")

                        if existing_image:
                            existing_image.path = file_path
                            existing_image.description = description
                            existing_image.timestamp = current_timestamp
                            existing_image.last_used_time = current_timestamp
                            existing_image.no_file_flag = False
                            if not hasattr(existing_image, "image_id") or not existing_image.image_id:
                                existing_image.image_id = str(uuid.uuid4())
                            if not hasattr(existing_image, "vlm_processed") or existing_image.vlm_processed is None:
                                existing_image.vlm_processed = True
                            existing_image.save()
                            logger.debug(f"[数据库] 更新已有图片记录: {image_hash[:8]}...")
                        else:
                            Images.create(
                                image_id=str(uuid.uuid4()),
                                emoji_hash=image_hash,
                                path=file_path,
                                type="image",
                                description=description,
                                timestamp=current_timestamp,
                                vlm_processed=True,
                                count=1,
                                last_used_time=current_timestamp,
                                no_file_flag=False,
                            )
                            logger.debug(f"[数据库] 创建新图片记录: {image_hash[:8]}...")

                await asyncio.to_thread(save_image)
            except Exception as e:
                logger.error(f"保存图片文件或元数据失败: {str(e)}")

            # 保存描述到ImageDescriptions表作为备用缓存
            self._save_description_to_db(
                image_hash,
                description,
                policy.cache_type,
            )

            logger.info(f"[VLM完成] 图片描述生成: {description[:50]}...")
            return f"[图片：{description}]"
        except Exception as e:
            logger.error(f"获取图片描述失败: {str(e)}")
            return "[图片(处理失败)]"

    @staticmethod
    def _transform_gif_uncached(
        gif_base64: str, similarity_threshold: float = 1000.0, max_frames: int = 15
    ) -> Optional[str]:
        """将GIF转换为水平拼接的静态图像, 跳过相似的帧

        Args:
            gif_base64: GIF的base64编码字符串
            similarity_threshold: 判定帧相似的阈值 (MSE)，越小表示要求差异越大才算不同帧，默认1000.0
            max_frames: 最大抽取的帧数，默认15

        Returns:
            Optional[str]: 拼接后的JPG图像的base64编码字符串, 或者在失败时返回None
        """
        selected_frames: list[Image.Image] = []
        try:
            if isinstance(gif_base64, str):
                gif_base64 = gif_base64.encode("ascii", errors="ignore").decode("ascii")
            gif_data = base64.b64decode(gif_base64)
            frame_limit = max(1, min(int(max_frames), MAX_GIF_OUTPUT_FRAMES))

            with Image.open(io.BytesIO(gif_data)) as gif:
                width, height = gif.size
                if width <= 0 or height <= 0 or width * height > MAX_DECODE_SOURCE_PIXELS:
                    raise ValueError(f"GIF source dimensions exceed the decode budget: {width}x{height}")
                source_size = (width, height)

                target_scale = min(
                    MAX_GIF_OUTPUT_HEIGHT / height,
                    MAX_GIF_OUTPUT_WIDTH / (frame_limit * width),
                )
                target_width = max(1, round(width * target_scale))
                target_height = max(1, round(height * target_scale))
                last_selected_frame_np: np.ndarray | None = None

                # Decode frames in order. Only selected, already-resized output
                # frames survive an iteration; a long animation stops at the
                # explicit metadata/decode budget.
                for frame_index in range(MAX_GIF_SCAN_FRAMES):
                    try:
                        gif.seek(frame_index)
                    except EOFError:
                        break
                    frame_width, frame_height = gif.size
                    if (
                        frame_width <= 0
                        or frame_height <= 0
                        or frame_width * frame_height > MAX_DECODE_SOURCE_PIXELS
                        or (frame_width, frame_height) != source_size
                    ):
                        raise ValueError("GIF frame dimensions exceed or change from the source budget")

                    current_frame = gif.convert("RGB")
                    current_frame_np: np.ndarray | None = None
                    try:
                        current_frame.load()
                        current_frame_np = np.array(current_frame)
                        should_select = last_selected_frame_np is None
                        if last_selected_frame_np is not None:
                            should_select = (
                                _legacy_uint8_frame_mse(current_frame_np, last_selected_frame_np)
                                > similarity_threshold
                            )

                        if should_select:
                            resized_frame = current_frame.resize(
                                (target_width, target_height),
                                Image.Resampling.LANCZOS,
                            )
                            selected_frames.append(resized_frame)
                            last_selected_frame_np = current_frame_np
                            if len(selected_frames) >= frame_limit:
                                break
                    finally:
                        current_frame.close()
                        current_frame_np = None

            if not selected_frames:
                logger.warning("GIF中没有找到任何帧")
                return None

            total_width = target_width * len(selected_frames)
            if total_width > MAX_GIF_OUTPUT_WIDTH or total_width * target_height > MAX_GIF_OUTPUT_WIDTH * MAX_GIF_OUTPUT_HEIGHT:
                raise ValueError("GIF contact sheet exceeds the output dimensions budget")

            with Image.new("RGB", (total_width, target_height)) as combined_image:
                for idx, frame in enumerate(selected_frames):
                    combined_image.paste(frame, (idx * target_width, 0))
                buffer = io.BytesIO()
                combined_image.save(buffer, format="JPEG", quality=85)
                return base64.b64encode(buffer.getvalue()).decode("utf-8")
        except MemoryError:
            logger.error("GIF转换失败: 内存不足，可能是GIF太大或帧数太多")
            return None  # 内存不够啦
        except Exception as e:
            logger.error(f"GIF转换失败: {str(e)}", exc_info=True)  # 记录详细错误信息
            return None  # 其他错误也返回None
        finally:
            for frame in selected_frames:
                frame.close()

    @staticmethod
    def transform_gif(gif_base64: str, similarity_threshold: float = 1000.0, max_frames: int = 15) -> Optional[str]:
        """Return the existing JPEG frame sheet, reusing conversions by content/options."""
        try:
            if isinstance(gif_base64, str):
                gif_base64 = gif_base64.encode("ascii", errors="ignore").decode("ascii")
            gif_data = base64.b64decode(gif_base64)
            encoded_input = base64.b64encode(gif_data).decode("ascii")
            frame_limit = max(1, min(int(max_frames), MAX_GIF_OUTPUT_FRAMES))

            def convert() -> bytes:
                converted_base64 = ImageManager._transform_gif_uncached(
                    encoded_input,
                    similarity_threshold,
                    frame_limit,
                )
                if converted_base64 is None:
                    raise ValueError("GIF frame conversion failed")
                return base64.b64decode(converted_base64)

            result = media_conversion_cache.get_or_compute(
                gif_data,
                "gif-horizontal-frame-sheet",
                {
                    "version": 2,
                    "similarity_threshold": similarity_threshold,
                    "max_frames": frame_limit,
                    "max_source_pixels": MAX_DECODE_SOURCE_PIXELS,
                    "scan_budget_frames": MAX_GIF_SCAN_FRAMES,
                    "target_height": MAX_GIF_OUTPUT_HEIGHT,
                    "target_width": MAX_GIF_OUTPUT_WIDTH,
                    "format": "JPEG",
                    "quality": 85,
                },
                convert,
            )
            return base64.b64encode(result).decode("utf-8")
        except Exception as error:
            logger.error(f"GIF转换失败: {str(error)}", exc_info=True)
            return None

    async def process_image(
        self,
        image_base64: str,
        additional_config: Any = None,
    ) -> Tuple[str, str]:
        # sourcery skip: hoist-if-from-if
        """处理图片并返回图片ID和描述

        Args:
            image_base64: 图片的base64编码

        Returns:
            Tuple[str, str]: (图片ID, 描述)
        """
        try:
            # 生成图片ID
            # 计算图片哈希
            # 确保base64字符串只包含ASCII字符
            if isinstance(image_base64, str):
                image_base64 = image_base64.encode("ascii", errors="ignore").decode("ascii")
            image_bytes = base64.b64decode(image_base64)
            raw_image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
            policy = resolve_visual_task_policy(
                additional_config,
                "image",
                default_prompt=CORE_GENERIC_IMAGE_PROMPT,
                default_temperature=0.4,
                default_max_tokens=300,
            )
            image_hash = scoped_media_hash(raw_image_hash, policy)

            if existing_image := Images.get_or_none((Images.emoji_hash == image_hash) & (Images.type == "image")):
                # 检查是否缺少必要字段，如果缺少则创建新记录
                if (
                    not hasattr(existing_image, "image_id")
                    or not existing_image.image_id
                    or not hasattr(existing_image, "count")
                    or existing_image.count is None
                    or not hasattr(existing_image, "vlm_processed")
                    or existing_image.vlm_processed is None
                ):
                    logger.debug(f"图片记录缺少必要字段，补全旧记录: {image_hash}")
                    if not existing_image.image_id:
                        existing_image.image_id = str(uuid.uuid4())
                    if existing_image.count is None:
                        existing_image.count = 0
                    if existing_image.vlm_processed is None:
                        existing_image.vlm_processed = False

                existing_image.count += 1
                existing_image.save()
                if not await asyncio.to_thread(
                    self._restore_and_touch_cached_image,
                    existing_image,
                    image_bytes,
                    preferred_directory="images",
                ):
                    logger.warning(f"无法恢复图片缓存文件，拒绝返回图片ID: {image_hash[:8]}...")
                    return "", "[图片]"
                if not existing_image.description:
                    await self._process_image_with_vlm(
                        existing_image.image_id,
                        image_base64,
                        policy,
                    )
                return existing_image.image_id, f"[picid:{existing_image.image_id}]"
            else:
                # print(f"图片不存在: {image_hash}")
                image_id = str(uuid.uuid4())

            # 保存新图片
            current_timestamp = time.time()
            image_dir = os.path.join(self.IMAGE_DIR, "images")
            from src.chat.emoji_system.media_cache_cleanup import ensure_managed_directory, restore_cached_file

            filename = f"{image_id}.png"
            file_path = os.path.join(image_dir, filename)

            def save_new_image() -> None:
                if not ensure_managed_directory(self.IMAGE_DIR, "images"):
                    raise OSError(f"图片缓存目录不可安全访问: {image_dir}")
                with db.connection_context(), MEDIA_FILESYSTEM_LOCK:
                    if not restore_cached_file(
                        file_path,
                        image_bytes,
                        root=self.IMAGE_DIR,
                        allowed_directories={"images"},
                    ):
                        raise OSError(f"无法安全保存图片缓存文件: {file_path}")

                    Images.create(
                        image_id=image_id,
                        emoji_hash=image_hash,
                        path=file_path,
                        type="image",
                        timestamp=current_timestamp,
                        vlm_processed=False,
                        count=1,
                        last_used_time=current_timestamp,
                        no_file_flag=False,
                    )

            await asyncio.to_thread(save_new_image)

            # 启动异步VLM处理
            await self._process_image_with_vlm(
                image_id,
                image_base64,
                policy,
            )

            return image_id, f"[picid:{image_id}]"

        except Exception as e:
            logger.error(f"处理图片失败: {str(e)}")
            return "", "[图片]"

    async def _process_image_with_vlm(
        self,
        image_id: str,
        image_base64: str,
        policy: VisualTaskPolicy,
    ) -> None:
        """使用VLM处理图片并更新数据库

        Args:
            image_id: 图片ID
            image_base64: 图片的base64编码
        """
        try:
            # 计算图片哈希
            # 确保base64字符串只包含ASCII字符
            if isinstance(image_base64, str):
                image_base64 = image_base64.encode("ascii", errors="ignore").decode("ascii")
            image_bytes = base64.b64decode(image_base64)
            raw_image_hash = hashlib.md5(image_bytes, usedforsecurity=False).hexdigest()
            image_hash = scoped_media_hash(raw_image_hash, policy)

            # 获取当前图片记录
            image = Images.get(Images.image_id == image_id)
            await asyncio.to_thread(
                self._restore_and_touch_cached_image,
                image,
                image_bytes,
                preferred_directory="images",
            )

            # 优先检查是否已有其他相同哈希的图片记录包含描述
            existing_with_description = Images.get_or_none(
                (Images.emoji_hash == image_hash)
                & (Images.type == "image")
                & (Images.description.is_null(False))
                & (Images.description != "")
            )
            if existing_with_description and existing_with_description.id != image.id:
                logger.debug(f"[缓存复用] 从其他相同图片记录复用描述: {existing_with_description.description[:50]}...")
                image.description = existing_with_description.description
                image.vlm_processed = True
                image.save()
                # 同时保存到ImageDescriptions表作为备用缓存
                self._save_description_to_db(
                    image_hash,
                    existing_with_description.description,
                    policy.cache_type,
                )
                return

            # 检查ImageDescriptions表的缓存描述
            if cached_description := self._get_description_from_db(
                image_hash,
                policy.cache_type,
            ):
                logger.debug(f"[缓存复用] 从ImageDescriptions表复用描述: {cached_description[:50]}...")
                image.description = cached_description
                image.vlm_processed = True
                image.save()
                return

            # 获取图片格式
            image_format = Image.open(io.BytesIO(image_bytes)).format.lower()  # type: ignore

            # 获取VLM描述
            from src.multimodal import get_multimodal_router

            perception = await get_multimodal_router().describe_image(
                image_base64,
                media_format=image_format,
                prompt=policy.prompt,
                metadata={
                    "temperature": policy.temperature,
                    "max_tokens": policy.max_tokens,
                    "extra_params": dict(policy.extra_params),
                },
            )
            if perception.degraded:
                logger.warning("VLM perception degraded; do not cache a failure marker")
                return
            description = perception.text

            if description is None:
                logger.warning("VLM未能生成图片描述")
                description = ""

            if cached_description := self._get_description_from_db(
                image_hash,
                policy.cache_type,
            ):
                logger.info(f"虽然生成了描述，但是找到缓存图片描述: {cached_description}")
                description = cached_description

            # 更新数据库
            image.description = description
            image.vlm_processed = True
            image.save()

            # 保存描述到ImageDescriptions表作为备用缓存
            self._save_description_to_db(
                image_hash,
                description,
                policy.cache_type,
            )

        except Exception as e:
            logger.error(f"VLM处理图片失败: {str(e)}")


# 创建全局单例
image_manager = None


def get_image_manager() -> ImageManager:
    """获取全局图片管理器单例"""
    global image_manager
    if image_manager is None:
        image_manager = ImageManager()
    return image_manager


def image_path_to_base64(image_path: str) -> str:
    """将图片路径转换为base64编码
    Args:
        image_path: 图片文件路径
    Returns:
        str: base64编码的图片数据
    Raises:
        FileNotFoundError: 当图片文件不存在时
        IOError: 当读取图片文件失败时
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    with open(image_path, "rb") as f:
        if image_data := f.read():
            return base64.b64encode(image_data).decode("utf-8")
        else:
            raise IOError(f"读取图片文件失败: {image_path}")
