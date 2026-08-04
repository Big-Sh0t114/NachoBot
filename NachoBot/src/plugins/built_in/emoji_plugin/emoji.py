import asyncio
import json
import random
from typing import Tuple

from json_repair import repair_json

# 导入新插件系统
from src.plugin_system import BaseAction, ActionActivationType

# 导入依赖的系统组件
from src.common.logger import get_logger
from src.chat.emoji_system.emoji_collage import build_emoji_collage
from src.chat.utils.utils_image import image_path_to_base64

# 导入API模块 - 标准Python包方式
from src.plugin_system.apis import emoji_api, llm_api, message_api

# NoReplyAction已集成到heartFC_chat.py中，不再需要导入
from src.config.config import global_config, model_config
from src.llm_models.utils_model import LLMRequest


logger = get_logger("emoji")


def _parse_selected_index(response: str, candidate_count: int) -> int | None:
    try:
        data = json.loads(repair_json(response))
        index = data.get("index") if isinstance(data, dict) else None
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if 1 <= index <= candidate_count:
            return index - 1
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


class EmojiAction(BaseAction):
    """表情动作 - 发送表情包"""

    activation_type = ActionActivationType.RANDOM
    random_activation_probability = global_config.emoji.emoji_chance
    parallel_action = True

    # 动作基本信息
    action_name = "emoji"
    action_description = "发送表情包辅助表达情绪"

    # LLM判断提示词
    llm_judge_prompt = """
    判定是否需要使用表情动作的条件：
    1. 用户明确要求使用表情包
    2. 这是一个适合表达强烈情绪的场合
    3. 不要发送太多表情包，如果你已经发送过多个表情包则回答"否"
    
    请回答"是"或"否"。
    """

    # 动作参数定义
    action_parameters = {}

    # 动作使用场景
    action_require = [
        "发送表情包辅助表达情绪",
        "表达情绪时可以选择使用",
        "不要连续发送，如果你已经发过[表情包]，就不要选择此动作",
    ]

    # 关联类型
    associated_types = ["emoji"]

    async def execute(self) -> Tuple[bool, str]:
        # sourcery skip: assign-if-exp, introduce-default-else, swap-if-else-branches, use-named-expression
        """执行表情动作"""
        try:
            # 1. 获取发送表情的原因
            reason = self.action_data.get("reason", "表达当前情绪")

            # 2. 从全部有效标签中选择最符合语境的标签
            available_emotions = emoji_api.get_available_emotions()
            if not available_emotions:
                logger.warning(f"{self.log_prefix} 没有带有效标签的表情包")
                return False, "没有带有效标签的表情包"

            recent_messages = message_api.get_recent_messages(chat_id=self.chat_id, limit=5)
            messages_text = ""
            if recent_messages:
                messages_text = message_api.build_readable_messages(
                    messages=recent_messages,
                    timestamp_mode="normal_no_YMD",
                    truncate=False,
                    show_actions=False,
                )

            prompt = f"""
            你是一个正在进行聊天的网友。请根据发送理由和最近聊天记录，从给定标签中选择最匹配的一个。
            最近的聊天记录：
            {messages_text}

            发送理由：{reason}
            可用标签：{available_emotions}
            只返回列表中一个标签的原文，不要解释或添加其他文字。
            """
            if global_config.debug.show_prompt:
                logger.info(f"{self.log_prefix} 生成的标签选择 Prompt: {prompt}")
            else:
                logger.debug(f"{self.log_prefix} 生成的标签选择 Prompt: {prompt}")

            models = llm_api.get_available_models()
            chat_model_config = models.get("utils_small")
            if not chat_model_config:
                logger.error(f"{self.log_prefix} 未找到'utils_small'模型配置")
                return False, "未找到'utils_small'模型配置"

            success, chosen_emotion, _, _ = await llm_api.generate_with_model(
                prompt,
                model_config=chat_model_config,
                request_type="emoji.tag_select",
                temperature=0,
                max_tokens=50,
            )
            normalized_emotions = {emotion.strip().casefold(): emotion for emotion in available_emotions}
            normalized_choice = chosen_emotion.strip().strip("\"'").casefold() if success else ""
            if normalized_choice in normalized_emotions:
                chosen_emotion = normalized_emotions[normalized_choice]
            else:
                chosen_emotion = random.choice(available_emotions)
                logger.warning(f"{self.log_prefix} 标签模型返回无效，随机使用标签: {chosen_emotion}")

            # 3. 优先抽取所选标签，不足时由其他标签补足，并保留解码后备项
            sampled_candidates = emoji_api.sample_candidates_by_emotion(chosen_emotion, count=10, backup_count=5)
            collage = await asyncio.to_thread(build_emoji_collage, sampled_candidates, 10)
            if not collage or not collage.candidates:
                logger.warning(f"{self.log_prefix} 没有可读取的表情包候选")
                return False, "没有可读取的表情包候选"

            if len(collage.candidates) == 1:
                selected_candidate = collage.candidates[0]
            else:
                visual_prompt = f"""
                你正在为聊天选择一张表情包。图片是带连续编号的候选联系图。
                最近的聊天记录：
                {messages_text}

                发送理由：{reason}
                已选择标签：{chosen_emotion}
                请从 1 到 {len(collage.candidates)} 中选择最符合当前语境的一张。
                只输出严格 JSON，例如 {{"index": 3}}，不要输出解释、Markdown 或其他字段。
                """
                selected_index = None
                try:
                    vlm = LLMRequest(model_set=model_config.model_task_config.vlm, request_type="emoji.select")
                    response, _ = await vlm.generate_response_for_image(
                        prompt=visual_prompt,
                        image_base64=collage.image_base64,
                        image_format=collage.image_format,
                        temperature=0,
                        max_tokens=50,
                    )
                    selected_index = _parse_selected_index(response, len(collage.candidates))
                    if selected_index is None:
                        logger.warning(f"{self.log_prefix} VLM返回无效选择: {response[:200]}")
                except Exception as error:
                    logger.warning(f"{self.log_prefix} VLM选图失败，将在候选内随机选择: {error}")

                if selected_index is None:
                    selected_candidate = random.choice(collage.candidates)
                else:
                    selected_candidate = collage.candidates[selected_index]

            emoji_base64 = image_path_to_base64(selected_candidate.full_path)
            emoji_description = selected_candidate.description
            logger.info(
                f"{self.log_prefix} 发送表情包[{chosen_emotion}] "
                f"候选数={len(collage.candidates)} hash={selected_candidate.emoji_hash[:8]}，原因: {reason}"
            )

            # 4. 只发送原图，并在发送成功后记录使用
            success = await self.send_emoji(emoji_base64)

            if success:
                emoji_api.record_usage(selected_candidate.emoji_hash)
                # 存储动作信息
                await self.store_action_info(
                    action_build_into_prompt=True,
                    action_prompt_display=f"你发送了表情包，原因：{reason}",
                    action_done=True,
                )
                return True, f"成功发送表情包:{emoji_description}"
            else:
                error_msg = "发送表情包失败"
                logger.error(f"{self.log_prefix} {error_msg}")

                await self.send_text("执行表情包动作失败")
                return False, error_msg

        except Exception as e:
            logger.error(f"{self.log_prefix} 表情动作执行失败: {e}", exc_info=True)
            return False, f"表情发送失败: {str(e)}"
