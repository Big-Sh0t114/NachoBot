import json
import time
import random
import hashlib
import asyncio
import os

from typing import List, Dict, Optional, Any, Tuple
from json_repair import repair_json

from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config, model_config
from src.common.logger import get_logger
from src.common.database.database_model import Expression
from src.common.database.database import db
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.utils.utils import get_embedding, cosine_similarity

logger = get_logger("expression_selector")

# SQLite OperationalError 重试参数
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.3  # 秒


def init_prompt():
    expression_evaluation_prompt = """
[任务说明]
你现在是一个内部评估系统，负责从备选列表中选择最符合当前语境的"表达情境"。
注意：本任务【不是】聊天回复生成！请【绝对不要】输出任何回复内容（reply）、情绪（emotion）等字段。你的唯一任务是输出一个包含情境编号的JSON。

[聊天上下文]
{chat_observe_info}

[上下文补充]
机器人的名字是{bot_name}
{target_message}

[可选的表达情境]
{all_situations}

[选择要求]
请你分析聊天内容的语境、情绪、话题类型，从上述情境中选择最适合当前聊天情境的，最多{max_num}个情境。
考虑因素包括：
1. 聊天的情绪氛围（轻松、严肃、幽默等）
2. 话题类型（日常、技术、游戏、情感等）
3. 情境与当前语境的匹配度
{target_message_extra_block}

[输出格式]
请严格以JSON格式输出，只需要选中的情境编号数组。不要包含任何markdown代码块（如```json）、不要包含任何其他字段（不要reply！）。
例如：
{{
    "selected_situations": [2, 3, 5, 7]
}}
"""
    Prompt(expression_evaluation_prompt, "expression_evaluation_prompt")


def weighted_sample(population: List[Dict], weights: List[float], k: int) -> List[Dict]:
    """按权重随机抽样"""
    if not population or not weights or k <= 0:
        return []

    if len(population) <= k:
        return population.copy()

    # 使用累积权重的方法进行加权抽样
    selected = []
    population_copy = population.copy()
    weights_copy = weights.copy()

    for _ in range(k):
        if not population_copy:
            break

        # 选择一个元素
        chosen_idx = random.choices(range(len(population_copy)), weights=weights_copy)[0]
        selected.append(population_copy.pop(chosen_idx))
        weights_copy.pop(chosen_idx)

    return selected


def _retry_on_locked(func):
    """装饰器：对 SQLite OperationalError (database is locked) 进行重试"""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if "database is locked" in str(e) or "locked" in str(e).lower():
                    last_exc = e
                    delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.1)
                    logger.warning(
                        f"SQLite locked (attempt {attempt + 1}/{_MAX_RETRIES}), "
                        f"retrying in {delay:.2f}s: {e}"
                    )
                    time.sleep(delay)
                else:
                    raise
        logger.error(f"SQLite locked after {_MAX_RETRIES} retries, giving up: {last_exc}")
        raise last_exc  # type: ignore

    return wrapper


class EmbeddingCache:
    """Embedding 向量缓存，使用 dirty flag 延迟批量写入，避免每次 set 都写磁盘"""

    def __init__(self, cache_file="data/expressions/embedding_cache.json"):
        self.cache_file = cache_file
        self.cache: Dict[str, Any] = {}
        self._dirty = False
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    self.cache = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load embedding cache: {e}")

    def save_if_dirty(self):
        """仅在有未写入变更时才保存到磁盘"""
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False)
            self._dirty = False
        except Exception as e:
            logger.error(f"Failed to save embedding cache: {e}")

    def get(self, key):
        return self.cache.get(str(key))

    def set(self, key, vector):
        self.cache[str(key)] = vector
        self._dirty = True
        # 不再立即写盘，由调用方在合适时机调用 save_if_dirty()


