import base64
import datetime
import json
import os
import random
import traceback
import asyncio
from io import BytesIO
from PIL import Image
from pathlib import Path
from typing import List, Dict

import httpx

from src.common.logger import get_logger
from src.plugin_system.apis import llm_api, config_api, emoji_api, platform_api
from src.plugin_system.core import component_registry
from .qzone_api import QzoneAuthError, create_qzone_api

logger = get_logger('Maizone.组件')


def encode_file(img):
    """将PIL.Image对象编码为base64 data URL"""
    form = (img.format or "PNG").upper()
    buffer = BytesIO()
    img.save(buffer, format=form)
    byte_data = buffer.getvalue()
    mime_type = f"image/{form.lower()}"
    encoded_string = base64.b64encode(byte_data).decode("utf-8")
    return f"data:{mime_type};base64,{encoded_string}"


async def generate_image(provider: str, image_model: str, api_key: str, image_prompt: str, image_dir: str,
                         batch_size: int = 1) -> bool:
    """
    用ModelScope或SiliconFlow API生成说说配图保存至对应路径

    Args:
        provider (str): 图片生成服务提供商，支持 "ModelScope" 或 "SiliconFlow"。
        image_model (str): 使用的图片生成模型名称。
        api_key (str): ModelScope 或 SiliconFlow API密钥。
        image_prompt (str): 说说内容，用于生成配图的描述。
        image_dir (str): 图片保存的目录路径。
        batch_size (int): 每次生成的图片数量，默认为1(Qwen不支持)。

    Returns:
        bool: 如果生成成功返回True，否则返回False。

    """
    # 生成图片
    plugin_config = component_registry.get_plugin_config('MaizonePlugin')
    logger.info(f"将使用{provider}-{image_model}模型生成图片...")
    try:
        if provider.lower() == "siliconflow":
            # SiliconFlow API
            url = "https://api.siliconflow.cn/v1/images/generations"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            data = {
                "model": image_model,
                "prompt": image_prompt,
                "negative_prompt": "lowres, bad anatomy, bad hands, text, error, cropped, worst quality, low quality, "
                                   "normal quality, jpeg artifacts, signature, watermark, username, blurry",
                "seed": random.randint(1, 9999999999),
            }
            if image_model == "Kwai-Kolors/Kolors":
                data["batch_size"] = batch_size  # Kolors模型支持多图

            # 查找参考图片
            ref_images = list(Path(image_dir).glob("done_ref.*"))
            if ref_images and config_api.get_plugin_config(plugin_config, "models.image_ref", False):
                image = Image.open(ref_images[0])
                data["image"] = encode_file(image)

            async with httpx.AsyncClient() as client:
                # 发送请求
                res = await client.post(url, headers=headers, json=data, timeout=60.0)
                if res.status_code != 200:
                    logger.error(f'生成图片出错，错误码[{res.status_code}]')
                    logger.error(f'错误响应: {res.text}')
                    return False
                json_data = res.json()
                image_urls = [img["url"] for img in json_data["images"]]

                # 确保目录存在
                Path(image_dir).mkdir(parents=True, exist_ok=True)

                # 下载并保存图片
                for i, img_url in enumerate(image_urls):
                    try:
                        # 下载图片
                        img_response = await client.get(img_url, timeout=60.0)
                        img_response.raise_for_status()

                        filename = f"sf_{i}.png"
                        save_path = Path(image_dir) / filename

                        # 处理图片
                        image = Image.open(BytesIO(img_response.content))
                        image.save(save_path)
                        logger.info(f"图片已保存至: {save_path}")

                    except Exception as e:
                        logger.error(f"下载图片失败: {str(e)}")
                        return False

        elif provider.lower() == "modelscope":
            # ModelScope API
            base_url = 'https://api-inference.modelscope.cn/'
            common_headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            # 准备请求数据
            data_ = {
                "model": image_model,
                "prompt": image_prompt,
                "negative_prompt": "lowres, bad anatomy, bad hands, text, error, cropped, worst quality, low quality, "
                                   "normal quality, jpeg artifacts, signature, watermark, username, blurry",
            }

            # 查找参考图片
            ref_images = list(Path(image_dir).glob("done_ref.*"))
            if ref_images and config_api.get_plugin_config(plugin_config, "models.image_ref", False):
                image = Image.open(ref_images[0])
                data_["image"] = encode_file(image)

            async with httpx.AsyncClient() as client:
                # 发送异步生成请求
                response = await client.post(
                    f"{base_url}v1/images/generations",
                    headers={**common_headers, "X-ModelScope-Async-Mode": "true"},
                    content=json.dumps(data_, ensure_ascii=False).encode('utf-8'),
                    timeout=60.0
                )
                response.raise_for_status()
                task_id = response.json()["task_id"]

                # 轮询任务状态
                while True:
                    result = await client.get(
                        f"{base_url}v1/tasks/{task_id}",
                        headers={**common_headers, "X-ModelScope-Task-Type": "image_generation"},
                        timeout=60.0
                    )
                    result.raise_for_status()
                    data = result.json()

                    if data["task_status"] == "SUCCEED":
                        # 下载生成的图片
                        image_url = data["output_images"][0]
                        img_response = await client.get(image_url, timeout=60.0)
                        img_response.raise_for_status()

                        # 处理图片
                        image = Image.open(BytesIO(img_response.content))

                        # 确保目录存在
                        Path(image_dir).mkdir(parents=True, exist_ok=True)

                        # 保存图片
                        filename = "ms_result.jpg"
                        save_path = Path(image_dir) / filename
                        image.save(save_path)
                        logger.info(f"图片已保存至: {save_path}")
                        break

                    elif data["task_status"] == "FAILED":
                        logger.error("生成图片任务失败")
                        return False

                    # 等待5秒后再次检查
                    await asyncio.sleep(5)

        else:
            logger.error(f"不支持的图片生成服务提供商: {provider}")
            return False

        return True

    except Exception as e:
        logger.error(f"生成图片失败: {e}")
        logger.error(traceback.format_exc())
        return False


