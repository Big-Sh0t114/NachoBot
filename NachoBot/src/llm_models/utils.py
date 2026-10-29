import base64
import io

from PIL import Image
from datetime import datetime

from src.common.logger import get_logger
from src.common.media_conversion_cache import media_conversion_cache
from src.common.database.database import db  # 确保 db 被导入用于 create_tables
from src.common.database.database_model import LLMUsage
from src.config.api_ada_configs import ModelInfo
from .payload_content.message import Message, MessageBuilder
from .model_client.base_client import UsageRecord

logger = get_logger("消息压缩工具")

MAX_DECODE_SOURCE_PIXELS = 16_000_000
MAX_ANIMATION_SCAN_FRAMES = 256
MAX_ANIMATION_RETAINED_FRAMES = 32
MAX_ANIMATION_DECODED_PIXELS = 64_000_000
MAX_ANIMATION_OUTPUT_PIXELS = 16_000_000
MAX_ANIMATION_OUTPUT_EDGE = 2_048


def _check_source_image_size(image: Image.Image) -> tuple[int, int]:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_DECODE_SOURCE_PIXELS:
        raise ValueError(f"image dimensions exceed the decode budget: {width}x{height}")
    return width, height


def _seekable_animation(image: Image.Image) -> bool:
    try:
        image.seek(1)
    except EOFError:
        image.seek(0)
        return False
    _check_source_image_size(image)
    image.seek(0)
    _check_source_image_size(image)
    return True


def _bounded_animation_plan(
    image: Image.Image,
) -> tuple[list[int], list[int], int, list[tuple[int, int]]]:
    """Scan only a bounded prefix and choose a deterministic, duration-preserving sample."""
    loop_count = max(0, int(image.info.get("loop", 0)))
    durations: list[int] = []
    frame_sizes: list[tuple[int, int]] = []
    scanned_pixels = 0
    for frame_index in range(MAX_ANIMATION_SCAN_FRAMES):
        try:
            image.seek(frame_index)
        except EOFError:
            break
        frame_size = _check_source_image_size(image)
        frame_pixels = frame_size[0] * frame_size[1]
        if scanned_pixels + frame_pixels > MAX_ANIMATION_DECODED_PIXELS:
            break
        scanned_pixels += frame_pixels
        frame_sizes.append(frame_size)
        durations.append(max(0, int(image.info.get("duration", 100))))

    if not durations:
        return [], [], loop_count, []

    scanned_frames = len(durations)
    retained_count = min(MAX_ANIMATION_RETAINED_FRAMES, scanned_frames)
    if scanned_frames <= retained_count:
        selected_indices = list(range(scanned_frames))
    else:
        selected_indices = [
            round(index * (scanned_frames - 1) / (retained_count - 1))
            for index in range(retained_count)
        ]

    retained_durations = []
    for selected_position, selected_index in enumerate(selected_indices):
        next_index = (
            selected_indices[selected_position + 1]
            if selected_position + 1 < len(selected_indices)
            else scanned_frames
        )
        retained_durations.append(sum(durations[selected_index:next_index]))

    return selected_indices, retained_durations, loop_count, frame_sizes


