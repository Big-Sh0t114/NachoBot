# -*- coding: utf-8 -*-
import datetime
import math
import random
import time
import re
import jieba
import networkx as nx
import numpy as np
from typing import List, Tuple, Set, Coroutine, Any
from collections import Counter
import traceback

from rich.traceback import install

from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config, model_config
from src.common.data_models.database_data_model import DatabaseMessages
from src.common.database.database_model import GraphNodes, GraphEdges  # Peewee Models导入
from src.common.logger import get_logger
from src.chat.utils.utils import cut_key_words
from src.chat.utils.chat_message_builder import (
    build_readable_messages,
)  # 导入 build_readable_messages


# 添加cosine_similarity函数
def cosine_similarity(v1, v2):
    """计算余弦相似度"""
    dot_product = np.dot(v1, v2)
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    return 0 if norm1 == 0 or norm2 == 0 else dot_product / (norm1 * norm2)


install(extra_lines=3)


def calculate_information_content(text):
    """计算文本的信息量（熵）"""
    char_count = Counter(text)
    total_chars = len(text)
    if total_chars == 0:
        return 0
    entropy = 0
    for count in char_count.values():
        probability = count / total_chars
        entropy -= probability * math.log2(probability)

    return entropy


logger = get_logger("memory")


class MemoryGraph:
    def __init__(self):
        self.G = nx.Graph()  # 使用 networkx 的图结构

    def connect_dot(self, concept1, concept2):
        # 避免自连接
        if concept1 == concept2:
            return

        current_time = datetime.datetime.now().timestamp()

        # 如果边已存在,增加 strength
        if self.G.has_edge(concept1, concept2):
            self.G[concept1][concept2]["strength"] = self.G[concept1][concept2].get("strength", 1) + 1
            # 更新最后修改时间
            self.G[concept1][concept2]["last_modified"] = current_time
        else:
            # 如果是新边,初始化 strength 为 1
            self.G.add_edge(
                concept1,
                concept2,
                strength=1,
                created_time=current_time,  # 添加创建时间
                last_modified=current_time,
            )  # 添加最后修改时间

    async def add_dot(self, concept, memory, hippocampus_instance=None):
        current_time = datetime.datetime.now().timestamp()

        if concept in self.G:
            if "memory_items" in self.G.nodes[concept]:
                # 获取现有的记忆项（已经是str格式）
                existing_memory = self.G.nodes[concept]["memory_items"]

                # 如果现有记忆不为空，则使用LLM整合新旧记忆
                if existing_memory and hippocampus_instance and hippocampus_instance.model_small:
                    try:
                        integrated_memory = await self._integrate_memories_with_llm(
                            existing_memory, str(memory), hippocampus_instance.model_small
                        )
                        self.G.nodes[concept]["memory_items"] = integrated_memory
                        # 整合成功，增加权重
                        current_weight = self.G.nodes[concept].get("weight", 0.0)
                        self.G.nodes[concept]["weight"] = current_weight + 1.0
                        logger.debug(f"节点 {concept} 记忆整合成功，权重增加到 {current_weight + 1.0}")
                        logger.info(f"节点 {concept} 记忆内容已更新：{integrated_memory}")
                    except Exception as e:
                        logger.error(f"LLM整合记忆失败: {e}")
                        # 降级到简单连接
                        new_memory_str = f"{existing_memory} | {memory}"
                        self.G.nodes[concept]["memory_items"] = new_memory_str
                        logger.info(f"节点 {concept} 记忆内容已简单拼接并更新：{new_memory_str}")
                else:
                    new_memory_str = str(memory)
                    self.G.nodes[concept]["memory_items"] = new_memory_str
                    logger.info(f"节点 {concept} 记忆内容已直接更新：{new_memory_str}")
            else:
                self.G.nodes[concept]["memory_items"] = str(memory)
                # 如果节点存在但没有memory_items,说明是第一次添加memory,设置created_time
                if "created_time" not in self.G.nodes[concept]:
                    self.G.nodes[concept]["created_time"] = current_time
                logger.info(f"节点 {concept} 创建新记忆：{str(memory)}")
            # 更新最后修改时间
            self.G.nodes[concept]["last_modified"] = current_time
        else:
            # 如果是新节点,创建新的记忆字符串
            self.G.add_node(
                concept,
                memory_items=str(memory),
                weight=1.0,  # 新节点初始权重为1.0
                created_time=current_time,  # 添加创建时间
                last_modified=current_time,
            )  # 添加最后修改时间
            logger.info(f"新节点 {concept} 已添加，记忆内容已写入：{str(memory)}")

    def get_dot(self, concept):
        # 检查节点是否存在于图中
        return (concept, self.G.nodes[concept]) if concept in self.G else None

    def get_related_item(self, topic, depth=1):
        if topic not in self.G:
            return [], []

        first_layer_items = []
        second_layer_items = []

        # 获取相邻节点
        neighbors = list(self.G.neighbors(topic))

        # 获取当前节点的记忆项
        node_data = self.get_dot(topic)
        if node_data:
            _, data = node_data
            if "memory_items" in data:
                # 直接使用完整的记忆内容
                if memory_items := data["memory_items"]:
                    first_layer_items.append(memory_items)

        # 只在depth=2时获取第二层记忆
        if depth >= 2:
            # 获取相邻节点的记忆项
            for neighbor in neighbors:
                if node_data := self.get_dot(neighbor):
                    _, data = node_data
                    if "memory_items" in data:
                        # 直接使用完整的记忆内容
                        if memory_items := data["memory_items"]:
                            second_layer_items.append(memory_items)

        return first_layer_items, second_layer_items

    async def _integrate_memories_with_llm(self, existing_memory: str, new_memory: str, llm_model: LLMRequest) -> str:
        """
        使用LLM整合新旧记忆内容

        Args:
            existing_memory: 现有的记忆内容（字符串格式，可能包含多条记忆）
            new_memory: 新的记忆内容
            llm_model: LLM模型实例

        Returns:
            str: 整合后的记忆内容
        """
        try:
            # 构建整合提示
            integration_prompt = f"""你是一个记忆整合专家。请将以下的旧记忆和新记忆整合成一条更完整、更准确的记忆内容。

旧记忆内容：
{existing_memory}

新记忆内容：
{new_memory}

整合要求：
1. 保留重要信息，去除重复内容
2. 如果新旧记忆有冲突，合理整合矛盾的地方
3. 将相关信息合并，形成更完整的描述
4. 保持语言简洁、准确
5. 只返回整合后的记忆内容，不要添加任何解释

整合后的记忆："""

            # 调用LLM进行整合
            content, (reasoning_content, model_name, tool_calls) = await llm_model.generate_response_async(
                integration_prompt
            )

            if content and content.strip():
                integrated_content = content.strip()
                logger.debug(f"LLM记忆整合成功，模型: {model_name}")
                return integrated_content
            else:
                logger.warning("LLM返回的整合结果为空，使用默认连接方式")
                return f"{existing_memory} | {new_memory}"

        except Exception as e:
            logger.error(f"LLM记忆整合过程中出错: {e}")
            return f"{existing_memory} | {new_memory}"

    @property
    def dots(self):
        # 返回所有节点对应的 Memory_dot 对象
        return [self.get_dot(node) for node in self.G.nodes()]

    def forget_topic(self, topic):
        """随机删除指定话题中的一条记忆，如果话题没有记忆则移除该话题节点"""
        if topic not in self.G:
            return None

        # 获取话题节点数据
        node_data = self.G.nodes[topic]

        # 删除整个节点
        self.G.remove_node(topic)
        # 如果节点存在memory_items
        if "memory_items" in node_data:
            if memory_items := node_data["memory_items"]:
                return (
                    f"删除了节点 {topic} 的完整记忆: {memory_items[:50]}..."
                    if len(memory_items) > 50
                    else f"删除了节点 {topic} 的完整记忆: {memory_items}"
                )
        return None