def format_feed_list(feed_list: List[Dict]) -> str:
    """
    格式化说说列表为分层清晰的字符串以便显示
    Args:
        feed_list: 说说列表

    Returns:
        str: 格式化后的字符串
    """
    if not feed_list:
        return "feed_list 为空"

    # 检查是否是错误情况
    if len(feed_list) == 1 and "error" in feed_list[0]:
        error_msg = feed_list[0].get("error", "未知错误")
        return f"{error_msg}"

    result = []
    result.append("=" * 80)
    result.append("FEED LIST")
    result.append("=" * 80)

    for i, feed in enumerate(feed_list, 1):
        result.append(f"\nFeed #{i}")
        result.append("-" * 40)

        # 基本信息
        result.append(f"target_qq: {feed.get('target_qq', 'N/A')}")
        result.append(f"tid: {feed.get('tid', 'N/A')}")
        result.append(f"content: {feed.get('content', 'N/A')}")

        # 图片信息
        images = feed.get('images', [])
        if images:
            result.append(f"images: {len(images)}")
            for j, img in enumerate(images, 1):
                result.append(f"  image_{j}: {img}")
        else:
            result.append("images: []")

        # 视频信息
        videos = feed.get('videos', [])
        if videos:
            result.append(f"videos: {len(videos)}")
            for j, video in enumerate(videos, 1):
                result.append(f"  video_{j}: {video}")
        else:
            result.append("videos: []")

        # 转发内容
        rt_con = feed.get('rt_con', '')
        result.append(f"rt_con: {rt_con if rt_con else 'N/A'}")

        # 评论信息
        comments = feed.get('comments', [])
        if comments:
            result.append(f"comments: {len(comments)}")
            for j, comment in enumerate(comments, 1):
                result.append(f"  comment_{j}:")
                result.append(f"    qq_account: {comment.get('qq_account', 'N/A')}")
                result.append(f"    nickname: {comment.get('nickname', 'N/A')}")
                result.append(f"    comment_tid: {comment.get('comment_tid', 'N/A')}")
                result.append(f"    content: {comment.get('content', 'N/A')}")
                parent_tid = comment.get('parent_tid')
                result.append(f"    parent_tid: {parent_tid if parent_tid else 'None'}")
                if j < len(comments):  # 不在最后一个评论后加空行
                    result.append("")
        else:
            result.append("comments: []")

    result.append("=" * 80)
    result.append(f"总数: {len(feed_list)}")

    return "\n".join(result)