def compress_messages(messages: list[Message], img_target_size: int = 1 * 1024 * 1024) -> list[Message]:
    """
    压缩消息列表中的图片
    :param messages: 消息列表
    :param img_target_size: 图片目标大小，默认1MB
    :return: 压缩后的消息列表
    """

    def _has_transparency(image: Image.Image) -> bool:
        if "transparency" in image.info:
            return True
        if "A" not in image.getbands():
            return False
        alpha = image.getchannel("A")
        try:
            minimum, _maximum = alpha.getextrema()
            return minimum < 255
        finally:
            alpha.close()

    def reformat_static_image(image_data: bytes) -> bytes:
        """
        将不透明静态图片转换为JPEG；保留透明图片及动图的原格式。
        :param image_data: 图片数据
        :return: 转换后的图片数据
        """
        with Image.open(io.BytesIO(image_data)) as image:
            _check_source_image_size(image)
            if _seekable_animation(image):
                return image_data
            if image.format and image.format.upper() in ["JPEG", "JPG", "PNG", "WEBP"]:
                image.load()
                if _has_transparency(image):
                    output = io.BytesIO()
                    image.save(output, format="PNG", optimize=True)
                    return output.getvalue()
                reformated_image_data = io.BytesIO()
                converted = image.convert("RGB")
                try:
                    converted.save(reformated_image_data, format="JPEG", quality=95, optimize=True)
                finally:
                    converted.close()
                return reformated_image_data.getvalue()
        return image_data

    def rescale_image(image_data: bytes, scale: float) -> tuple[bytes, tuple[int, int] | None, tuple[int, int] | None]:
        """
        缩放图片
        :param image_data: 图片数据
        :param scale: 缩放比例
        :return: 缩放后的图片数据
        """
        with Image.open(io.BytesIO(image_data)) as image:
            original_size = _check_source_image_size(image)
            pixel_count = original_size[0] * original_size[1]
            output_buffer = io.BytesIO()

            if _seekable_animation(image):
                selected_indices, durations, loop_count, frame_sizes = _bounded_animation_plan(image)
                if not selected_indices:
                    raise ValueError("animated image contains no decodable frames")
                if any(frame_size != original_size for frame_size in frame_sizes):
                    raise ValueError("animated frame canvas changed during bounded scan")

                retained_count = len(selected_indices)
                scale_for_output = min(
                    max(0.0, scale) * 0.5,
                    MAX_ANIMATION_OUTPUT_EDGE / max(original_size),
                    (MAX_ANIMATION_OUTPUT_PIXELS / (retained_count * pixel_count)) ** 0.5,
                )
                new_size = (
                    max(1, int(original_size[0] * scale_for_output)),
                    max(1, int(original_size[1] * scale_for_output)),
                )
                while new_size[0] * new_size[1] * retained_count > MAX_ANIMATION_OUTPUT_PIXELS:
                    if new_size[0] >= new_size[1] and new_size[0] > 1:
                        new_size = (new_size[0] - 1, new_size[1])
                    elif new_size[1] > 1:
                        new_size = (new_size[0], new_size[1] - 1)
                    else:
                        break

                selected_set = set(selected_indices)
                frames: list[Image.Image] = []
                replayed_pixels = 0
                try:
                    for frame_index in range(selected_indices[-1] + 1):
                        image.seek(frame_index)
                        frame_size = _check_source_image_size(image)
                        if frame_size != frame_sizes[frame_index]:
                            raise ValueError("animated frame dimensions changed between bounded passes")
                        replayed_pixels += frame_size[0] * frame_size[1]
                        if replayed_pixels > MAX_ANIMATION_DECODED_PIXELS:
                            raise ValueError("animated frame replay exceeded the decode pixel budget")
                        if frame_index in selected_set:
                            decoded_frame = image.convert("RGBA")
                            try:
                                frames.append(decoded_frame.resize(new_size, Image.Resampling.LANCZOS))
                            finally:
                                decoded_frame.close()
                        else:
                            # Complete intervening delta frames so selected
                            # frames retain their displayed composited content.
                            image.load()

                    output_format = (image.format or "GIF").upper()
                    if output_format == "WEBP":
                        frames[0].save(
                            output_buffer,
                            format="WEBP",
                            save_all=True,
                            append_images=frames[1:],
                            duration=durations,
                            loop=loop_count,
                            quality=80,
                            method=4,
                        )
                    else:
                        frames[0].save(
                            output_buffer,
                            format="GIF",
                            save_all=True,
                            append_images=frames[1:],
                            optimize=True,
                            duration=durations,
                            loop=loop_count,
                        )
                finally:
                    for frame in frames:
                        frame.close()
            else:
                new_size = (
                    max(1, int(original_size[0] * scale)),
                    max(1, int(original_size[1] * scale)),
                )
                image.load()
                resized_image = image.resize(new_size, Image.Resampling.LANCZOS)
                try:
                    if _has_transparency(resized_image):
                        resized_image.save(output_buffer, format="PNG", optimize=True)
                    else:
                        converted = resized_image.convert("RGB")
                        try:
                            converted.save(output_buffer, format="JPEG", quality=95, optimize=True)
                        finally:
                            converted.close()
                finally:
                    resized_image.close()

        return output_buffer.getvalue(), original_size, new_size

    def compress_base64_image(
        image_format: str, base64_data: str, target_size: int = 1 * 1024 * 1024
    ) -> tuple[str, str]:
        original_b64_data_size = len(base64_data)  # 计算原始数据大小

        image_data = base64.b64decode(base64_data)

        def convert() -> bytes:
            # 先尝试转换格式为JPEG
            converted = reformat_static_image(image_data)
            encoded = base64.b64encode(converted)
            if len(encoded) <= target_size:
                logger.info(f"成功将图片转为JPEG格式，编码后大小: {len(encoded) / 1024:.1f}KB")
                return converted

            # 如果转换后仍然大于目标大小，进行尺寸压缩
            scale = min(1.0, target_size / len(encoded))
            resized, original_size, new_size = rescale_image(converted, scale)
            if original_size and new_size:
                logger.info(
                    f"压缩图片: {original_size[0]}x{original_size[1]} -> {new_size[0]}x{new_size[1]}\n"
                    f"压缩前大小: {original_b64_data_size / 1024:.1f}KB, 压缩后大小: "
                    f"{len(base64.b64encode(resized)) / 1024:.1f}KB"
                )
            return resized

        try:
            converted_data = media_conversion_cache.get_or_compute(
                image_data,
                "llm-message-image-compression",
                {
                    "version": 2,
                    "target_size": target_size,
                    "static_quality": 95,
                    "animated_scale_factor": 0.5,
                    "max_source_pixels": MAX_DECODE_SOURCE_PIXELS,
                    "max_animation_scan_frames": MAX_ANIMATION_SCAN_FRAMES,
                    "max_animation_retained_frames": MAX_ANIMATION_RETAINED_FRAMES,
                    "max_animation_decoded_pixels": MAX_ANIMATION_DECODED_PIXELS,
                    "max_animation_output_pixels": MAX_ANIMATION_OUTPUT_PIXELS,
                    "max_animation_output_edge": MAX_ANIMATION_OUTPUT_EDGE,
                    "animation_output": {"webp_quality": 80, "webp_method": 4, "gif_optimize": True},
                    "resampling": "lanczos",
                },
                convert,
            )
        except Exception as error:
            logger.error(f"图片压缩失败，保留原始内容: {error}")
            return image_format, base64_data
        converted_base64 = base64.b64encode(converted_data).decode("utf-8")
        try:
            with Image.open(io.BytesIO(converted_data)) as converted_image:
                converted_format = (converted_image.format or image_format).lower()
        except Exception:
            converted_format = image_format
        return converted_format, converted_base64

    compressed_messages = []
    for message in messages:
        if isinstance(message.content, list):
            # 检查content，如有图片则压缩
            message_builder = MessageBuilder()
            for content_item in message.content:
                if isinstance(content_item, tuple) and len(content_item) == 2:
                    # 图片，进行压缩
                    compressed_format, compressed_base64 = compress_base64_image(
                        content_item[0], content_item[1], target_size=img_target_size
                    )
                    message_builder.add_image_content(compressed_format, compressed_base64)
                elif isinstance(content_item, tuple) and len(content_item) == 3 and content_item[0] == "video":
                    message_builder.add_video_content(content_item[1], content_item[2])
                else:
                    message_builder.add_text_content(content_item)
            compressed_messages.append(message_builder.build())
        else:
            compressed_messages.append(message)

    return compressed_messages