# 海马体
class Hippocampus:
    def __init__(self):
        self.memory_graph = MemoryGraph()
        self.model_small: LLMRequest = None  # type: ignore
        self.entorhinal_cortex: EntorhinalCortex = None  # type: ignore
        self.parahippocampal_gyrus: ParahippocampalGyrus = None  # type: ignore

    def initialize(self):
        # 初始化子组件
        self.entorhinal_cortex = EntorhinalCortex(self)
        self.parahippocampal_gyrus = ParahippocampalGyrus(self)
        # 从数据库加载记忆图
        self.entorhinal_cortex.sync_memory_from_db()
        self.model_small = LLMRequest(
            model_set=model_config.model_task_config.utils_small, request_type="memory.modify"
        )

    def get_all_node_names(self) -> list:
        """获取记忆图中所有节点的名字列表"""
        return list(self.memory_graph.G.nodes())

    def calculate_weighted_activation(self, current_activation: float, edge_strength: int, target_node: str) -> float:
        """
        计算考虑节点权重的激活值

        Args:
            current_activation: 当前激活值
            edge_strength: 边的强度
            target_node: 目标节点名称

        Returns:
            float: 计算后的激活值
        """
        # 基础激活值计算
        base_activation = current_activation - (1 / edge_strength)

        if base_activation <= 0:
            return 0.0

        # 获取目标节点的权重
        if target_node in self.memory_graph.G:
            node_data = self.memory_graph.G.nodes[target_node]
            node_weight = node_data.get("weight", 1.0)

            # 权重加成：每次整合增加10%激活值，最大加成200%
            weight_multiplier = 1.0 + min((node_weight - 1.0) * 0.1, 2.0)

            return base_activation * weight_multiplier
        else:
            return base_activation

    @staticmethod
    def calculate_node_hash(concept, memory_items) -> int:
        """计算节点的特征值"""
        # memory_items已经是str格式，直接按分隔符分割
        if memory_items:
            unique_items = {item.strip() for item in memory_items.split(" | ") if item.strip()}
        else:
            unique_items = set()

        # 使用frozenset来保证顺序一致性
        content = f"{concept}:{frozenset(unique_items)}"
        return hash(content)

    @staticmethod
    def calculate_edge_hash(source, target) -> int:
        """计算边的特征值"""
        # 直接使用元组，保证顺序一致性
        return hash((source, target))

    @staticmethod
    def find_topic_llm(text: str, topic_num: int | list[int]):
        # sourcery skip: inline-immediately-returned-variable
        topic_num_str = ""
        if isinstance(topic_num, list):
            topic_num_str = f"{topic_num[0]}-{topic_num[1]}"
        else:
            topic_num_str = topic_num

        prompt = (
            f"这是一段文字：\n{text}\n\n请你从这段话中总结出最多{topic_num_str}个关键的概念，必须是某种概念，比如人，事，物，概念，事件，地点 等等，帮我列出来，"
            f"将主题用逗号隔开，并加上<>,例如<主题1>,<主题2>......尽可能精简。只需要列举最多{topic_num}个话题就好，不要有序号，不要告诉我其他内容。"
            f"如果确定找不出主题或者没有明显主题，返回<none>。"
        )

        return prompt

    @staticmethod
    def topic_what(text, topic):
        # sourcery skip: inline-immediately-returned-variable
        # 不再需要 time_info 参数
        prompt = (
            f'这是一段文字：\n{text}\n\n我想让你基于这段文字来概括"{topic}"这个概念，帮我总结成几句自然的话，'
            f"要求包含对这个概念的定义，内容，知识，时间和人物，这些信息必须来自这段文字，不能添加信息。\n只输出几句自然的话就好"
        )
        return prompt

    @staticmethod
    def calculate_topic_num(text, compress_rate):
        """计算文本的话题数量"""
        information_content = calculate_information_content(text)
        topic_by_length = text.count("\n") * compress_rate
        topic_by_information_content = max(1, min(5, int((information_content - 3) * 2)))
        topic_num = int((topic_by_length + topic_by_information_content) / 2)
        logger.debug(
            f"topic_by_length: {topic_by_length}, topic_by_information_content: {topic_by_information_content}, "
            f"topic_num: {topic_num}"
        )
        return topic_num

    def get_memory_from_keyword(self, keyword: str, max_depth: int = 2) -> list:
        """从关键词获取相关记忆。

        Args:
            keyword (str): 关键词
            max_depth (int, optional): 记忆检索深度，默认为2。1表示只获取直接相关的记忆，2表示获取间接相关的记忆。

        Returns:
            list: 记忆列表，每个元素是一个元组 (topic, memory_content, similarity)
                - topic: str, 记忆主题
                - memory_content: str, 该主题下的完整记忆内容
                - similarity: float, 与关键词的相似度
        """
        if not keyword:
            return []

        # 获取所有节点
        all_nodes = list(self.memory_graph.G.nodes())
        memories = []

        # 计算关键词的词集合
        keyword_words = set(jieba.cut(keyword))

        # 遍历所有节点，计算相似度
        for node in all_nodes:
            node_words = set(jieba.cut(node))
            all_words = keyword_words | node_words
            v1 = [1 if word in keyword_words else 0 for word in all_words]
            v2 = [1 if word in node_words else 0 for word in all_words]
            similarity = cosine_similarity(v1, v2)

            # 如果相似度超过阈值，获取该节点的记忆
            if similarity >= 0.3:  # 可以调整这个阈值
                node_data = self.memory_graph.G.nodes[node]
                # 直接使用完整的记忆内容
                if memory_items := node_data.get("memory_items", ""):
                    memories.append((node, memory_items, similarity))

        # 按相似度降序排序
        memories.sort(key=lambda x: x[2], reverse=True)
        return memories

    async def get_keywords_from_text(self, text: str) -> Tuple[List[str], List]:
        """从文本中提取关键词。

        Args:
            text (str): 输入文本
            fast_retrieval (bool, optional): 是否使用快速检索。默认为False。
                如果为True，使用jieba分词提取关键词，速度更快但可能不够准确。
                如果为False，使用LLM提取关键词，速度较慢但更准确。
        """
        if not text:
            return [], []

        # 使用LLM提取关键词 - 根据详细文本长度分布优化topic_num计算
        text_length = len(text)
        topic_num: int | list[int] = 0

        keywords_lite = cut_key_words(text)
        if keywords_lite:
            logger.debug(f"提取关键词极简版: {keywords_lite}")

        if text_length <= 12:
            topic_num = [1, 3]  # 6-10字符: 1个关键词 (27.18%的文本)
        elif text_length <= 20:
            topic_num = [2, 4]  # 11-20字符: 2个关键词 (22.76%的文本)
        elif text_length <= 30:
            topic_num = [3, 5]  # 21-30字符: 3个关键词 (10.33%的文本)
        elif text_length <= 50:
            topic_num = [4, 5]  # 31-50字符: 4个关键词 (9.79%的文本)
        else:
            topic_num = 5  # 51+字符: 5个关键词 (其余长文本)

        topics_response, _ = await self.model_small.generate_response_async(self.find_topic_llm(text, topic_num))

        # 提取关键词
        keywords = re.findall(r"<([^>]+)>", topics_response)
        if not keywords:
            keywords = []
        else:
            keywords = [
                keyword.strip()
                for keyword in ",".join(keywords).replace("，", ",").replace("、", ",").replace(" ", ",").split(",")
                if keyword.strip()
            ]

        if keywords:
            logger.debug(f"提取关键词: {keywords}")

        return keywords, keywords_lite

    async def get_memory_from_topic(
        self,
        keywords: list[str],
        max_memory_num: int = 3,
        max_memory_length: int = 2,
        max_depth: int = 3,
    ) -> list:
        """从文本中提取关键词并获取相关记忆。

        Args:
            keywords (list): 输入文本
            max_memory_num (int, optional): 返回的记忆条目数量上限。默认为3，表示最多返回3条与输入文本相关度最高的记忆。
            max_memory_length (int, optional): 每个主题最多返回的记忆条目数量。默认为2，表示每个主题最多返回2条相似度最高的记忆。
            max_depth (int, optional): 记忆检索深度。默认为3。值越大，检索范围越广，可以获取更多间接相关的记忆，但速度会变慢。

        Returns:
            list: 记忆列表，每个元素是一个元组 (topic, memory_content)
                - topic: str, 记忆主题
                - memory_content: str, 该主题下的完整记忆内容
        """
        if not keywords:
            return []

        logger.info(f"提取的关键词: {', '.join(keywords)}")

        # 过滤掉不存在于记忆图中的关键词
        valid_keywords = [keyword for keyword in keywords if keyword in self.memory_graph.G]
        if not valid_keywords:
            logger.debug("没有找到有效的关键词节点")
            return []

        logger.debug(f"有效的关键词: {', '.join(valid_keywords)}")

        # 从每个关键词获取记忆
        activate_map = {}  # 存储每个词的累计激活值

        # 对每个关键词进行扩散式检索
        for keyword in valid_keywords:
            logger.debug(f"开始以关键词 '{keyword}' 为中心进行扩散检索 (最大深度: {max_depth}):")
            # 初始化激活值
            activation_values = {keyword: 1.0}
            # 记录已访问的节点
            visited_nodes = {keyword}
            # 待处理的节点队列，每个元素是(节点, 激活值, 当前深度)
            nodes_to_process = [(keyword, 1.0, 0)]

            while nodes_to_process:
                current_node, current_activation, current_depth = nodes_to_process.pop(0)

                # 如果激活值小于0或超过最大深度，停止扩散
                if current_activation <= 0 or current_depth >= max_depth:
                    continue

                # 获取当前节点的所有邻居
                neighbors = list(self.memory_graph.G.neighbors(current_node))

                for neighbor in neighbors:
                    if neighbor in visited_nodes:
                        continue

                    # 获取连接强度
                    edge_data = self.memory_graph.G[current_node][neighbor]
                    strength = edge_data.get("strength", 1)

                    # 计算新的激活值
                    new_activation = current_activation - (1 / strength)

                    if new_activation > 0:
                        activation_values[neighbor] = new_activation
                        visited_nodes.add(neighbor)
                        nodes_to_process.append((neighbor, new_activation, current_depth + 1))
                        # logger.debug(
                        # f"节点 '{neighbor}' 被激活，激活值: {new_activation:.2f} (通过 '{current_node}' 连接，强度: {strength}, 深度: {current_depth + 1})"
                        # )  # noqa: E501

            # 更新激活映射
            for node, activation_value in activation_values.items():
                if activation_value > 0:
                    if node in activate_map:
                        activate_map[node] += activation_value
                    else:
                        activate_map[node] = activation_value

        # 基于激活值平方的独立概率选择
        remember_map = {}
        # logger.info("基于激活值平方的归一化选择:")

        # 计算所有激活值的平方和
        total_squared_activation = sum(activation**2 for activation in activate_map.values())
        if total_squared_activation > 0:
            # 计算归一化的激活值
            normalized_activations = {
                node: (activation**2) / total_squared_activation for node, activation in activate_map.items()
            }

            # 按归一化激活值排序并选择前max_memory_num个
            sorted_nodes = sorted(normalized_activations.items(), key=lambda x: x[1], reverse=True)[:max_memory_num]

            # 将选中的节点添加到remember_map
            for node, normalized_activation in sorted_nodes:
                remember_map[node] = activate_map[node]  # 使用原始激活值
                logger.debug(
                    f"节点 '{node}' (归一化激活值: {normalized_activation:.2f}, 激活值: {activate_map[node]:.2f})"
                )
        else:
            logger.info("没有有效的激活值")

        # 从选中的节点中提取记忆
        all_memories = []
        # logger.info("开始从选中的节点中提取记忆:")
        for node, activation in remember_map.items():
            logger.debug(f"处理节点 '{node}' (激活值: {activation:.2f}):")
            node_data = self.memory_graph.G.nodes[node]
            if memory_items := node_data.get("memory_items", ""):
                logger.debug("节点包含完整记忆")
                # 计算记忆与关键词的相似度
                memory_words = set(jieba.cut(memory_items))
                text_words = set(keywords)
                if all_words := memory_words | text_words:
                    # 计算相似度（虽然这里没有使用，但保持逻辑一致性）
                    v1 = [1 if word in memory_words else 0 for word in all_words]
                    v2 = [1 if word in text_words else 0 for word in all_words]
                    _ = cosine_similarity(v1, v2)  # 计算但不使用，用_表示

                    # 添加完整记忆到结果中
                    all_memories.append((node, memory_items, activation))
            else:
                logger.info("节点没有记忆")

        # 去重（基于记忆内容）
        logger.debug("开始记忆去重:")
        seen_memories = set()
        unique_memories = []
        for topic, memory_items, activation_value in all_memories:
            # memory_items现在是完整的字符串格式
            memory = memory_items or ""
            if memory not in seen_memories:
                seen_memories.add(memory)
                unique_memories.append((topic, memory_items, activation_value))
                logger.debug(f"保留记忆: {memory} (来自节点: {topic}, 激活值: {activation_value:.2f})")
            else:
                logger.debug(f"跳过重复记忆: {memory} (来自节点: {topic})")

        # 转换为(关键词, 记忆)格式
        result = []
        for topic, memory_items, _ in unique_memories:
            # memory_items现在是完整的字符串格式
            memory = memory_items or ""
            result.append((topic, memory))
            logger.debug(f"选中记忆: {memory} (来自节点: {topic})")

        return result

    async def get_activate_from_text(
        self, text: str, max_depth: int = 3, fast_retrieval: bool = False
    ) -> tuple[float, list[str], list[str]]:
        """从文本中提取关键词并获取相关记忆。

        Args:
            text (str): 输入文本
            max_depth (int, optional): 记忆检索深度。默认为2。
            fast_retrieval (bool, optional): 是否使用快速检索。默认为False。
                如果为True，使用jieba分词和TF-IDF提取关键词，速度更快但可能不够准确。
                如果为False，使用LLM提取关键词，速度较慢但更准确。

        Returns:
            float: 激活节点数与总节点数的比值
            list[str]: 有效的关键词
        """
        keywords, keywords_lite = await self.get_keywords_from_text(text)

        # 过滤掉不存在于记忆图中的关键词
        valid_keywords = [keyword for keyword in keywords if keyword in self.memory_graph.G]
        if not valid_keywords:
            # logger.info("没有找到有效的关键词节点")
            return 0, keywords, keywords_lite

        logger.debug(f"有效的关键词: {', '.join(valid_keywords)}")

        # 从每个关键词获取记忆
        activate_map = {}  # 存储每个词的累计激活值

        # 对每个关键词进行扩散式检索
        for keyword in valid_keywords:
            logger.debug(f"开始以关键词 '{keyword}' 为中心进行扩散检索 (最大深度: {max_depth}):")
            # 初始化激活值
            activation_values = {keyword: 1.5}
            # 记录已访问的节点
            visited_nodes = {keyword}
            # 待处理的节点队列，每个元素是(节点, 激活值, 当前深度)
            nodes_to_process = [(keyword, 1.0, 0)]

            while nodes_to_process:
                current_node, current_activation, current_depth = nodes_to_process.pop(0)

                # 如果激活值小于0或超过最大深度，停止扩散
                if current_activation <= 0 or current_depth >= max_depth:
                    continue

                # 获取当前节点的所有邻居
                neighbors = list(self.memory_graph.G.neighbors(current_node))

                for neighbor in neighbors:
                    if neighbor in visited_nodes:
                        continue

                    # 获取连接强度
                    edge_data = self.memory_graph.G[current_node][neighbor]
                    strength = edge_data.get("strength", 1)

                    # 计算新的激活值
                    new_activation = current_activation - (1 / strength)

                    if new_activation > 0:
                        activation_values[neighbor] = new_activation
                        visited_nodes.add(neighbor)
                        nodes_to_process.append((neighbor, new_activation, current_depth + 1))
                        # logger.debug(
                        # f"节点 '{neighbor}' 被激活，激活值: {new_activation:.2f} (通过 '{current_node}' 连接，强度: {strength}, 深度: {current_depth + 1})")  # noqa: E501

            # 更新激活映射
            for node, activation_value in activation_values.items():
                if activation_value > 0:
                    if node in activate_map:
                        activate_map[node] += activation_value
                    else:
                        activate_map[node] = activation_value

        # 输出激活映射
        # logger.info("激活映射统计:")
        # for node, total_activation in sorted(activate_map.items(), key=lambda x: x[1], reverse=True):
        #     logger.info(f"节点 '{node}': 累计激活值 = {total_activation:.2f}")

        # 计算激活节点数与总节点数的比值
        total_activation = sum(activate_map.values())
        # logger.debug(f"总激活值: {total_activation:.2f}")
        total_nodes = len(self.memory_graph.G.nodes())
        # activated_nodes = len(activate_map)
        activation_ratio = total_activation / total_nodes if total_nodes > 0 else 0
        activation_ratio = activation_ratio * 50
        logger.debug(f"总激活值: {total_activation:.2f}, 总节点数: {total_nodes}, 激活: {activation_ratio}")

        return activation_ratio, keywords, keywords_lite


