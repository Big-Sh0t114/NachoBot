"""
中期记忆存储层
负责摘要数据的持久化和读取（Phase 1: 使用 JSON 文件）
"""

import json
import os
import time
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict
from pathlib import Path

from src.common.logger import get_logger

logger = get_logger("mid_term_memory_store")


@dataclass
class RecallCue:
    """召回线索（包含文本和向量）"""

    text: str
    embedding: Optional[List[float]] = None

    def to_dict(self) -> dict:
        """转换为字典"""
        return {"text": self.text, "embedding": self.embedding}

    @classmethod
    def from_dict(cls, data: dict) -> "RecallCue":
        """从字典创建实例"""
        return cls(text=data.get("text", ""), embedding=data.get("embedding"))


@dataclass
class MidTermMemorySummary:
    """一条中期记忆摘要"""

    summary_id: str
    chat_id: str
    time_range_start: float
    time_range_end: float
    participants: List[str]
    summary: str
    recall_cues: List[RecallCue]  # Phase 2: 支持向量存储
    created_at: float

    def to_dict(self) -> dict:
        """转换为字典"""
        data = asdict(self)
        # 转换 recall_cues
        data["recall_cues"] = [cue.to_dict() for cue in self.recall_cues]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MidTermMemorySummary":
        """从字典创建实例"""
        # 兼容旧格式（recall_cues 为字符串列表）
        recall_cues_data = data.get("recall_cues", [])
        recall_cues = []

        for cue in recall_cues_data:
            if isinstance(cue, str):
                # 旧格式：纯字符串
                recall_cues.append(RecallCue(text=cue, embedding=None))
            elif isinstance(cue, dict):
                # 新格式：字典
                recall_cues.append(RecallCue.from_dict(cue))

        return cls(
            summary_id=data["summary_id"],
            chat_id=data["chat_id"],
            time_range_start=data["time_range_start"],
            time_range_end=data["time_range_end"],
            participants=data["participants"],
            summary=data["summary"],
            recall_cues=recall_cues,
            created_at=data["created_at"],
        )


class MidTermMemoryStore:
    """中期记忆存储管理器"""

    def __init__(self, storage_dir: str):
        """
        初始化存储管理器

        Args:
            storage_dir: 存储目录路径
        """
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_chat_file_path(self, chat_id: str) -> Path:
        """获取指定聊天的存储文件路径"""
        # 使用 chat_id 的哈希值作为文件名，避免特殊字符问题
        safe_chat_id = chat_id.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self.storage_dir / f"{safe_chat_id}.json"

    def load_summaries(self, chat_id: str) -> List[MidTermMemorySummary]:
        """
        加载指定聊天的所有中期记忆摘要

        Args:
            chat_id: 聊天ID

        Returns:
            摘要列表
        """
        file_path = self._get_chat_file_path(chat_id)

        if not file_path.exists():
            return []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            summaries = [MidTermMemorySummary.from_dict(item) for item in data.get("summaries", [])]
            logger.debug(f"从 {file_path} 加载了 {len(summaries)} 条中期记忆")
            return summaries

        except Exception as e:
            logger.error(f"加载中期记忆失败 (chat_id={chat_id}): {e}")
            return []

    def save_summaries(self, chat_id: str, summaries: List[MidTermMemorySummary]) -> bool:
        """
        保存指定聊天的所有中期记忆摘要

        Args:
            chat_id: 聊天ID
            summaries: 摘要列表

        Returns:
            是否保存成功
        """
        file_path = self._get_chat_file_path(chat_id)

        try:
            data = {
                "chat_id": chat_id,
                "summaries": [summary.to_dict() for summary in summaries],
                "version": "1.0",
            }

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            logger.debug(f"保存了 {len(summaries)} 条中期记忆到 {file_path}")
            return True

        except Exception as e:
            logger.error(f"保存中期记忆失败 (chat_id={chat_id}): {e}")
            return False

    def get_last_summary_end_time(self, chat_id: str) -> float:
        """
        获取最后一条摘要的结束时间

        Args:
            chat_id: 聊天ID

        Returns:
            最后一条摘要的结束时间，如果没有则返回 0.0
        """
        summaries = self.load_summaries(chat_id)
        if not summaries:
            return 0.0
        return max(summary.time_range_end for summary in summaries)

    @staticmethod
    def prune_summaries(
        summaries: List[MidTermMemorySummary],
        max_summaries: int,
        ttl_seconds: float,
        now: Optional[float] = None,
    ) -> List[MidTermMemorySummary]:
        """
        对摘要列表应用淘汰策略：先按时间过期（TTL），再按数量上限裁剪。

        Args:
            summaries: 原始摘要列表
            max_summaries: 最大保留数量
            ttl_seconds: 过期时间（秒），<= 0 表示不启用时间过期
            now: 当前时间戳（默认取 time.time()，便于测试）

        Returns:
            裁剪后的摘要列表（不修改入参）
        """
        if now is None:
            now = time.time()

        result = list(summaries)

        # 1. 时间过期：以内容结束时间 time_range_end 判断新旧
        if ttl_seconds and ttl_seconds > 0:
            cutoff = now - ttl_seconds
            result = [s for s in result if s.time_range_end >= cutoff]

        # 2. 数量上限：保留最新的 max_summaries 条
        if max_summaries > 0 and len(result) > max_summaries:
            result = result[-max_summaries:]

        return result

    def add_summary(
        self,
        chat_id: str,
        summary: MidTermMemorySummary,
        max_summaries: int,
        ttl_seconds: float = 0.0,
    ) -> List[MidTermMemorySummary]:
        """
        添加新的摘要，应用淘汰策略后持久化。

        Args:
            chat_id: 聊天ID
            summary: 新的摘要
            max_summaries: 最大保留数量
            ttl_seconds: 过期时间（秒），<= 0 表示不启用时间过期

        Returns:
            持久化后的摘要列表（已裁剪）；保存失败时返回裁剪后的列表但不保证已落盘
        """
        summaries = self.load_summaries(chat_id)
        summaries.append(summary)

        summaries = self.prune_summaries(summaries, max_summaries, ttl_seconds)

        self.save_summaries(chat_id, summaries)
        return summaries
