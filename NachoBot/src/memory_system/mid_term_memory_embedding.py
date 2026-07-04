"""
中期记忆 Embedding 服务封装
提供简单的文本向量化接口
"""

from typing import List, Optional
import numpy as np

from src.common.logger import get_logger

logger = get_logger("mid_term_memory_embedding")


class MidTermEmbeddingService:
    """中期记忆 Embedding 服务（简化封装）"""

    def __init__(self):
        self._adapter = None
        self._initialized = False

    def _ensure_adapter(self):
        """懒加载 embedding adapter"""
        if self._initialized:
            return

        try:
            from src.A_memorix.core.embedding.api_adapter import EmbeddingAPIAdapter

            # 使用默认配置初始化
            self._adapter = EmbeddingAPIAdapter(
                batch_size=32,
                max_concurrent=5,
                default_dimension=1024,
                enable_cache=True,
                model_name="auto",
            )
            self._initialized = True
            logger.info("中期记忆 Embedding 服务初始化成功")

        except Exception as e:
            logger.warning(f"中期记忆 Embedding 服务初始化失败（将使用降级方案）: {e}")
            self._adapter = None
            self._initialized = True

    async def embed_text(self, text: str) -> Optional[List[float]]:
        """
        将单条文本转换为向量

        Args:
            text: 输入文本

        Returns:
            向量列表，失败返回 None
        """
        self._ensure_adapter()

        if not self._adapter:
            return None

        try:
            result = await self._adapter.encode(text)
            if isinstance(result, np.ndarray):
                return result.flatten().tolist()
            return result

        except Exception as e:
            logger.warning(f"文本向量化失败: {e}")
            return None

    async def embed_texts(self, texts: List[str]) -> List[Optional[List[float]]]:
        """
        批量将文本转换为向量

        Args:
            texts: 文本列表

        Returns:
            向量列表，每个元素对应一条文本的向量（失败为 None）
        """
        self._ensure_adapter()

        if not self._adapter or not texts:
            return [None] * len(texts)

        try:
            result = await self._adapter.encode(texts)

            if isinstance(result, np.ndarray):
                # 转换为列表格式
                if result.ndim == 1:
                    # 单个向量
                    return [result.tolist()]
                else:
                    # 多个向量
                    return [vec.tolist() for vec in result]

            return [None] * len(texts)

        except Exception as e:
            logger.warning(f"批量文本向量化失败: {e}")
            return [None] * len(texts)

    def is_available(self) -> bool:
        """检查 embedding 服务是否可用"""
        self._ensure_adapter()
        return self._adapter is not None


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    计算两个向量的余弦相似度

    Args:
        vec1: 向量1
        vec2: 向量2

    Returns:
        相似度 [0, 1]
    """
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0

    try:
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = sum(a * a for a in vec1) ** 0.5
        norm2 = sum(b * b for b in vec2) ** 0.5

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)

    except Exception:
        return 0.0


# 全局单例
_embedding_service = MidTermEmbeddingService()


def get_embedding_service() -> MidTermEmbeddingService:
    """获取 embedding 服务实例"""
    return _embedding_service