# 负责海马体与其他部分的交互
class EntorhinalCortex:
    def __init__(self, hippocampus: Hippocampus):
        self.hippocampus = hippocampus
        self.memory_graph = hippocampus.memory_graph

    async def sync_memory_to_db(self):
        """将记忆图同步到数据库"""
        start_time = time.time()
        current_time = datetime.datetime.now().timestamp()

        # 获取数据库中所有节点和内存中所有节点
        db_nodes = {node.concept: node for node in GraphNodes.select()}
        memory_nodes = list(self.memory_graph.G.nodes(data=True))

        # 批量准备节点数据
        nodes_to_create = []
        nodes_to_update = []
        nodes_to_delete = set()

        # 处理节点
        for concept, data in memory_nodes:
            if not concept or not isinstance(concept, str):
                self.memory_graph.G.remove_node(concept)
                continue

            memory_items = data.get("memory_items", "")

            # 直接检查字符串是否为空，不需要分割成列表
            if not memory_items or memory_items.strip() == "":
                self.memory_graph.G.remove_node(concept)
                continue

            # 计算内存中节点的特征值
            memory_hash = self.hippocampus.calculate_node_hash(concept, memory_items)
            created_time = data.get("created_time", current_time)
            last_modified = data.get("last_modified", current_time)

            # memory_items直接作为字符串存储，不需要JSON序列化
            if not memory_items:
                continue

            # 获取权重属性
            weight = data.get("weight", 1.0)

            if concept not in db_nodes:
                nodes_to_create.append(
                    {
                        "concept": concept,
                        "memory_items": memory_items,
                        "weight": weight,
                        "hash": memory_hash,
                        "created_time": created_time,
                        "last_modified": last_modified,
                    }
                )
            else:
                db_node = db_nodes[concept]
                if db_node.hash != memory_hash:
                    nodes_to_update.append(
                        {
                            "concept": concept,
                            "memory_items": memory_items,
                            "weight": weight,
                            "hash": memory_hash,
                            "last_modified": last_modified,
                        }
                    )

        # 计算需要删除的节点
        memory_concepts = {concept for concept, _ in memory_nodes}
        nodes_to_delete = set(db_nodes.keys()) - memory_concepts

        # 批量处理节点
        if nodes_to_create:
            batch_size = 100
            for i in range(0, len(nodes_to_create), batch_size):
                batch = nodes_to_create[i : i + batch_size]
                GraphNodes.insert_many(batch).execute()

        if nodes_to_update:
            batch_size = 100
            for i in range(0, len(nodes_to_update), batch_size):
                batch = nodes_to_update[i : i + batch_size]
                for node_data in batch:
                    GraphNodes.update(**{k: v for k, v in node_data.items() if k != "concept"}).where(
                        GraphNodes.concept == node_data["concept"]
                    ).execute()

        if nodes_to_delete:
            GraphNodes.delete().where(GraphNodes.concept.in_(nodes_to_delete)).execute()  # type: ignore

        # 处理边的信息
        db_edges = list(GraphEdges.select())
        memory_edges = list(self.memory_graph.G.edges(data=True))

        # 创建边的哈希值字典
        db_edge_dict = {}
        for edge in db_edges:
            edge_hash = self.hippocampus.calculate_edge_hash(edge.source, edge.target)
            db_edge_dict[(edge.source, edge.target)] = {"hash": edge_hash, "strength": edge.strength}

        # 批量准备边数据
        edges_to_create = []
        edges_to_update = []

        # 处理边
        for source, target, data in memory_edges:
            edge_hash = self.hippocampus.calculate_edge_hash(source, target)
            edge_key = (source, target)
            strength = data.get("strength", 1)
            created_time = data.get("created_time", current_time)
            last_modified = data.get("last_modified", current_time)

            if edge_key not in db_edge_dict:
                edges_to_create.append(
                    {
                        "source": source,
                        "target": target,
                        "strength": strength,
                        "hash": edge_hash,
                        "created_time": created_time,
                        "last_modified": last_modified,
                    }
                )
            elif db_edge_dict[edge_key]["hash"] != edge_hash:
                edges_to_update.append(
                    {
                        "source": source,
                        "target": target,
                        "strength": strength,
                        "hash": edge_hash,
                        "last_modified": last_modified,
                    }
                )

        # 计算需要删除的边
        memory_edge_keys = {(source, target) for source, target, _ in memory_edges}
        edges_to_delete = set(db_edge_dict.keys()) - memory_edge_keys

        # 批量处理边
        if edges_to_create:
            batch_size = 100
            for i in range(0, len(edges_to_create), batch_size):
                batch = edges_to_create[i : i + batch_size]
                GraphEdges.insert_many(batch).execute()

        if edges_to_update:
            batch_size = 100
            for i in range(0, len(edges_to_update), batch_size):
                batch = edges_to_update[i : i + batch_size]
                for edge_data in batch:
                    GraphEdges.update(**{k: v for k, v in edge_data.items() if k not in ["source", "target"]}).where(
                        (GraphEdges.source == edge_data["source"]) & (GraphEdges.target == edge_data["target"])
                    ).execute()

        if edges_to_delete:
            for source, target in edges_to_delete:
                GraphEdges.delete().where((GraphEdges.source == source) & (GraphEdges.target == target)).execute()

        end_time = time.time()
        logger.info(f"[数据库] 同步完成，总耗时: {end_time - start_time:.2f}秒")
        logger.info(
            f"[数据库] 同步了 {len(nodes_to_create) + len(nodes_to_update)} 个节点和 {len(edges_to_create) + len(edges_to_update)} 条边"
        )

    async def resync_memory_to_db(self):
        """清空数据库并重新同步所有记忆数据"""
        start_time = time.time()
        logger.info("[数据库] 开始重新同步所有记忆数据...")

        # 清空数据库
        clear_start = time.time()
        GraphNodes.delete().execute()
        GraphEdges.delete().execute()
        clear_end = time.time()
        logger.info(f"[数据库] 清空数据库耗时: {clear_end - clear_start:.2f}秒")

        # 获取所有节点和边
        memory_nodes = list(self.memory_graph.G.nodes(data=True))
        memory_edges = list(self.memory_graph.G.edges(data=True))
        current_time = datetime.datetime.now().timestamp()

        # 批量准备节点数据
        nodes_data = []
        for concept, data in memory_nodes:
            memory_items = data.get("memory_items", "")

            # 直接检查字符串是否为空，不需要分割成列表
            if not memory_items or memory_items.strip() == "":
                self.memory_graph.G.remove_node(concept)
                continue

            # 计算内存中节点的特征值
            memory_hash = self.hippocampus.calculate_node_hash(concept, memory_items)
            created_time = data.get("created_time", current_time)
            last_modified = data.get("last_modified", current_time)

            # memory_items直接作为字符串存储，不需要JSON序列化
            if not memory_items:
                continue

            # 获取权重属性
            weight = data.get("weight", 1.0)

            nodes_data.append(
                {
                    "concept": concept,
                    "memory_items": memory_items,
                    "weight": weight,
                    "hash": memory_hash,
                    "created_time": created_time,
                    "last_modified": last_modified,
                }
            )

        # 批量插入节点
        if nodes_data:
            batch_size = 100
            for i in range(0, len(nodes_data), batch_size):
                batch = nodes_data[i : i + batch_size]
                GraphNodes.insert_many(batch).execute()

        # 批量准备边数据
        edges_data = []
        for source, target, data in memory_edges:
            try:
                edges_data.append(
                    {
                        "source": source,
                        "target": target,
                        "strength": data.get("strength", 1),
                        "hash": self.hippocampus.calculate_edge_hash(source, target),
                        "created_time": data.get("created_time", current_time),
                        "last_modified": data.get("last_modified", current_time),
                    }
                )
            except Exception as e:
                logger.error(f"准备边 {source}-{target} 数据时发生错误: {e}")
                continue

        # 批量插入边
        if edges_data:
            batch_size = 100
            for i in range(0, len(edges_data), batch_size):
                batch = edges_data[i : i + batch_size]
                GraphEdges.insert_many(batch).execute()

        end_time = time.time()
        logger.info(f"[数据库] 重新同步完成，总耗时: {end_time - start_time:.2f}秒")
        logger.info(f"[数据库] 同步了 {len(nodes_data)} 个节点和 {len(edges_data)} 条边")

    def sync_memory_from_db(self):
        """从数据库同步数据到内存中的图结构"""
        current_time = datetime.datetime.now().timestamp()
        need_update = False

        # 清空当前图
        self.memory_graph.G.clear()

        # 统计加载情况
        total_nodes = 0
        loaded_nodes = 0
        skipped_nodes = 0

        # 从数据库加载所有节点
        nodes = list(GraphNodes.select())
        total_nodes = len(nodes)

        for node in nodes:
            concept = node.concept
            try:
                # 处理空字符串或None的情况
                if not node.memory_items or node.memory_items.strip() == "":
                    logger.warning(f"节点 {concept} 的memory_items为空，跳过")
                    skipped_nodes += 1
                    continue

                # 直接使用memory_items
                memory_items = node.memory_items.strip()

                # 检查时间字段是否存在
                if not node.created_time or not node.last_modified:
                    # 更新数据库中的节点
                    update_data = {}
                    if not node.created_time:
                        update_data["created_time"] = current_time
                    if not node.last_modified:
                        update_data["last_modified"] = current_time

                    if update_data:
                        GraphNodes.update(**update_data).where(GraphNodes.concept == concept).execute()

                # 获取时间信息(如果不存在则使用当前时间)
                created_time = node.created_time or current_time
                last_modified = node.last_modified or current_time

                # 获取权重属性
                weight = node.weight if hasattr(node, "weight") and node.weight is not None else 1.0

                # 添加节点到图中
                self.memory_graph.G.add_node(
                    concept,
                    memory_items=memory_items,
                    weight=weight,
                    created_time=created_time,
                    last_modified=last_modified,
                )
                loaded_nodes += 1
            except Exception as e:
                logger.error(f"加载节点 {concept} 时发生错误: {e}")
                skipped_nodes += 1
                continue

        # 从数据库加载所有边
        edges = list(GraphEdges.select())
        for edge in edges:
            source = edge.source
            target = edge.target
            strength = edge.strength

            # 检查时间字段是否存在
            if not edge.created_time or not edge.last_modified:
                need_update = True
                # 更新数据库中的边
                update_data = {}
                if not edge.created_time:
                    update_data["created_time"] = current_time
                if not edge.last_modified:
                    update_data["last_modified"] = current_time

                GraphEdges.update(**update_data).where(
                    (GraphEdges.source == source) & (GraphEdges.target == target)
                ).execute()

            # 获取时间信息(如果不存在则使用当前时间)
            created_time = edge.created_time or current_time
            last_modified = edge.last_modified or current_time

            # 只有当源节点和目标节点都存在时才添加边
            if source in self.memory_graph.G and target in self.memory_graph.G:
                self.memory_graph.G.add_edge(
                    source, target, strength=strength, created_time=created_time, last_modified=last_modified
                )

        if need_update:
            logger.info("[数据库] 已为缺失的时间字段进行补充")

        # 输出加载统计信息
        logger.info(
            f"[数据库] 记忆加载完成: 总计 {total_nodes} 个节点, 成功加载 {loaded_nodes} 个, 跳过 {skipped_nodes} 个"
        )