async def send_feed(message: str,
                    image_directory: str = "",
                    enable_image: bool = False,
                    image_mode: str = "random",
                    ai_probability: float = 0.5,
                    image_number: int = 1,
                    ) -> bool:
    """
    发送说说及图片目录下的所有未处理图片。

    Args:
        message (str): 要发送的说说内容。
        image_directory (str): 图片存储的目录路径。
        enable_image (bool): 是否启用图片功能。
        image_mode (str): 图片模式，可选值为 "only_ai", "only_emoji", "random"。
        ai_probability (float): 在随机模式下使用AI生成图片的概率，范围为0到1。
        image_number (int): 要生成的图片数量，范围为1到4。

    Returns:
        bool: 如果发送成功返回True，否则返回False。

    Raises:
        Exception: 如果在发送过程中发生错误，将记录日志并返回False。
    """
    qzone = create_qzone_api()

    images = []
    if not enable_image:
        # 如果未启用图片功能，直接发送纯文本
        try:
            tid = await qzone.publish_emotion(message, images)
            logger.info(f"成功发送说说，tid: {tid}")
            return True
        except Exception:
            logger.error("发送说说失败")
            logger.error(traceback.format_exc())
            return False

    # 验证配置有效性
    if image_mode not in ["only_ai", "only_emoji", "random"]:
        logger.error(f"无效的图片模式: {image_mode}，已默认更改为 random")
        image_mode = "random"
    ai_probability = max(0.0, min(1.0, ai_probability))  # 限制在0-1之间
    image_number = max(1, min(4, image_number))  # 限制在1-4之间

    # 决定图片来源
    if image_mode == "only_ai":
        use_ai = True
    elif image_mode == "only_emoji":
        use_ai = False
    else:  # random模式
        use_ai = random.random() < ai_probability

    # 获取图片
    if use_ai:
        # 使用AI生成图片
        plugin_config = component_registry.get_plugin_config('MaizonePlugin')
        if api_key := config_api.get_plugin_config(plugin_config, "models.api_key", ""):
            models = llm_api.get_available_models()
            prompt_model = config_api.get_plugin_config(plugin_config, "models.text_model", "replyer")  # 获取模型配置
            model_config = models[prompt_model]
            personality = config_api.get_global_config("personality.personality", "一只猫娘")  # 人格
            image_provider = config_api.get_plugin_config(plugin_config, "models.image_provider", "SiliconFlow")
            image_model = config_api.get_plugin_config(plugin_config, "models.image_model",
                                                       "Kwai-Kolors/Kolors")  # 获取图片模型配置
            enable_ref = config_api.get_plugin_config(plugin_config, "models.image_ref", False)  # 启用参考图
            logger.info("正在生成图片提示词...")
            # 生成图片提示词
            prompt = f"""
                请根据以下QQ空间说说内容配图，并构建生成配图的风格和prompt。
                说说主人信息：'{personality}'。
                说说内容:'{message}'。
                请注意：仅回复用于生成图片的prompt，不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )
                """
            if enable_ref:
                prompt += "说说主人的人设参考图片将随同提示词一起发送给生图AI，可使用'in the style of'或'基于此图'等描述引导生成风格"
            success, image_prompt, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_config,
                request_type="story.generate",
                temperature=0.3,
                max_tokens=1000
            )
            if success:
                logger.info(f'即将生成说说配图：{image_prompt}')
            else:
                logger.error('生成说说配图prompt失败')
            ai_success = await generate_image(
                provider=image_provider,
                image_model=image_model,
                api_key=api_key,
                image_prompt=image_prompt,
                image_dir=image_directory,
                batch_size=image_number
            )
            if ai_success:
                # 获取目录下所有文件
                all_files = [f for f in os.listdir(image_directory)
                             if os.path.isfile(os.path.join(image_directory, f))]

                # 筛选未处理的图片（不以"done_"开头的文件）
                unprocessed_files = [f for f in all_files if not f.startswith("done_")]
                unprocessed_files_sorted = sorted(unprocessed_files)

                for image_file in unprocessed_files_sorted:
                    full_path = os.path.join(image_directory, image_file)
                    with open(full_path, "rb") as img:
                        images.append(img.read())

                    # 生成带时间戳的前缀 (格式: done_YYYYMMDD_HHMMSS_)
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    new_filename = f"done_{timestamp}_{image_file}"
                    new_path = os.path.join(image_directory, new_filename)
                    os.rename(full_path, new_path)
            else:
                logger.error("AI图片生成失败")
                return False
        else:
            logger.error("未配置API Key，无法生成AI图片")
            return False
    else:
        # 使用表情包
        for _ in range(image_number):
            image = await emoji_api.get_by_description(message)
            if image:
                image_base64, description, scene = image
                image_data = base64.b64decode(image_base64)
                images.append(image_data)

    try:
        tid = await qzone.publish_emotion(message, images)
        logger.info(f"成功发送说说，tid: {tid}")
        return True
    except Exception:
        logger.error("发送说说失败")
        logger.error(traceback.format_exc())
        return False


