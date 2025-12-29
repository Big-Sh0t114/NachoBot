import json
import random

from json_repair import repair_json
from typing import List, Tuple

from src.config.config import global_config, model_config
from src.common.logger import get_logger
from src.common.data_models.database_data_model import DatabaseMessages
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.utils.utils import parse_keywords_string
from src.chat.utils.chat_message_builder import build_readable_messages
from src.chat.memory_system.Hippocampus import hippocampus_manager
from src.llm_models.utils_model import LLMRequest


logger = get_logger("memory_activator")


def get_keywords_from_json(json_str) -> List:
    """
    从JSON字符串中提取关键词列表

    Args:
        json_str: JSON格式的字符串

    Returns:
        List[str]: 关键词列表
    """
    try:
        # 使用repair_json修复JSON格式
        fixed_json = repair_json(json_str)

        # 如果repair_json返回的是字符串，需要解析为Python对象
        result = json.loads(fixed_json) if isinstance(fixed_json, str) else fixed_json
        return result.get("keywords", [])
    except Exception as e:
        logger.error(f"解析关键词JSON失败: {e}")
        return []


def init_prompt():
    # --- Group Chat Prompt ---
    memory_activator_prompt = """
    你需要根据以下信息来挑选合适的记忆编号
    以下是一段聊天记录，请根据这些信息，和下方的记忆，挑选和群聊内容有关的记忆编号
    
    聊天记录:
    {obs_info_text}
    你想要回复的消息:
    {target_message}
    
    记忆：
    {memory_info}
    
    请输出一个json格式，包含以下字段：
    {{
        "memory_ids": "记忆1编号,记忆2编号,记忆3编号,......"
    }}
    不要输出其他多余内容，只输出json格式就好
    """

    Prompt(memory_activator_prompt, "memory_activator_prompt")


