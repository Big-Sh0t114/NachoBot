from typing import Tuple

from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.utils.prompt_builder import Prompt
from src.plugin_system import BaseAction, ActionActivationType
from src.chat.memory_system.Hippocampus import hippocampus_manager
from src.chat.utils.utils import cut_key_words

logger = get_logger("memory")


def init_prompt():
    Prompt(
        """
以下是一些记忆条目的分类：
----------------------
{category_list}
----------------------
每一个分类条目类型代表了你对用户："{person_name}"的印象的一个类别

现在，你有一条对 {person_name} 的新记忆内容：
{memory_point}

请判断该记忆内容是否属于上述分类，请给出分类的名称。
如果不属于上述分类，请输出一个合适的分类名称，对新记忆内容进行概括。要求分类名具有概括性。
注意分类数一般不超过5个
请严格用json格式输出，不要输出任何其他内容：
{{
    "category": "分类名称"
}} """,
        "relation_category",
    )

    Prompt(
        """
以下是有关{category}的现有记忆：
----------------------
{memory_list}
----------------------

现在，你有一条对 {person_name} 的新记忆内容：
{memory_point}

请判断该新记忆内容是否已经存在于现有记忆中，你可以对现有进行进行以下修改：
注意，一般来说记忆内容不超过5个，且记忆文本不应太长

1.新增：当记忆内容不存在于现有记忆，且不存在矛盾，请用json格式输出：
{{
    "new_memory": "需要新增的记忆内容"
}}
2.加深印象：如果这个新记忆已经存在于现有记忆中，在内容上与现有记忆类似，请用json格式输出：
{{
    "memory_id": 1, #请输出你认为需要加深印象的，与新记忆内容类似的，已经存在的记忆的序号
    "integrate_memory": "加深后的记忆内容，合并内容类似的新记忆和旧记忆"
}}
3.整合：如果这个新记忆与现有记忆产生矛盾，请你结合其他记忆进行整合，用json格式输出：
{{
    "memory_id": 1, #请输出你认为需要整合的，与新记忆存在矛盾的，已经存在的记忆的序号
    "integrate_memory": "整合后的记忆内容，合并内容矛盾的新记忆和旧记忆"
}}

现在，请你根据情况选出合适的修改方式，并输出json，不要输出其他内容：
""",
        "relation_category_update",
    )


class BuildMemoryAction(BaseAction):
    """关系动作 - 构建关系"""

    activation_type = ActionActivationType.LLM_JUDGE
    parallel_action = True

    # 动作基本信息
    action_name = "build_memory"
    action_description = (
        "了解对于某个概念或者某件事的记忆，并存储下来，在之后的聊天中，你可以根据这条记忆来获取相关信息"
    )

    # 动作参数定义
    action_parameters = {
        "concept_name": "需要了解或记忆的概念或事件的名称",
        "concept_description": "需要了解或记忆的概念或事件的描述，需要具体且明确",
    }

    # 动作使用场景
    action_require = [
        "了解对于某个概念或者某件事的记忆，并存储下来，在之后的聊天中，你可以根据这条记忆来获取相关信息",
        "有你不了解的概念",
        "有人要求你记住某个概念或者事件",
        "你对某件事或概念有新的理解，或产生了兴趣",
    ]

    # 关联类型
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        """执行关系动作"""

        try:
            # 1. 获取构建关系的原因
            concept_description = self.action_data.get("concept_description", "")
            logger.info(f"{self.log_prefix} 添加记忆原因: {self.reasoning}")
            concept_name = self.action_data.get("concept_name", "")
            # 2. 获取目标用户信息

            # 对 concept_name 进行jieba分词
            concept_name_tokens = cut_key_words(concept_name)
            # logger.info(f"{self.log_prefix} 对 concept_name 进行分词结果: {concept_name_tokens}")

            filtered_concept_name_tokens = [
                token
                for token in concept_name_tokens
                if all(keyword not in token for keyword in global_config.memory.memory_ban_words)
            ]

            if not filtered_concept_name_tokens:
                logger.warning(f"{self.log_prefix} 过滤后的概念名称列表为空，跳过添加记忆")
                return False, "过滤后的概念名称列表为空，跳过添加记忆"

            similar_topics_dict = (
                hippocampus_manager.get_hippocampus().parahippocampal_gyrus.get_similar_topics_from_keywords(
                    filtered_concept_name_tokens
                )
            )
            await hippocampus_manager.get_hippocampus().parahippocampal_gyrus.add_memory_with_similar(
                concept_description, similar_topics_dict
            )

            return True, f"成功添加记忆: {concept_name}"

        except Exception as e:
            logger.error(f"{self.log_prefix} 构建记忆时出错: {e}")
            return False, f"构建记忆时出错: {e}"


# 还缺一个关系的太多遗忘和对应的提取
init_prompt()
