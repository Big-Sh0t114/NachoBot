"""
中期记忆核心模块
负责摘要生成、插入和召回逻辑（Phase 2.5: 分级召回 + 智能触发 + 质量校验）
"""

import time
import math
import uuid
from collections import OrderedDict
from typing import List, Optional, Tuple, TYPE_CHECKING
from datetime import datetime

from src.common.logger import get_logger
from src.memory_system.mid_term_memory_config import get_mid_term_memory_config
from src.memory_system.mid_term_memory_store import (
    MidTermMemoryStore,
    MidTermMemorySummary,
    RecallCue,
)
from src.memory_system.mid_term_memory_embedding import (
    get_embedding_service,
    cosine_similarity,
)
from src.memory_system.mid_term_memory_response import _parse_summary_response
from src.plugin_system.apis import message_api, llm_api
from src.config.config import model_config

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages

logger = get_logger("mid_term_memory")

class MidTermMemoryManager:
    """中期记忆管理器（每个 chat_id 一个实例）"""

    def __init__(self, chat_id: str):
        """
        初始化中期记忆管理器

        Args:
            chat_id: 聊天ID
        """
        self.chat_id = chat_id
        self.config = get_mid_term_memory_config()
        self.store = MidTermMemoryStore(self.config.storage_dir)

        # 加载已有的摘要，并立即应用淘汰策略（数量上限 + TTL），
        # 避免历史遗留的超量/过期摘要进入内存
        loaded = self.store.load_summaries(chat_id)
        pruned = self.store.prune_summaries(
            loaded, self.config.max_summaries, self.config.summary_ttl_seconds
        )
        self.summaries: List[MidTermMemorySummary] = pruned
        # 若加载后发生了裁剪，回写文件保持一致
        if len(pruned) != len(loaded):
            self.store.save_summaries(chat_id, pruned)
            logger.info(f"[{chat_id}] 初始化清理：{len(loaded)} -> {len(pruned)} 条摘要")

        self.last_summary_end_time: float = (
            max((s.time_range_end for s in self.summaries), default=0.0)
        )

        # 上次"实际生成"摘要的墙钟时间戳，用于冷却控制（避免频繁 LLM 调用）
        # 用已有摘要中最新的 created_at 初始化，使重启后冷却仍然生效
        self.last_summary_build_time: float = (
            max((s.created_at for s in self.summaries), default=0.0)
        )

        # 追赶模式标记：重启后首次调用时检测是否有大量积压
        self._catchup_done: bool = False

        logger.info(
            f"[{chat_id}] 中期记忆管理器初始化完成，"
            f"已加载 {len(self.summaries)} 条摘要，"
            f"最后摘要时间: {self.last_summary_end_time}"
        )

    async def maybe_build_summary(
        self,
        current_window_oldest_time: float,
    ) -> Optional[str]:
        """
        检查是否需要生成新的中期记忆摘要
        支持重启后批量追赶模式：一次性处理多批积压消息

        Args:
            current_window_oldest_time: 当前上下文窗口中最旧消息的时间戳

        Returns:
            如果生成了新摘要，返回摘要文本；否则返回 None
        """
        if not self.config.enabled:
            return None

        # 批量追赶模式：重启后首次调用，跳过冷却检查，连续处理积压
        if not self._catchup_done:
            self._catchup_done = True
            result = await self._catchup_build(current_window_oldest_time)
            if result:
                return result

        # 正常模式：冷却检查
        interval = self.config.min_summary_interval_seconds
        if interval and interval > 0 and self.last_summary_build_time > 0:
            since_last = time.time() - self.last_summary_build_time
            if since_last < interval:
                logger.debug(
                    f"[{self.chat_id}] 距上次生成摘要仅 {since_last:.0f}s (< {interval:.0f}s)，冷却中，跳过"
                )
                return None

        return await self._try_build_one_summary(current_window_oldest_time)

    async def _catchup_build(self, current_window_oldest_time: float) -> Optional[str]:
        """
        重启后批量追赶：连续处理多批积压消息（不受冷却限制）

        Returns:
            最后一批生成的摘要文本，或 None
        """
        last_result = None
        batches_built = 0
        max_batches = self.config.max_catchup_batches

        for _ in range(max_batches):
            result = await self._try_build_one_summary(current_window_oldest_time)
            if result is None:
                break
            last_result = result
            batches_built += 1

        if batches_built > 0:
            logger.info(
                f"[{self.chat_id}] 追赶模式完成，连续生成了 {batches_built} 批摘要"
            )

        return last_result

    async def _try_build_one_summary(self, current_window_oldest_time: float) -> Optional[str]:
        """
        尝试生成一批摘要（单次逻辑，提取自原 maybe_build_summary）

        Returns:
            生成的摘要文本，或 None
        """
        # 如果窗口最旧消息时间 <= 上次摘要结束时间，说明没有"滑过"的消息
        if current_window_oldest_time <= self.last_summary_end_time:
            return None

        # 检查时间跨度是否足够
        time_gap = current_window_oldest_time - self.last_summary_end_time
        if time_gap < self.config.min_time_gap_for_summary:
            logger.debug(
                f"[{self.chat_id}] 时间跨度不足 ({time_gap:.1f}s < {self.config.min_time_gap_for_summary}s)，跳过摘要生成"
            )
            return None

        # 查询被"滑过"的消息（从上次摘要结束时间到窗口最旧消息时间）
        try:
            gap_messages = message_api.get_messages_by_time_in_chat(
                chat_id=self.chat_id,
                start_time=self.last_summary_end_time,
                end_time=current_window_oldest_time,
                limit=self.config.max_messages_per_summary,
                limit_mode="latest",
            )

            if len(gap_messages) < self.config.min_messages_for_summary:
                logger.debug(
                    f"[{self.chat_id}] 消息数量不足 ({len(gap_messages)} < {self.config.min_messages_for_summary})，跳过摘要生成"
                )
                return None

            # 如果消息数量达到上限，记录警告
            if len(gap_messages) >= self.config.max_messages_per_summary:
                logger.warning(
                    f"[{self.chat_id}] 检测到 {len(gap_messages)} 条消息（已达上限），"
                    f"只处理最近的 {self.config.max_messages_per_summary} 条。"
                    f"时间范围: {self._format_timestamp(self.last_summary_end_time)} ~ "
                    f"{self._format_timestamp(current_window_oldest_time)}"
                )
            else:
                logger.info(f"[{self.chat_id}] 检测到 {len(gap_messages)} 条被滑过的消息，开始生成中期记忆摘要")

            # 生成摘要（含质量校验和重试）
            summary = await self._generate_summary_with_retry(gap_messages)
            if summary:
                # 保存摘要
                self.summaries = self.store.add_summary(
                    self.chat_id,
                    summary,
                    self.config.max_summaries,
                    self.config.summary_ttl_seconds,
                )
                self.last_summary_end_time = summary.time_range_end
                self.last_summary_build_time = time.time()

                logger.info(
                    f"[{self.chat_id}] 成功生成中期记忆摘要 (ID: {summary.summary_id}), "
                    f"时间范围: {self._format_timestamp(summary.time_range_start)} ~ {self._format_timestamp(summary.time_range_end)}"
                )
                return summary.summary

        except Exception as e:
            logger.error(f"[{self.chat_id}] 生成中期记忆摘要失败: {e}", exc_info=True)

        return None

    async def _generate_summary_with_retry(
        self,
        messages: List["DatabaseMessages"],
    ) -> Optional[MidTermMemorySummary]:
        """
        带重试和质量校验的摘要生成

        Args:
            messages: 需要摘要的消息列表

        Returns:
            生成的摘要对象，失败则返回 None
        """
        max_retries = self.config.llm_max_retries
        for attempt in range(max_retries):
            summary = await self._generate_summary(messages)
            if summary is None:
                continue

            # 质量校验
            if len(summary.summary) < self.config.min_summary_length:
                logger.warning(
                    f"[{self.chat_id}] 摘要过短 ({len(summary.summary)} < {self.config.min_summary_length})，"
                    f"第 {attempt + 1}/{max_retries} 次尝试"
                )
                continue

            if len(summary.recall_cues) < self.config.min_recall_cues:
                logger.warning(
                    f"[{self.chat_id}] recall_cues 不足 ({len(summary.recall_cues)} < {self.config.min_recall_cues})，"
                    f"第 {attempt + 1}/{max_retries} 次尝试"
                )
                continue

            # 通过校验
            return summary

        logger.error(f"[{self.chat_id}] 摘要生成在 {max_retries} 次尝试后仍未通过质量校验")
        return None

    async def _generate_summary(
        self,
        messages: List["DatabaseMessages"],
    ) -> Optional[MidTermMemorySummary]:
        """
        使用 LLM 生成消息摘要

        Args:
            messages: 需要摘要的消息列表

        Returns:
            生成的摘要对象，失败则返回 None
        """
        if not messages:
            return None

        # 提取时间范围
        time_range_start = messages[0].time
        time_range_end = messages[-1].time

        # 收集参与者
        participants = list(set(msg.user_info.user_nickname for msg in messages if msg.user_info))

        # 构建消息文本
        message_text = self._build_message_text(messages)

        # 截断过长的输入
        if len(message_text) > self.config.max_summary_input_chars:
            message_text = message_text[: self.config.max_summary_input_chars] + "\n... (内容过长已截断)"

        # 构建摘要 Prompt
        prompt = self._build_summary_prompt(time_range_start, time_range_end, participants, message_text)

        try:
            # 调用 LLM 生成摘要
            # 使用 utils 模型配置（通用工具模型）
            utils_model_config = model_config.model_task_config.utils

            success, response, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=utils_model_config,
                request_type="mid_term_memory.summary",
                temperature=self.config.llm_temperature,
                max_tokens=self.config.llm_max_tokens,
            )

            if not success:
                logger.error(f"[{self.chat_id}] LLM 调用失败: {response}")
                return None

            # 解析并校验 JSON 响应；严格解析失败时由 helper 使用 json_repair 回退。
            summary_text, recall_cues_text = _parse_summary_response(response)

            # Phase 2: 为 recall_cues 生成向量
            recall_cues = await self._vectorize_recall_cues(recall_cues_text)

            # 创建摘要对象
            summary = MidTermMemorySummary(
                summary_id=str(uuid.uuid4()),
                chat_id=self.chat_id,
                time_range_start=time_range_start,
                time_range_end=time_range_end,
                participants=participants,
                summary=summary_text,
                recall_cues=recall_cues,
                created_at=time.time(),
            )

            return summary

        except Exception as e:
            logger.error(f"[{self.chat_id}] LLM 摘要生成失败: {e}", exc_info=True)
            return None

    def _build_message_text(self, messages: List["DatabaseMessages"]) -> str:
        """构建消息文本块"""
        lines = []
        for msg in messages:
            timestamp = self._format_timestamp(msg.time)
            user_name = msg.user_info.user_nickname if msg.user_info else "Unknown"
            content = msg.processed_plain_text or ""
            lines.append(f"[{timestamp}] {user_name}: {content}")

        return "\n".join(lines)

    def _build_summary_prompt(
        self,
        time_range_start: float,
        time_range_end: float,
        participants: List[str],
        message_text: str,
    ) -> str:
        """构建摘要生成的 Prompt"""
        time_range_str = f"{self._format_timestamp(time_range_start)} ~ {self._format_timestamp(time_range_end)}"
        participants_str = "、".join(participants)

        prompt = f"""请把后续多条即将被裁切出短期上下文的聊天消息压缩成聊天回想。

要求：
1. 只根据后续消息总结，不要编造没有出现的信息。
2. summary 是完整摘要，保留话题脉络、人物立场、已达成结论、待办和重要细节。
3. recall_cues 输出 3-5 个用于之后语义匹配的查询式段落（简短的关键词或短句）。
4. 只输出 JSON，格式：{{"summary":"...","recall_cues":["..."]}}
5. 不要输出 markdown 代码块标记。

时间范围：{time_range_str}
参与人物：{participants_str}

后续聊天记录：
{message_text}
"""
        return prompt

    def _active_summaries(self) -> List[MidTermMemorySummary]:
        """
        获取当前未过期的摘要（读取时过滤）。

        管理器是进程内长期存活的单例，摘要可能在运行期间过期。
        此方法在召回/注入前剔除已过期的摘要，并在检测到过期时
        惰性回写文件与内存，保持一致。
        """
        ttl = self.config.summary_ttl_seconds
        if not ttl or ttl <= 0:
            return self.summaries

        cutoff = time.time() - ttl
        active = [s for s in self.summaries if s.time_range_end >= cutoff]

        # 惰性清理：若有过期项被剔除，同步内存与文件
        if len(active) != len(self.summaries):
            logger.info(
                f"[{self.chat_id}] 惰性清理过期摘要：{len(self.summaries)} -> {len(active)} 条"
            )
            self.summaries = active
            self.store.save_summaries(self.chat_id, active)

        return active

    def get_all_summaries_text(self) -> str:
        """
        获取所有摘要的文本（Phase 1: 简单全文注入）

        Returns:
            格式化的摘要文本块
        """
        summaries = self._active_summaries()
        if not summaries:
            return ""

        lines = ["[聊天回想 - 中期记忆]"]
        for i, summary in enumerate(summaries, 1):
            time_range_str = (
                f"{self._format_timestamp(summary.time_range_start)} ~ "
                f"{self._format_timestamp(summary.time_range_end)}"
            )
            lines.append(f"\n回想 {i} ({time_range_str}):")
            lines.append(summary.summary)

        return "\n".join(lines)

    async def recall_relevant_summaries(
        self,
        current_messages: List["DatabaseMessages"],
    ) -> str:
        """
        Phase 2.5: 分级向量召回 + 时间衰减 + token 预算控制

        召回分两级：
        - 高相关度（>= recall_threshold_high）：完整注入摘要全文
        - 中相关度（>= recall_threshold_low）：仅注入 recall_cues 关键词提示

        Args:
            current_messages: 当前上下文中的消息列表

        Returns:
            格式化的召回摘要文本块
        """
        # 读取时过滤过期摘要
        active_summaries = self._active_summaries()
        logger.debug(f"[{self.chat_id}] 开始召回，活跃摘要数: {len(active_summaries)}, 向量召回: {self.config.enable_vector_recall}")

        if not self.config.enable_vector_recall:
            # 向量召回未启用，使用降级策略
            return self._fallback_recall(active_summaries)

        if not active_summaries:
            logger.debug(f"[{self.chat_id}] 没有摘要可以召回")
            return ""

        # 检查 embedding 服务是否可用
        embedding_service = get_embedding_service()
        if not embedding_service.is_available():
            logger.debug(f"[{self.chat_id}] Embedding 服务不可用，使用降级策略")
            return self._fallback_recall(active_summaries)

        try:
            # 1. 构建查询文本（支持 LLM 辅助提取关键词）
            query_text = await self._build_smart_query(current_messages)
            logger.debug(f"[{self.chat_id}] 查询文本: {query_text[:100]}...")
            if not query_text:
                logger.debug(f"[{self.chat_id}] 查询文本为空")
                return ""

            # 2. 查询文本向量化
            query_embedding = await embedding_service.embed_text(query_text)
            if not query_embedding:
                logger.debug(f"[{self.chat_id}] 查询文本向量化失败，使用降级策略")
                return self._fallback_recall(active_summaries)

            # 3. 计算与所有活跃摘要的相似度（含时间衰减）
            scored_summaries = self._score_summaries(active_summaries, query_embedding)

            # 4. 分级召回
            high_relevance = []  # 完整注入
            mid_relevance = []   # 仅关键词

            for summary, score in scored_summaries:
                if score >= self.config.recall_threshold_high:
                    high_relevance.append((summary, score))
                elif score >= self.config.recall_threshold_low:
                    mid_relevance.append((summary, score))

            # 限制总召回数量
            max_total = self.config.max_recalled_summaries
            high_relevance = high_relevance[:max_total]
            remaining_slots = max(0, max_total - len(high_relevance))
            mid_relevance = mid_relevance[:remaining_slots]

            if not high_relevance and not mid_relevance:
                logger.debug(f"[{self.chat_id}] 没有满足阈值的摘要 (high >= {self.config.recall_threshold_high}, low >= {self.config.recall_threshold_low})")
                return ""

            # 5. 格式化输出（带 token 预算控制）
            output = self._format_tiered_recall(high_relevance, mid_relevance)

            logger.info(
                f"[{self.chat_id}] 分级召回: {len(high_relevance)} 条高相关 + {len(mid_relevance)} 条中相关 "
                f"(阈值: high={self.config.recall_threshold_high}, low={self.config.recall_threshold_low})"
            )
            return output

        except Exception as e:
            logger.warning(f"[{self.chat_id}] 向量召回失败: {e}，使用降级策略")
            return self._fallback_recall(active_summaries)

    def _score_summaries(
        self,
        summaries: List[MidTermMemorySummary],
        query_embedding: List[float],
    ) -> List[Tuple[MidTermMemorySummary, float]]:
        """
        计算摘要的综合得分（向量相似度 * 时间衰减）

        Returns:
            按得分降序排列的 (摘要, 得分) 列表
        """
        now = time.time()
        candidates = []

        for summary in summaries:
            # 向量相似度：取所有 recall_cues 中最高分
            best_score = 0.0
            for cue in summary.recall_cues:
                if cue.embedding:
                    score = cosine_similarity(query_embedding, cue.embedding)
                    best_score = max(best_score, score)

            if best_score <= 0:
                continue

            # 时间衰减
            if self.config.enable_time_decay:
                age_hours = (now - summary.time_range_end) / 3600.0
                half_life = self.config.time_decay_half_life_hours
                # 指数衰减：age_hours == half_life 时衰减到 0.5
                decay = math.pow(0.5, age_hours / half_life) if half_life > 0 else 1.0
                final_score = best_score * decay
            else:
                final_score = best_score

            candidates.append((summary, final_score))

        # 按得分降序排序
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates

    def _format_tiered_recall(
        self,
        high_relevance: List[Tuple[MidTermMemorySummary, float]],
        mid_relevance: List[Tuple[MidTermMemorySummary, float]],
    ) -> str:
        """
        格式化分级召回结果，受 max_injection_chars 预算控制

        - 高相关度：完整摘要文本
        - 中相关度：仅 recall_cues 关键词
        """
        budget = self.config.max_injection_chars
        lines = ["[聊天回想]"]
        current_chars = len(lines[0])
        idx = 1

        # 高相关度：完整注入
        for summary, score in high_relevance:
            time_range_str = (
                f"{self._format_timestamp(summary.time_range_start)} ~ "
                f"{self._format_timestamp(summary.time_range_end)}"
            )
            header = f"\n回想 {idx} ({time_range_str}, 相关度: {score:.2f}):"
            body = summary.summary

            entry_len = len(header) + len(body)
            if current_chars + entry_len > budget:
                # 预算不足，截断摘要
                remaining = budget - current_chars - len(header) - 10
                if remaining > 50:
                    lines.append(header)
                    lines.append(body[:remaining] + "...")
                    current_chars += len(header) + remaining + 3
                break

            lines.append(header)
            lines.append(body)
            current_chars += entry_len
            idx += 1

        # 中相关度：仅注入关键词提示
        if mid_relevance and current_chars < budget:
            lines.append("\n[相关话题提示]")
            current_chars += 8
            for summary, score in mid_relevance:
                cue_texts = [cue.text for cue in summary.recall_cues if cue.text]
                if not cue_texts:
                    continue
                time_str = self._format_timestamp(summary.time_range_end)
                hint = f"- ({time_str}, {score:.2f}) {' / '.join(cue_texts)}"
                if current_chars + len(hint) > budget:
                    break
                lines.append(hint)
                current_chars += len(hint)

        return "\n".join(lines)

    def _fallback_recall(self, active_summaries: List[MidTermMemorySummary]) -> str:
        """
        降级召回策略：按时间倒序取最近 N 条摘要，受 token 预算控制
        不再全量注入所有摘要
        """
        if not active_summaries:
            return ""

        # 按时间倒序取最近 N 条
        recent_count = self.config.fallback_recent_count
        recent = sorted(active_summaries, key=lambda s: s.time_range_end, reverse=True)[:recent_count]

        budget = self.config.max_injection_chars
        lines = ["[聊天回想 - 中期记忆（近期回顾）]"]
        current_chars = len(lines[0])

        for i, summary in enumerate(recent, 1):
            time_range_str = (
                f"{self._format_timestamp(summary.time_range_start)} ~ "
                f"{self._format_timestamp(summary.time_range_end)}"
            )
            header = f"\n回想 {i} ({time_range_str}):"
            body = summary.summary

            entry_len = len(header) + len(body)
            if current_chars + entry_len > budget:
                remaining = budget - current_chars - len(header) - 10
                if remaining > 50:
                    lines.append(header)
                    lines.append(body[:remaining] + "...")
                break

            lines.append(header)
            lines.append(body)
            current_chars += entry_len

        result = "\n".join(lines)
        if result == "[聊天回想 - 中期记忆（近期回顾）]":
            return ""

        logger.info(f"[{self.chat_id}] 降级召回：注入最近 {min(recent_count, len(recent))} 条摘要")
        return result

    async def _vectorize_recall_cues(self, cues_text: List[str]) -> List[RecallCue]:
        """
        为召回线索生成向量

        Args:
            cues_text: 召回线索文本列表

        Returns:
            包含向量的 RecallCue 对象列表
        """
        if not cues_text:
            return []

        embedding_service = get_embedding_service()
        if not embedding_service.is_available():
            # Embedding 服务不可用，只保存文本
            logger.debug(f"[{self.chat_id}] Embedding 服务不可用，只保存召回线索文本")
            return [RecallCue(text=text, embedding=None) for text in cues_text]

        try:
            # 批量向量化
            embeddings = await embedding_service.embed_texts(cues_text)

            recall_cues = []
            for text, embedding in zip(cues_text, embeddings, strict=False):
                recall_cues.append(RecallCue(text=text, embedding=embedding))

            logger.debug(f"[{self.chat_id}] 成功向量化 {len(recall_cues)} 条召回线索")
            return recall_cues

        except Exception as e:
            logger.warning(f"[{self.chat_id}] 召回线索向量化失败: {e}")
            # 失败时只保存文本
            return [RecallCue(text=text, embedding=None) for text in cues_text]

    async def _build_smart_query(self, messages: List["DatabaseMessages"]) -> str:
        """
        智能查询构建：优先使用 LLM 提取关键词，失败时回退到拼接原文

        Args:
            messages: 当前上下文消息列表

        Returns:
            用于向量召回的查询文本
        """
        if not messages:
            return ""

        # 提取最近 N 条消息
        count = self.config.query_message_count
        recent_messages = messages[-count:]
        raw_texts = []
        for msg in recent_messages:
            content = msg.processed_plain_text or ""
            if content.strip():
                raw_texts.append(content.strip())

        if not raw_texts:
            return ""

        raw_query = " ".join(raw_texts)

        # 如果启用 LLM 辅助提取关键词
        if self.config.enable_llm_query_extraction:
            try:
                extracted = await self._llm_extract_query_keywords(raw_query)
                if extracted:
                    return extracted
            except Exception as e:
                logger.debug(f"[{self.chat_id}] LLM 提取关键词失败，回退到原文: {e}")

        # 回退：直接使用原文拼接
        return raw_query

    async def _llm_extract_query_keywords(self, raw_text: str) -> Optional[str]:
        """
        使用 LLM 从最近对话中提取核心话题/关键词，用于向量召回

        Args:
            raw_text: 拼接的原始消息文本

        Returns:
            提取的关键词/短句，失败返回 None
        """
        # 限制输入长度，避免浪费 token
        if len(raw_text) > 500:
            raw_text = raw_text[-500:]

        prompt = f"""从以下对话片段中提取当前讨论的核心话题和关键词，用于从历史记忆中召回相关内容。
输出要求：直接输出 3-5 个关键词或短句，用空格分隔，不要输出其他内容。

对话片段：
{raw_text}

关键词："""

        try:
            utils_model_config = model_config.model_task_config.utils
            success, response, _, _ = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=utils_model_config,
                request_type="mid_term_memory.query_extraction",
                temperature=0.1,
                max_tokens=100,
            )

            if success and response and response.strip():
                result = response.strip()
                # 基本校验：不能太长，不能包含明显的格式标记
                if len(result) < 200 and not result.startswith("{") and not result.startswith("["):
                    return result

        except Exception as e:
            logger.debug(f"[{self.chat_id}] LLM query 提取异常: {e}")

        return None

    def _build_query_from_messages(self, messages: List["DatabaseMessages"]) -> str:
        """
        从最近消息中提取查询文本（同步版本，作为后备）

        Args:
            messages: 消息列表

        Returns:
            查询文本
        """
        if not messages:
            return ""

        count = self.config.query_message_count
        recent_messages = messages[-count:]
        query_parts = []

        for msg in recent_messages:
            content = msg.processed_plain_text or ""
            if content.strip():
                query_parts.append(content.strip())

        return " ".join(query_parts)

    @staticmethod
    def _format_timestamp(timestamp: float) -> str:
        """格式化时间戳"""
        return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