class MemoryActivator:
    def __init__(self):
        self.key_words_model = LLMRequest(
            model_set=model_config.model_task_config.utils_small,
            request_type="memory.activator",
        )
        # 用于记忆选择的 LLM 模型
        self.memory_selection_model = LLMRequest(
            model_set=model_config.model_task_config.utils_small,
            request_type="memory.selection",
        )

    async def activate_memory_with_chat_history(
        self, target_message, chat_history: List[DatabaseMessages]
    ) -> List[Tuple[str, str]]:
        """
        激活记忆
        """
        # 如果记忆系统被禁用，直接返回空列表
        if not global_config.memory.enable_memory:
            return []

        keywords_list = set()

        for msg in chat_history:
            keywords = parse_keywords_string(msg.key_words)
            if keywords:
                if len(keywords_list) < 30:
                    # 最多容纳30个关键词
                    keywords_list.update(keywords)
                    logger.debug(f"提取关键词: {keywords_list}")
                else:
                    break

        if not keywords_list:
            logger.debug("没有提取到关键词，返回空记忆列表")
            return []

        # 从海马体获取相关记忆
        related_memory = await hippocampus_manager.get_memory_from_topic(
            valid_keywords=list(keywords_list), max_memory_num=5, max_memory_length=3, max_depth=3
        )

        # logger.info(f"当前记忆关键词: {keywords_list}")
        logger.debug(f"获取到的记忆: {related_memory}")

        if not related_memory:
            logger.debug("海马体没有返回相关记忆")
            return []

        used_ids = set()
        candidate_memories = []

        # 为每个记忆分配随机ID并过滤相关记忆
        for memory in related_memory:
            keyword, content = memory
            found = any(kw in content for kw in keywords_list)
            if found:
                # 随机分配一个不重复的2位数id
                while True:
                    random_id = "{:02d}".format(random.randint(0, 99))
                    if random_id not in used_ids:
                        used_ids.add(random_id)
                        break
                candidate_memories.append({"memory_id": random_id, "keyword": keyword, "content": content})

        if not candidate_memories:
            logger.info("没有找到相关的候选记忆")
            return []

        # 如果只有少量记忆，直接返回
        if len(candidate_memories) <= 2:
            logger.debug(f"候选记忆较少({len(candidate_memories)}个)，直接返回")
            # 转换为 (keyword, content) 格式
            return [(mem["keyword"], mem["content"]) for mem in candidate_memories]

        return await self._select_memories_with_llm(target_message, chat_history, candidate_memories)

    async def _select_memories_with_llm(
        self, target_message, chat_history: List[DatabaseMessages], candidate_memories
    ) -> List[Tuple[str, str]]:
        """
        使用 LLM 选择合适的记忆

        Args:
            target_message: 目标消息
            chat_history_prompt: 聊天历史
            candidate_memories: 候选记忆列表，每个记忆包含 memory_id、keyword、content

        Returns:
            List[Tuple[str, str]]: 选择的记忆列表，格式为 (keyword, content)
        """
        try:
            # 构建聊天历史字符串
            obs_info_text = build_readable_messages(
                chat_history,
                replace_bot_name=True,
                timestamp_mode="relative",
                read_mark=0.0,
                show_actions=True,
            )

            # 构建记忆信息字符串
            memory_lines = []
            for memory in candidate_memories:
                memory_id = memory["memory_id"]
                keyword = memory["keyword"]
                content = memory["content"]

                # 将 content 列表转换为字符串
                if isinstance(content, list):
                    content_str = " | ".join(str(item) for item in content)
                else:
                    content_str = str(content)

                memory_lines.append(f"记忆编号 {memory_id}: [关键词: {keyword}] {content_str}")

            memory_info = "\n".join(memory_lines)

            # 获取并格式化 prompt
            prompt_template = await global_prompt_manager.get_prompt_async("memory_activator_prompt")
            formatted_prompt = prompt_template.format(
                obs_info_text=obs_info_text, target_message=target_message, memory_info=memory_info
            )

            # 调用 LLM
            response, (reasoning_content, model_name, _) = await self.memory_selection_model.generate_response_async(
                formatted_prompt, temperature=0.3, max_tokens=150
            )

            if global_config.debug.show_prompt:
                logger.info(f"记忆选择 prompt: {formatted_prompt}")
                logger.info(f"LLM 记忆选择响应: {response}")
            else:
                logger.debug(f"记忆选择 prompt: {formatted_prompt}")
                logger.debug(f"LLM 记忆选择响应: {response}")

            # 解析响应获取选择的记忆编号
            try:
                fixed_json = repair_json(response)

                # 解析为 Python 对象
                result = json.loads(fixed_json) if isinstance(fixed_json, str) else fixed_json

                # 提取 memory_ids 字段并解析逗号分隔的编号
                if memory_ids_str := result.get("memory_ids", ""):
                    memory_ids = [mid.strip() for mid in str(memory_ids_str).split(",") if mid.strip()]
                    # 过滤掉空字符串和无效编号
                    valid_memory_ids = [mid for mid in memory_ids if mid and len(mid) <= 3]
                    selected_memory_ids = valid_memory_ids
                else:
                    selected_memory_ids = []
            except Exception as e:
                logger.error(f"解析记忆选择响应失败: {e}", exc_info=True)
                selected_memory_ids = []

            # 根据编号筛选记忆
            selected_memories = []
            memory_id_to_memory = {mem["memory_id"]: mem for mem in candidate_memories}

            selected_memories = [
                memory_id_to_memory[memory_id] for memory_id in selected_memory_ids if memory_id in memory_id_to_memory
            ]
            logger.info(f"LLM 选择的记忆编号: {selected_memory_ids}")
            logger.info(f"最终选择的记忆数量: {len(selected_memories)}")

            # 转换为 (keyword, content) 格式
            return [(mem["keyword"], mem["content"]) for mem in selected_memories]

        except Exception as e:
            logger.error(f"LLM 选择记忆时出错: {e}", exc_info=True)
            # 出错时返回前3个候选记忆作为备选，转换为 (keyword, content) 格式
            return [(mem["keyword"], mem["content"]) for mem in candidate_memories[:3]]


init_prompt()
