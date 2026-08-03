"""NachoBot 记忆服务层 - A_Memorix 的薄封装。

提供简洁的 API 供 NachoBot 其他模块调用 A_Memorix 功能，
内部委托到 a_memorix_host_service.invoke()。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.common.logger import get_logger

logger = get_logger("memory_service")


def _get_host_service():
    """延迟导入，避免循环依赖。"""
    from src.A_memorix.host_service import a_memorix_host_service
    return a_memorix_host_service


class MemoryService:
    """NachoBot 长期记忆服务。"""

    def is_enabled(self) -> bool:
        """检查 A_Memorix 是否已启用。"""
        try:
            return _get_host_service().is_enabled()
        except Exception:
            return False

    async def search(
        self,
        query: str,
        *,
        chat_id: str = "",
        limit: int = 5,
        mode: str = "search",
        person_id: str = "",
        time_start: Optional[str | float] = None,
        time_end: Optional[str | float] = None,
        respect_filter: bool = True,
        user_id: str = "",
        group_id: str = "",
    ) -> Dict[str, Any]:
        """检索长期记忆。"""
        return await _get_host_service().invoke(
            "search_memory",
            {
                "query": query,
                "chat_id": chat_id,
                "limit": limit,
                "mode": mode,
                "person_id": person_id,
                "time_start": time_start,
                "time_end": time_end,
                "respect_filter": bool(respect_filter),
                "user_id": user_id,
                "group_id": group_id,
            },
        )

    async def ingest_summary(
        self,
        *,
        external_id: str = "",
        chat_id: str = "",
        text: str = "",
        participants: Optional[List[str]] = None,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: str = "",
        group_id: str = "",
    ) -> Dict[str, Any]:
        """写入聊天摘要到长期记忆。"""
        return await _get_host_service().invoke(
            "ingest_summary",
            {
                "external_id": external_id,
                "chat_id": chat_id,
                "text": text,
                "participants": participants or [],
                "time_start": time_start,
                "time_end": time_end,
                "tags": tags or [],
                "metadata": metadata or {},
                "user_id": user_id,
                "group_id": group_id,
            },
        )

    async def ingest_text(
        self,
        *,
        external_id: str = "",
        source_type: str = "",
        text: str = "",
        chat_id: str = "",
        person_ids: Optional[List[str]] = None,
        participants: Optional[List[str]] = None,
        timestamp: Optional[str | float] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        entities: Optional[List[Any]] = None,
        relations: Optional[List[Any]] = None,
        user_id: str = "",
        group_id: str = "",
    ) -> Dict[str, Any]:
        """写入普通文本记忆。"""
        return await _get_host_service().invoke(
            "ingest_text",
            {
                "external_id": external_id,
                "source_type": source_type,
                "text": text,
                "chat_id": chat_id,
                "person_ids": person_ids or [],
                "participants": participants or [],
                "timestamp": timestamp,
                "tags": tags or [],
                "metadata": metadata or {},
                "entities": entities or [],
                "relations": relations or [],
                "user_id": user_id,
                "group_id": group_id,
            },
        )

    async def get_person_profile(
        self,
        person_id: str,
        chat_id: str = "",
        limit: int = 10,
    ) -> Dict[str, Any]:
        """获取人物画像。"""
        return await _get_host_service().invoke(
            "get_person_profile",
            {"person_id": person_id, "chat_id": chat_id, "limit": limit},
        )

    async def memory_stats(self) -> Dict[str, Any]:
        """获取记忆统计信息。"""
        return await _get_host_service().invoke("memory_stats")

    async def maintain_memory(
        self,
        action: str,
        target: str = "",
        hours: Optional[float] = None,
        reason: str = "",
        limit: int = 50,
    ) -> Dict[str, Any]:
        """维护记忆状态（reinforce/protect/restore/freeze）。"""
        return await _get_host_service().invoke(
            "maintain_memory",
            {
                "action": action,
                "target": target,
                "hours": hours,
                "reason": reason,
                "limit": limit,
            },
        )


# 全局单例
memory_service = MemoryService()