# LRU 管理器缓存（chat_id -> MidTermMemoryManager）
class _LRUManagerCache:
    """带 LRU 淘汰的管理器缓存，防止内存无限增长"""

    def __init__(self, max_size: int = 50):
        self._cache: OrderedDict[str, MidTermMemoryManager] = OrderedDict()
        self._max_size = max_size

    def get(self, chat_id: str) -> Optional[MidTermMemoryManager]:
        """获取管理器，命中时移到末尾（最近使用）"""
        if chat_id in self._cache:
            self._cache.move_to_end(chat_id)
            return self._cache[chat_id]
        return None

    def put(self, chat_id: str, manager: MidTermMemoryManager) -> None:
        """放入管理器，超出上限时淘汰最久未使用的"""
        if chat_id in self._cache:
            self._cache.move_to_end(chat_id)
        else:
            self._cache[chat_id] = manager
            # 淘汰最久未使用的
            while len(self._cache) > self._max_size:
                evicted_id, _ = self._cache.popitem(last=False)
                logger.debug(f"LRU 淘汰中期记忆管理器: {evicted_id}")

    def __contains__(self, chat_id: str) -> bool:
        return chat_id in self._cache

    def __len__(self) -> int:
        return len(self._cache)


_config = get_mid_term_memory_config()
_manager_cache = _LRUManagerCache(max_size=_config.manager_cache_max_size)


def get_mid_term_memory_manager(chat_id: str) -> MidTermMemoryManager:
    """
    获取指定聊天的中期记忆管理器（LRU 缓存模式）

    Args:
        chat_id: 聊天ID

    Returns:
        中期记忆管理器实例
    """
    manager = _manager_cache.get(chat_id)
    if manager is None:
        manager = MidTermMemoryManager(chat_id)
        _manager_cache.put(chat_id, manager)
    return manager