class ExpressionSelector:
    def __init__(self):
        self.llm_model = LLMRequest(
            model_set=model_config.model_task_config.utils_small, request_type="expression.selector"
        )
        self.embedding_cache = EmbeddingCache()
        self._sync_task_started = False
        self._sync_lock = None
        # 保护 SQLite 写操作的异步锁，防止并发写入触发 database is locked
        self._db_write_lock = None

    async def _get_db_write_lock(self) -> asyncio.Lock:
        """惰性初始化 DB 写锁（必须在事件循环内创建）"""
        if self._db_write_lock is None:
            self._db_write_lock = asyncio.Lock()
        return self._db_write_lock

    async def _start_sync_task_if_needed(self):
        if not self._sync_task_started:
            if self._sync_lock is None:
                self._sync_lock = asyncio.Lock()
            async with self._sync_lock:
                if not self._sync_task_started:
                    asyncio.create_task(self._sync_embeddings_task())
                    self._sync_task_started = True

    async def _sync_embeddings_task(self):
        while True:
            try:
                # ===== 先一次性读出所有需要的信息，立即释放 DB 连接 =====
                expr_data_list = []
                try:
                    query = Expression.select(
                        Expression.id, Expression.situation, Expression.style
                    )
                    expr_data_list = [
                        {"id": expr.id, "situation": expr.situation, "style": expr.style}
                        for expr in query
                    ]
                except Exception as e:
                    logger.error(f"Embedding sync: failed to read expressions: {e}")

                # ===== DB 连接已释放，逐个生成 embedding =====
                new_embeddings = 0
                for expr_data in expr_data_list:
                    if not self.embedding_cache.get(expr_data["id"]):
                        text = f"当 {expr_data['situation']} 时，使用 {expr_data['style']}"
                        vector = await get_embedding(text)
                        if vector:
                            self.embedding_cache.set(expr_data["id"], vector)
                            new_embeddings += 1
                            await asyncio.sleep(0.5)  # 避免触发频率限制

                # 批量写入缓存文件
                if new_embeddings > 0:
                    self.embedding_cache.save_if_dirty()
                    logger.debug(f"Embedding sync: cached {new_embeddings} new embeddings")

            except Exception as e:
                logger.error(f"Embedding sync task error: {e}")

            await asyncio.sleep(300)  # 每5分钟检查一次

    def can_use_expression_for_chat(self, chat_id: str) -> bool:
        """
        检查指定聊天流是否允许使用表达

        Args:
            chat_id: 聊天流ID

        Returns:
            bool: 是否允许使用表达
        """
        try:
            use_expression, _, _ = global_config.expression.get_expression_config_for_chat(chat_id)
            return use_expression
        except Exception as e:
            logger.error(f"检查表达使用权限失败: {e}")
            return False

    @staticmethod
    def _parse_stream_config_to_chat_id(stream_config_str: str) -> Optional[str]:
        """解析'platform:id:type'为chat_id（与get_stream_id一致）"""
        try:
            parts = stream_config_str.split(":")
            if len(parts) != 3:
                return None
            platform = parts[0]
            id_str = parts[1]
            stream_type = parts[2]
            is_group = stream_type == "group"
            if is_group:
                components = [platform, str(id_str)]
            else:
                components = [platform, str(id_str), "private"]
            key = "_".join(components)
            return hashlib.md5(key.encode()).hexdigest()
        except Exception:
            return None

    def get_related_chat_ids(self, chat_id: str) -> List[str]:
        """根据expression_groups配置，获取与当前chat_id相关的所有chat_id（包括自身）"""
        groups = global_config.expression.expression_groups
        
        # 检查是否存在全局共享组（包含"*"的组）
        global_group_exists = any("*" in group for group in groups)
        
        if global_group_exists:
            # 如果存在全局共享组，则返回所有可用的chat_id
            all_chat_ids = set()
            for group in groups:
                for stream_config_str in group:
                    if chat_id_candidate := self._parse_stream_config_to_chat_id(stream_config_str):
                        all_chat_ids.add(chat_id_candidate)
            return list(all_chat_ids) if all_chat_ids else [chat_id]
        
        # 否则使用现有的组逻辑
        for group in groups:
            group_chat_ids = []
            for stream_config_str in group:
                if chat_id_candidate := self._parse_stream_config_to_chat_id(stream_config_str):
                    group_chat_ids.append(chat_id_candidate)
            if chat_id in group_chat_ids:
                return group_chat_ids
        return [chat_id]

    def get_all_style_expressions(self, chat_id: str) -> List[Dict[str, Any]]:
        """获取当前聊天流所有相关的表达方式"""
        related_chat_ids = self.get_related_chat_ids(chat_id)
        style_query = Expression.select().where(
            (Expression.chat_id.in_(related_chat_ids)) & (Expression.type == "style")
        )
        return [
            {
                "id": expr.id,
                "situation": expr.situation,
                "style": expr.style,
                "count": expr.count,
                "last_active_time": expr.last_active_time,
                "source_id": expr.chat_id,
                "type": "style",
                "create_date": expr.create_date if expr.create_date is not None else expr.last_active_time,
            }
            for expr in style_query
        ]

    def get_random_expressions(self, chat_id: str, total_num: int) -> List[Dict[str, Any]]:
        # sourcery skip: extract-duplicate-method, move-assign
        # 支持多chat_id合并抽选
        related_chat_ids = self.get_related_chat_ids(chat_id)

        # 优化：一次性查询所有相关chat_id的表达方式
        style_query = Expression.select().where(
            (Expression.chat_id.in_(related_chat_ids)) & (Expression.type == "style")
        )

        style_exprs = [
            {
                "id": expr.id,
                "situation": expr.situation,
                "style": expr.style,
                "count": expr.count,
                "last_active_time": expr.last_active_time,
                "source_id": expr.chat_id,
                "type": "style",
                "create_date": expr.create_date if expr.create_date is not None else expr.last_active_time,
            }
            for expr in style_query
        ]

        # 按权重抽样（使用count作为权重）
        if style_exprs:
            style_weights = [expr.get("count", 1) for expr in style_exprs]
            selected_style = weighted_sample(style_exprs, style_weights, total_num)
        else:
            selected_style = []
        return selected_style

    @_retry_on_locked
    def _do_update_expressions_in_db(self, updates_by_key: Dict, increment: float):
        """在 db.atomic() 事务中执行实际的 DB 读写，带重试"""
        to_update = []
        with db.atomic():
            for chat_id, expr_type, situation, style in updates_by_key:
                query = Expression.select().where(
                    (Expression.chat_id == chat_id)
                    & (Expression.type == expr_type)
                    & (Expression.situation == situation)
                    & (Expression.style == style)
                )
                if query.exists():
                    expr_obj = query.get()
                    current_count = expr_obj.count
                    new_count = min(current_count + increment, 5.0)
                    expr_obj.count = new_count
                    expr_obj.last_active_time = time.time()
                    to_update.append(expr_obj)
                    logger.debug(
                        f"表达方式激活: 原count={current_count:.3f}, 增量={increment}, 新count={new_count:.3f}"
                    )

            # 单次批量写入，而不是逐条 save()
            if to_update:
                Expression.bulk_update(to_update, fields=[Expression.count, Expression.last_active_time])
                logger.debug(f"批量更新了 {len(to_update)} 条表达方式记录")

    async def update_expressions_count_batch(self, expressions_to_update: List[Dict[str, Any]], increment: float = 0.1):
        """对一批表达方式更新count值，通过异步锁串行化写入，避免并发锁冲突"""
        if not expressions_to_update:
            return

        updates_by_key = {}
        for expr in expressions_to_update:
            source_id: str = expr.get("source_id")  # type: ignore
            expr_type: str = expr.get("type", "style")
            situation: str = expr.get("situation")  # type: ignore
            style: str = expr.get("style")  # type: ignore
            if not source_id or not situation or not style:
                logger.warning(f"表达方式缺少必要字段，无法更新: {expr}")
                continue
            key = (source_id, expr_type, situation, style)
            if key not in updates_by_key:
                updates_by_key[key] = expr

        if not updates_by_key:
            return

        # 通过异步锁保证同一时刻只有一个协程在写 Expression 表
        lock = await self._get_db_write_lock()
        async with lock:
            # 在线程池中执行同步 DB 操作，避免阻塞事件循环
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._do_update_expressions_in_db, updates_by_key, increment
            )

    async def select_suitable_expressions_llm(
        self,
        chat_id: str,
        chat_info: str,
        max_num: int = 10,
        target_message: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[int]]:
        # sourcery skip: inline-variable, list-comprehension
        """使用LLM选择适合的表达方式"""

        # 检查是否允许在此聊天流中使用表达
        if not self.can_use_expression_for_chat(chat_id):
            logger.debug(f"聊天流 {chat_id} 不允许使用表达，返回空列表")
            return [], []

        await self._start_sync_task_if_needed()

        # 获取所有表达方式进行判断
        all_style_exprs = self.get_all_style_expressions(chat_id)

        if len(all_style_exprs) < 10:
            logger.info(f"聊天流 {chat_id} 表达方式正在积累中")
            return [], []

        # 0. 优先尝试Embedding向量匹配
        query_vector = None
        if target_message or chat_info:
            # 构建查询文本
            query_text = target_message if target_message else chat_info
            # 限制查询文本长度，避免超出embedding token限制
            if len(query_text) > 800:
                query_text = query_text[-800:]
            query_vector = await get_embedding(query_text)
            
        if query_vector:
            similarities = []
            for expr in all_style_exprs:
                expr_vector = self.embedding_cache.get(expr["id"])
                if expr_vector:
                    sim = cosine_similarity(query_vector, expr_vector)
                    if sim >= 0.75:  # 设定的相似度匹配阈值
                        similarities.append((sim, expr))
            
            if similarities:
                # 按相似度降序排序
                similarities.sort(key=lambda x: x[0], reverse=True)
                top_exprs = [x[1] for x in similarities[:max_num]]
                selected_ids = [expr["id"] for expr in top_exprs]
                
                logger.debug(f"通过Embedding匹配到 {len(top_exprs)} 个表达情境，最高相似度 {similarities[0][0]:.3f}")
                await self.update_expressions_count_batch(top_exprs, 0.006)
                return top_exprs, selected_ids

        # 1. 如果匹配不到（或向量生成失败），回退到LLM逻辑并带权重随机抽样
        style_exprs = self.get_random_expressions(chat_id, 20)

        # 2. 构建所有表达方式的索引和情境列表
        all_expressions: List[Dict[str, Any]] = []
        all_situations: List[str] = []

        # 添加style表达方式
        for expr in style_exprs:
            expr = expr.copy()
            all_expressions.append(expr)
            all_situations.append(f"{len(all_expressions)}.当 {expr['situation']} 时，使用 {expr['style']}")

        if not all_expressions:
            logger.warning("没有找到可用的表达方式")
            return [], []

        all_situations_str = "\n".join(all_situations)

        if target_message:
            target_message_str = f"目标回复内容参考：{target_message}"
            target_message_extra_block = "4.考虑你要回复的目标消息"
        else:
            target_message_str = ""
            target_message_extra_block = ""

        # 3. 构建prompt（只包含情境，不包含完整的表达方式）
        prompt = (await global_prompt_manager.get_prompt_async("expression_evaluation_prompt")).format(
            bot_name=global_config.bot.nickname,
            chat_observe_info=chat_info,
            all_situations=all_situations_str,
            max_num=max_num,
            target_message=target_message_str,
            target_message_extra_block=target_message_extra_block,
        )

        # 4. 调用LLM
        try:
            # start_time = time.time()
            content, (reasoning_content, model_name, _) = await self.llm_model.generate_response_async(prompt=prompt)
            # logger.info(f"LLM请求时间: {model_name}  {time.time() - start_time} \n{prompt}")

            # logger.info(f"模型名称: {model_name}")
            # logger.info(f"LLM返回结果: {content}")
            # if reasoning_content:
            #     logger.info(f"LLM推理: {reasoning_content}")
            # else:
            #     logger.info(f"LLM推理: 无")

            if not content:
                logger.warning("LLM返回空结果")
                return [], []

            # 5. 解析结果
            result = repair_json(content)
            if isinstance(result, str):
                result = json.loads(result)

            if isinstance(result, list):
                selected_indices = result
            elif isinstance(result, dict) and "selected_situations" in result:
                selected_indices = result["selected_situations"]
            else:
                logger.error("LLM返回格式错误")
                logger.info(f"LLM返回结果: \n{content}")
                return [], []

            # 根据索引获取完整的表达方式
            valid_expressions: List[Dict[str, Any]] = []
            selected_ids = []
            for idx in selected_indices:
                if isinstance(idx, int) and 1 <= idx <= len(all_expressions):
                    expression = all_expressions[idx - 1]  # 索引从1开始
                    selected_ids.append(expression["id"])
                    valid_expressions.append(expression)

            # 对选中的所有表达方式，一次性更新count数
            if valid_expressions:
                await self.update_expressions_count_batch(valid_expressions, 0.006)

            # logger.info(f"LLM从{len(all_expressions)}个情境中选择了{len(valid_expressions)}个")
            return valid_expressions, selected_ids

        except Exception as e:
            logger.error(f"LLM处理表达方式选择时出错: {e}")
            return [], []


init_prompt()

try:
    expression_selector = ExpressionSelector()
except Exception as e:
    logger.error(f"ExpressionSelector初始化失败: {e}")