class LLMUsageRecorder:
    """
    LLM使用情况记录器
    """

    def __init__(self):
        try:
            # 使用 Peewee 创建表，safe=True 表示如果表已存在则不会抛出错误
            db.create_tables([LLMUsage], safe=True)
            # logger.debug("LLMUsage 表已初始化/确保存在。")
        except Exception as e:
            logger.error(f"创建 LLMUsage 表失败: {str(e)}")

    def record_usage_to_database(
        self,
        model_info: ModelInfo,
        model_usage: UsageRecord,
        user_id: str,
        request_type: str,
        endpoint: str,
        time_cost: float = 0.0,
    ):
        prompt_tokens = max(0, int(model_usage.prompt_tokens or 0))
        completion_tokens = max(0, int(model_usage.completion_tokens or 0))
        total_tokens = max(0, int(model_usage.total_tokens or 0))
        cache_reported = bool(getattr(model_usage, "prompt_cache_reported", False))
        cache_hit_tokens = max(0, int(getattr(model_usage, "prompt_cache_hit_tokens", 0) or 0))
        if not cache_reported:
            # Missing provider metadata must retain the ordinary input price.
            cache_hit_tokens = 0
            cache_miss_tokens = 0
        else:
            cache_hit_tokens = min(cache_hit_tokens, prompt_tokens)
            cache_miss_tokens = prompt_tokens - cache_hit_tokens

        configured_cache_price = getattr(model_info, "cache_price_in", None)
        effective_cache_price = model_info.price_in if configured_cache_price is None else configured_cache_price
        input_cost = (
            (prompt_tokens - cache_hit_tokens) * model_info.price_in
            + cache_hit_tokens * effective_cache_price
        ) / 1000000
        output_cost = (completion_tokens / 1000000) * model_info.price_out
        total_cost = round(input_cost + output_cost, 6)
        try:
            # 使用 Peewee 模型创建记录
            LLMUsage.create(
                model_name=model_info.model_identifier,
                model_assign_name=model_info.name,
                model_api_provider=model_info.api_provider,
                user_id=user_id,
                request_type=request_type,
                endpoint=endpoint,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                prompt_cache_hit_tokens=cache_hit_tokens,
                prompt_cache_miss_tokens=cache_miss_tokens,
                prompt_cache_reported=cache_reported,
                cost=total_cost or 0.0,
                time_cost=round(time_cost or 0.0, 3),
                status="success",
                timestamp=datetime.now(),  # Peewee 会处理 DateTimeField
            )
            logger.debug(
                f"Token使用情况 - 模型: {model_usage.model_name}, "
                f"用户: {user_id}, 类型: {request_type}, "
                f"提示词: {model_usage.prompt_tokens}, 完成: {model_usage.completion_tokens}, "
                f"总计: {model_usage.total_tokens}"
            )
        except Exception as e:
            logger.error(f"记录token使用情况失败: {str(e)}")


llm_usage_recorder = LLMUsageRecorder()