# 负责整合，遗忘，合并记忆
class ParahippocampalGyrus:
    def __init__(self, hippocampus: Hippocampus):
        self.hippocampus = hippocampus
        self.memory_graph = hippocampus.memory_graph

        self.memory_modify_model = LLMRequest(
            model_set=model_config.model_task_config.utils, request_type="memory.modify"
        )

    async def memory_compress(self, messages: list[DatabaseMessages], compress_rate=0.1):
        """压缩和总结消息内容，生成记忆主题和摘要。

        Args:
            messages (list): 消息列表，每个消息是一个字典，包含数据库消息结构。
            compress_rate (float, optional): 压缩率，用于控制生成的主题数量。默认为0.1。

        Returns:
            tuple: (compressed_memory, similar_topics_dict)
                - compressed_memory: set, 压缩后的记忆集合，每个元素是一个元组 (topic, summary)
                - similar_topics_dict: dict, 相似主题字典

        Process:
            1. 使用 build_readable_messages 生成包含时间、人物信息的格式化文本。
            2. 使用LLM提取关键主题。
            3. 过滤掉包含禁用关键词的主题。
            4. 为每个主题生成摘要。
            5. 查找与现有记忆中的相似主题。
        """
        if not messages:
            return set(), {}

        # 1. 使用 build_readable_messages 生成格式化文本
        # build_readable_messages 只返回一个字符串，不需要解包
        input_text = build_readable_messages(
            messages,
            timestamp_mode="normal_no_YMD",  # 使用 'YYYY-MM-DD HH:MM:SS' 格式
            replace_bot_name=False,  # 保留原始用户名
        )

        # 如果生成的可读文本为空（例如所有消息都无效），则直接返回
        if not input_text:
            logger.warning("无法从提供的消息生成可读文本，跳过记忆压缩。")
            return set(), {}

        current_date = f"当前日期: {datetime.datetime.now().isoformat()}"
        input_text = f"{current_date}\n{input_text}"

        logger.debug(f"记忆来源:\n{input_text}")

        # 2. 使用LLM提取关键主题
        topic_num = self.hippocampus.calculate_topic_num(input_text, compress_rate)
        topics_response, _ = await self.memory_modify_model.generate_response_async(
            self.hippocampus.find_topic_llm(input_text, topic_num)
        )

        # 提取<>中的内容
        topics = re.findall(r"<([^>]+)>", topics_response)

        if not topics:
            topics = ["none"]
        else:
            topics = [
                topic.strip()
                for topic in ",".join(topics).replace("，", ",").replace("、", ",").replace(" ", ",").split(",")
                if topic.strip()
            ]

        # 3. 过滤掉包含禁用关键词的topic
        filtered_topics = [
            topic for topic in topics if all(keyword not in topic for keyword in global_config.memory.memory_ban_words)
        ]

        logger.debug(f"过滤后话题: {filtered_topics}")

        # 4. 创建所有话题的摘要生成任务
        tasks: List[Tuple[str, Coroutine[Any, Any, Tuple[str, Tuple[str, str, List | None]]]]] = []
        topic_what_prompt: str = ""
        for topic in filtered_topics:
            # 调用修改后的 topic_what，不再需要 time_info
            topic_what_prompt = self.hippocampus.topic_what(input_text, topic)
            try:
                task = self.memory_modify_model.generate_response_async(topic_what_prompt)
                tasks.append((topic.strip(), task))
            except Exception as e:
                logger.error(f"生成话题 '{topic}' 的摘要时发生错误: {e}")
                continue

        # 等待所有任务完成
        compressed_memory: Set[Tuple[str, str]] = set()
        similar_topics_dict = {}

        for topic, task in tasks:
            response = await task
            if response:
                compressed_memory.add((topic, response[0]))

                existing_topics = list(self.memory_graph.G.nodes())
                similar_topics = []

                for existing_topic in existing_topics:
                    topic_words = set(jieba.cut(topic))
                    existing_words = set(jieba.cut(existing_topic))

                    all_words = topic_words | existing_words
                    v1 = [1 if word in topic_words else 0 for word in all_words]
                    v2 = [1 if word in existing_words else 0 for word in all_words]

                    similarity = cosine_similarity(v1, v2)

                    if similarity >= 0.7:
                        similar_topics.append((existing_topic, similarity))

                similar_topics.sort(key=lambda x: x[1], reverse=True)
                similar_topics = similar_topics[:3]
                similar_topics_dict[topic] = similar_topics

        if global_config.debug.show_prompt:
            logger.info(f"prompt: {topic_what_prompt}")
            logger.info(f"压缩后的记忆: {compressed_memory}")
            logger.info(f"相似主题: {similar_topics_dict}")

        return compressed_memory, similar_topics_dict

    def get_similar_topics_from_keywords(
        self,
        keywords: list[str] | str,
        top_k: int = 3,
        threshold: float = 0.7,
    ) -> dict[str, list[tuple[str, float]]]:
        """基于输入的关键词，返回每个关键词对应的相似主题列表。

        Args:
            keywords: 关键词列表或以逗号/空格/顿号分隔的字符串。
            top_k: 每个关键词返回的相似主题数量上限。
            threshold: 相似度阈值，低于该值的主题将被过滤。

        Returns:
            dict[str, list[tuple[str, float]]]: {keyword: [(topic, similarity), ...]}
        """
        # 规范化输入为列表[str]
        if isinstance(keywords, str):
            # 支持中英文逗号、顿号、空格分隔
            parts = keywords.replace("，", ",").replace("、", ",").replace(" ", ",").strip(", ")
            keyword_list = [p.strip() for p in parts.split(",") if p.strip()]
        else:
            keyword_list = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]

        if not keyword_list:
            return {}

        existing_topics = list(self.memory_graph.G.nodes())
        result: dict[str, list[tuple[str, float]]] = {}

        for kw in keyword_list:
            kw_words = set(jieba.cut(kw))
            similar_topics: list[tuple[str, float]] = []

            for topic in existing_topics:
                topic_words = set(jieba.cut(topic))
                all_words = kw_words | topic_words
                if not all_words:
                    continue
                v1 = [1 if w in kw_words else 0 for w in all_words]
                v2 = [1 if w in topic_words else 0 for w in all_words]
                sim = cosine_similarity(v1, v2)
                if sim >= threshold:
                    similar_topics.append((topic, sim))

            similar_topics.sort(key=lambda x: x[1], reverse=True)
            result[kw] = similar_topics[:top_k]

        return result

    async def add_memory_with_similar(
        self,
        memory_item: str,
        similar_topics_dict: dict[str, list[tuple[str, float]]],
    ) -> bool:
        """将单条记忆内容与相似主题写入记忆网络并同步数据库。

        按 build_memory_for_chat 的方式：为 similar_topics_dict 的每个键作为主题添加节点内容，
        并与其相似主题建立连接，连接强度为 int(similarity * 10)。

        Args:
            memory_item: 记忆内容字符串，将作为每个主题节点的 memory_items。
            similar_topics_dict: {topic: [(similar_topic, similarity), ...]}

        Returns:
            bool: 是否成功执行添加与同步。
        """
        try:
            if not memory_item or not isinstance(memory_item, str):
                return False

            if not similar_topics_dict or not isinstance(similar_topics_dict, dict):
                return False

            current_time = time.time()

            # 为每个主题写入节点
            for topic, similar_list in similar_topics_dict.items():
                if not topic or not isinstance(topic, str):
                    continue

                await self.hippocampus.memory_graph.add_dot(topic, memory_item, self.hippocampus)

                # 连接相似主题
                if isinstance(similar_list, list):
                    for item in similar_list:
                        try:
                            similar_topic, similarity = item
                        except Exception:
                            continue
                        if not isinstance(similar_topic, str):
                            continue
                        if topic == similar_topic:
                            continue
                        # 强度按 build_memory_for_chat 的规则
                        strength = int(max(0.0, float(similarity)) * 10) if similarity is not None else 0
                        if strength <= 0:
                            continue
                        # 确保相似主题节点存在（如果没有，也可以只建立边，networkx会创建节点，但需初始化属性）
                        if similar_topic not in self.memory_graph.G:
                            # 创建一个空的相似主题节点，避免悬空边，memory_items 为空字符串
                            self.memory_graph.G.add_node(
                                similar_topic,
                                memory_items="",
                                weight=1.0,
                                created_time=current_time,
                                last_modified=current_time,
                            )
                        self.memory_graph.G.add_edge(
                            topic,
                            similar_topic,
                            strength=strength,
                            created_time=current_time,
                            last_modified=current_time,
                        )

            # 同步数据库
            await self.hippocampus.entorhinal_cortex.sync_memory_to_db()
            return True
        except Exception as e:
            logger.error(f"添加记忆节点失败: {e}")
            return False

    async def operation_forget_topic(self, percentage=0.005):
        start_time = time.time()
        logger.info("[遗忘] 开始检查数据库...")

        # 验证百分比参数
        if not 0 <= percentage <= 1:
            logger.warning(f"[遗忘] 无效的遗忘百分比: {percentage}, 使用默认值 0.005")
            percentage = 0.005

        all_nodes = list(self.memory_graph.G.nodes())
        all_edges = list(self.memory_graph.G.edges())

        if not all_nodes and not all_edges:
            logger.info("[遗忘] 记忆图为空,无需进行遗忘操作")
            return

        # 确保至少检查1个节点和边，且不超过总数
        check_nodes_count = max(1, min(len(all_nodes), int(len(all_nodes) * percentage)))
        check_edges_count = max(1, min(len(all_edges), int(len(all_edges) * percentage)))

        # 只有在有足够的节点和边时才进行采样
        if len(all_nodes) >= check_nodes_count and len(all_edges) >= check_edges_count:
            try:
                nodes_to_check = random.sample(all_nodes, check_nodes_count)
                edges_to_check = random.sample(all_edges, check_edges_count)
            except ValueError as e:
                logger.error(f"[遗忘] 采样错误: {str(e)}")
                return
        else:
            logger.info("[遗忘] 没有足够的节点或边进行遗忘操作")
            return

        # 使用列表存储变化信息
        edge_changes = {
            "weakened": [],  # 存储减弱的边
            "removed": [],  # 存储移除的边
        }
        node_changes = {
            "reduced": [],  # 存储减少记忆的节点
            "removed": [],  # 存储移除的节点
        }

        current_time = datetime.datetime.now().timestamp()

        logger.info("[遗忘] 开始检查连接...")
        edge_check_start = time.time()
        for source, target in edges_to_check:
            edge_data = self.memory_graph.G[source][target]
            last_modified = edge_data.get("last_modified")

            if current_time - last_modified > 3600 * global_config.memory.memory_forget_time:
                current_strength = edge_data.get("strength", 1)
                new_strength = current_strength - 1

                if new_strength <= 0:
                    self.memory_graph.G.remove_edge(source, target)
                    edge_changes["removed"].append(f"{source} -> {target}")
                else:
                    edge_data["strength"] = new_strength
                    edge_data["last_modified"] = current_time
                    edge_changes["weakened"].append(f"{source}-{target} (强度: {current_strength} -> {new_strength})")
        edge_check_end = time.time()
        logger.info(f"[遗忘] 连接检查耗时: {edge_check_end - edge_check_start:.2f}秒")

        logger.info("[遗忘] 开始检查节点...")
        node_check_start = time.time()
        for node in nodes_to_check:
            # 检查节点是否存在，以防在迭代中被移除（例如边移除导致）
            if node not in self.memory_graph.G:
                continue

            node_data = self.memory_graph.G.nodes[node]

            # 首先获取记忆项
            memory_items = node_data.get("memory_items", "")
            # 直接检查记忆内容是否为空
            if not memory_items or memory_items.strip() == "":
                try:
                    self.memory_graph.G.remove_node(node)
                    node_changes["removed"].append(f"{node}(空节点)")  # 标记为空节点移除
                    logger.debug(f"[遗忘] 移除了空的节点: {node}")
                except nx.NetworkXError as e:
                    logger.warning(f"[遗忘] 移除空节点 {node} 时发生错误（可能已被移除）: {e}")
                continue  # 处理下一个节点

            # --- 如果节点不为空，则执行原来的不活跃检查和随机移除逻辑 ---
            last_modified = node_data.get("last_modified", current_time)
            node_weight = node_data.get("weight", 1.0)

            # 条件1：检查是否长时间未修改 (使用配置的遗忘时间)
            time_threshold = 3600 * global_config.memory.memory_forget_time

            # 基于权重调整遗忘阈值：权重越高，需要更长时间才能被遗忘
            # 权重为1时使用默认阈值，权重越高阈值越大（越难遗忘）
            adjusted_threshold = time_threshold * node_weight

            if current_time - last_modified > adjusted_threshold and memory_items:
                # 既然每个节点现在是完整记忆，直接删除整个节点
                try:
                    self.memory_graph.G.remove_node(node)
                    node_changes["removed"].append(f"{node}(长时间未修改,权重{node_weight:.1f})")
                    logger.debug(f"[遗忘] 移除了长时间未修改的节点: {node} (权重: {node_weight:.1f})")
                except nx.NetworkXError as e:
                    logger.warning(f"[遗忘] 移除节点 {node} 时发生错误（可能已被移除）: {e}")
                    continue
        node_check_end = time.time()
        logger.info(f"[遗忘] 节点检查耗时: {node_check_end - node_check_start:.2f}秒")

        if any(edge_changes.values()) or any(node_changes.values()):
            sync_start = time.time()

            await self.hippocampus.entorhinal_cortex.resync_memory_to_db()

            sync_end = time.time()
            logger.info(f"[遗忘] 数据库同步耗时: {sync_end - sync_start:.2f}秒")

            # 汇总输出所有变化
            logger.info("[遗忘] 遗忘操作统计:")
            if edge_changes["weakened"]:
                logger.info(
                    f"[遗忘] 减弱的连接 ({len(edge_changes['weakened'])}个): {', '.join(edge_changes['weakened'])}"
                )

            if edge_changes["removed"]:
                logger.info(
                    f"[遗忘] 移除的连接 ({len(edge_changes['removed'])}个): {', '.join(edge_changes['removed'])}"
                )

            if node_changes["reduced"]:
                logger.info(
                    f"[遗忘] 减少记忆的节点 ({len(node_changes['reduced'])}个): {', '.join(node_changes['reduced'])}"
                )

            if node_changes["removed"]:
                logger.info(
                    f"[遗忘] 移除的节点 ({len(node_changes['removed'])}个): {', '.join(node_changes['removed'])}"
                )
        else:
            logger.info("[遗忘] 本次检查没有节点或连接满足遗忘条件")

        end_time = time.time()
        logger.info(f"[遗忘] 总耗时: {end_time - start_time:.2f}秒")