async def read_feed(target_qq: str, num: int) -> list[dict]:
    """
    通过调用QZone API的`get_list`方法阅读指定QQ号的说说，返回说说列表。

    Args:
        target_qq (str): 目标QQ号，表示需要读取其说说的用户。
        num (int): 要获取的说说数量。

    Returns:
        list: 包含说说信息的列表；确实没有动态时返回空列表。

    Raises:
        Exception: QQ空间拒绝请求或响应无法解析时向调用方传播。

    """
    qzone = create_qzone_api()

    try:
        feeds_list = await qzone.get_list(target_qq, num)
        logger.debug(f"获取到的说说列表: {format_feed_list(feeds_list)}")
        return feeds_list
    except Exception:
        logger.error("获取list失败")
        logger.error(traceback.format_exc())
        raise


async def monitor_read_feed() -> list[dict]:
    """
    通过调用QZone API的`monitor_get_list`方法定时阅读说说，返回说说列表。

    Returns:
        list: 包含说说信息的列表。

    Raises:
        Exception: 如果在获取说说列表时发生错误，记录后向调用方传播。
    """
    qzone = create_qzone_api()

    try:
        feeds_list = await qzone.monitor_get_list()
        logger.debug(f"获取到的说说列表: {format_feed_list(feeds_list)}")
        return feeds_list
    except QzoneAuthError:
        raise
    except Exception:
        logger.error("获取list失败")
        logger.error(traceback.format_exc())
        raise


async def like_feed(target_qq: str, fid: str, abstime: int = 0) -> bool:
    """
    调用QZone API的`like`方法点赞指定说说。

    Args:
        target_qq (str): 目标QQ号，表示需要点赞其说说的用户。
        fid (str): 说说的动态ID。

    Returns:
        bool: 如果点赞成功返回True，否则返回False。

    Raises:
        Exception: 如果在点赞过程中发生错误，将记录错误日志并返回False。
    """
    try:
        success = await platform_api.like_qzone(fid, target_qq, abstime=abstime)
    except platform_api.PlatformAPINotSupportedError:
        qzone = create_qzone_api()
        if qzone is None:
            return False
        success = await qzone.like(fid, target_qq, abstime=abstime)
    if not success:
        logger.error("点赞失败")
        logger.error(traceback.format_exc())
        return success
    return True


async def comment_feed(target_qq: str, fid: str, content: str) -> bool:
    """
    通过调用QZone API的`comment`方法评论指定说说。

    Args:
        target_qq (str): 目标QQ号，表示需要评论其说说的用户。
        fid (str): 说说的动态ID。
        content (str): 评论的文本内容。

    Returns:
        bool: 如果评论成功返回True，否则返回False。

    Raises:
        Exception: 如果在评论过程中发生错误，将记录错误日志并返回False。
    """
    try:
        success = await platform_api.comment_qzone(fid, target_qq, content)
    except platform_api.PlatformAPINotSupportedError:
        qzone = create_qzone_api()
        if qzone is None:
            return False
        success = await qzone.comment(fid, target_qq, content)
    if not success:
        logger.error("评论失败")
        logger.error(traceback.format_exc())
        return False
    return True


async def reply_feed(fid: str, target_qq: str, target_nickname: str, content: str, comment_tid: str) -> bool:
    """
    通过调用QZone API的`reply`方法回复指定评论。

    Args:
        fid (str): 说说的动态ID。
        target_qq (str): 目标QQ号。
        target_nickname (str): 目标QQ昵称。
        content (str): 回复的文本内容。
        comment_tid (str): 评论的唯一标识ID。

    Returns:
        bool: 如果回复成功返回True，否则返回False。

    Raises:
        Exception: 如果在回复过程中发生错误，将记录错误日志并返回False。
    """
    qzone = create_qzone_api()

    success = await qzone.reply(fid, target_qq, target_nickname, content, comment_tid)
    if not success:
        logger.error("评论失败")
        logger.error(traceback.format_exc())
        return False
    return True
