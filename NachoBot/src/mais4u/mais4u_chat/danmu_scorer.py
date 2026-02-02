"""Danmu quality scorer using small model for filtering low-value messages."""

import re
from typing import List, Tuple

from src.common.logger import get_logger
from src.llm_models.utils_model import LLMRequest
from src.config.config import model_config
from src.mais4u.s4u_config import s4u_config_main

logger = get_logger("danmu_scorer")

# Scoring prompt template
SCORE_SINGLE_PROMPT = """你现在正在杂谈直播，请评估以下弹幕的回复价值（0-1分）：
弹幕："{danmu_text}"
用户：{user_name}

评分标准（请严格遵守）：
- 0.8-1.0：有趣的问题、真诚互动、有话题性、能引发主播吐槽或讨论
- 0.6-0.7：普通提问、日常问候（如"晚上好"）、简单的正面反馈
- 0.4-0.5：意义不明、简短无意义语气词、与直播无关的自言自语
- 0.1-0.3：复读刷屏、广告、纯符号
- 0.0：攻击性内容、纯噪音

注意：普通的打招呼或简单互动应给 0.6 以上，只有完全没法接话的才给 0.5 以下。
只输出一个数字（如 0.7），不要输出任何解释或其他文字。"""

SCORE_BATCH_PROMPT = """你现在正在杂谈直播，请评估以下弹幕的回复价值（每条0-1分）：

{danmu_list}

评分标准（请严格遵守）：
- 0.8-1.0：有趣的问题、真诚互动、有话题性
- 0.6-0.7：普通提问、日常问候、正面反馈
- 0.4-0.5：意义不明、简短语气词
- 0.1-0.3：复读刷屏、广告
- 0.0：攻击性内容

每一行只输出一个数字，顺序对应上面的弹幕，不要输出任何解释或其他文字。"""


class DanmuScorer:
    """使用小模型评估弹幕回复价值"""

    def __init__(self):
        self._llm_request = LLMRequest(model_set=model_config.model_task_config.planner, request_type="danmu_scorer")
        self._config = s4u_config_main.streamer_mode
        logger.info("[DanmuScorer] Initialized with threshold=%.2f", self._config.score_threshold)

    async def score_single(self, danmu_text: str, user_name: str) -> float:
        """
        单条弹幕打分

        Args:
            danmu_text: 弹幕文本
            user_name: 用户名

        Returns:
            0-1 之间的分数，越高越值得回复
        """
        prompt = SCORE_SINGLE_PROMPT.format(danmu_text=danmu_text, user_name=user_name)

        try:
            response_text, _ = await self._llm_request.generate_response_async(prompt)
            score = self._parse_score(response_text)
            if score < 0.6:  # Log specific details for low scores to debug
                logger.debug(
                    "[DanmuScorer] Low Score: '%s' scored %.2f (Raw: %s)",
                    danmu_text[:20],
                    score,
                    response_text.strip(),
                )
            else:
                logger.debug("[DanmuScorer] '%s' scored %.2f", danmu_text[:20], score)
            return score
        except Exception as e:
            logger.warning("[DanmuScorer] Score failed for '%s': %s", danmu_text[:20], e)
            return 0.6  # 默认通过分数 (Previously 0.5 was too strict)

    async def score_batch(self, danmu_list: List[Tuple[str, str]]) -> List[float]:
        """
        批量打分（更高效）

        Args:
            danmu_list: [(danmu_text, user_name), ...]

        Returns:
            对应的分数列表
        """
        if not danmu_list:
            return []

        if len(danmu_list) == 1:
            score = await self.score_single(danmu_list[0][0], danmu_list[0][1])
            return [score]

        # 构建批量打分 prompt
        danmu_formatted = "\n".join(
            [f"{i + 1}. [{user_name}]: {danmu_text}" for i, (danmu_text, user_name) in enumerate(danmu_list)]
        )
        prompt = SCORE_BATCH_PROMPT.format(danmu_list=danmu_formatted)

        try:
            response_text, _ = await self._llm_request.generate_response_async(prompt)
            scores = self._parse_scores(response_text, len(danmu_list))
            logger.info("[DanmuScorer] Batch scored %d danmu", len(scores))
            return scores
        except Exception as e:
            logger.warning("[DanmuScorer] Batch score failed: %s", e)
            return [0.6] * len(danmu_list)  # 默认通过

    def is_valid(self, score: float) -> bool:
        """检查分数是否达到有效阈值"""
        return score >= self._config.score_threshold

    def _parse_score(self, response: str) -> float:
        """解析单个分数"""
        try:
            # Check raw response
            if len(response) > 20:
                logger.debug(f"[DanmuScorer] Raw Response (Verbose): {response}")

            # 尝试提取数字
            match = re.search(r"(\d+\.?\d*)", response.strip())
            if match:
                score = float(match.group(1))
                return max(0.0, min(1.0, score))  # 限制在 0-1
        except (ValueError, AttributeError):
            pass
        return 0.5  # 解析失败默认中等

    def _parse_scores(self, response: str, expected_count: int) -> List[float]:
        """解析多个分数"""
        scores = []
        lines = response.strip().split("\n")

        for line in lines:
            match = re.search(r"(\d+\.?\d*)", line)
            if match:
                score = float(match.group(1))
                scores.append(max(0.0, min(1.0, score)))

        # 如果解析数量不匹配，用默认值填充
        while len(scores) < expected_count:
            scores.append(0.5)

        return scores[:expected_count]


# Global instance
_danmu_scorer: DanmuScorer = None


def get_danmu_scorer() -> DanmuScorer:
    """获取全局 DanmuScorer 实例"""
    global _danmu_scorer
    if _danmu_scorer is None:
        _danmu_scorer = DanmuScorer()
    return _danmu_scorer