class HippocampusManager:
    def __init__(self):
        self._hippocampus: Hippocampus = None  # type: ignore
        self._initialized = False

    def initialize(self):
        """初始化海马体实例"""
        if self._initialized:
            return self._hippocampus

        self._hippocampus = Hippocampus()
        self._hippocampus.initialize()
        self._initialized = True

        # 输出记忆图统计信息
        memory_graph = self._hippocampus.memory_graph.G
        node_count = len(memory_graph.nodes())
        edge_count = len(memory_graph.edges())

        logger.info(f"""
                    --------------------------------
                    记忆系统参数配置:
                    遗忘间隔: {global_config.memory.forget_memory_interval}秒|遗忘比例: {global_config.memory.memory_forget_percentage}|遗忘: {global_config.memory.memory_forget_time}小时之后
                    记忆图统计信息: 节点数量: {node_count}, 连接数量: {edge_count}
                    --------------------------------""")  # noqa: E501

        return self._hippocampus

    def get_hippocampus(self):
        if not self._initialized:
            raise RuntimeError("HippocampusManager 尚未初始化，请先调用 initialize 方法")
        return self._hippocampus

    async def forget_memory(self, percentage: float = 0.005):
        """遗忘记忆的公共接口"""
        if not self._initialized:
            raise RuntimeError("HippocampusManager 尚未初始化，请先调用 initialize 方法")
        return await self._hippocampus.parahippocampal_gyrus.operation_forget_topic(percentage)

    async def get_memory_from_topic(
        self, valid_keywords: list[str], max_memory_num: int = 3, max_memory_length: int = 2, max_depth: int = 3
    ) -> list:
        """从文本中获取相关记忆的公共接口"""
        if not self._initialized:
            raise RuntimeError("HippocampusManager 尚未初始化，请先调用 initialize 方法")
        try:
            response = await self._hippocampus.get_memory_from_topic(
                valid_keywords, max_memory_num, max_memory_length, max_depth
            )
        except Exception as e:
            logger.error(f"文本激活记忆失败: {e}")
            response = []
        return response

    async def get_activate_from_text(
        self, text: str, max_depth: int = 3, fast_retrieval: bool = False
    ) -> tuple[float, list[str], list[str]]:
        """从文本中获取激活值的公共接口"""
        if not self._initialized:
            raise RuntimeError("HippocampusManager 尚未初始化，请先调用 initialize 方法")
        try:
            return await self._hippocampus.get_activate_from_text(text, max_depth, fast_retrieval)
        except Exception as e:
            logger.error(f"文本产生激活值失败: {e}")
            logger.error(traceback.format_exc())
            return 0.0, [], []

    def get_memory_from_keyword(self, keyword: str, max_depth: int = 2) -> list:
        """从关键词获取相关记忆的公共接口"""
        if not self._initialized:
            raise RuntimeError("HippocampusManager 尚未初始化，请先调用 initialize 方法")
        return self._hippocampus.get_memory_from_keyword(keyword, max_depth)

    def get_all_node_names(self) -> list:
        """获取所有节点名称的公共接口"""
        if not self._initialized:
            raise RuntimeError("HippocampusManager 尚未初始化，请先调用 initialize 方法")
        return self._hippocampus.get_all_node_names()


# 创建全局实例
hippocampus_manager = HippocampusManager()
