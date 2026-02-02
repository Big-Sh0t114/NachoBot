import json
from json_repair import repair_json
from typing import List

from src.llm_models.utils_model import LLMRequest
from src.config.api_ada_configs import TaskConfig
from src.common.logger import get_logger
from src.mais4u.s4u_config import s4u_config

logger = get_logger("bilingual_splitter")

SPLIT_PROMPT_TEMPLATE = """
你是一个字幕切分助手。请将以下双语文本切分为适合字幕显示的片段。

文本内容：
{text}

要求：
1. **语义优先**：请根据句子的语义结构进行切分，尽量保持词组、从句的完整性，不要生硬地切断主谓宾结构。
2. **长度适中**：在保持语义完整的前提下，将中文内容切分为适合字幕显示的片段（建议每段10-15个中文字符）。
3. **严格语义对齐**：日文翻译必须与切分后的中文片段在语义上完全对应，**不要出现中文读完了日文还没读完，或者日文读完了中文还没出来的错位**。
4. **颜文字处理**：如果文本中包含颜文字（如 (´・ω・`)），请将其放入视觉上最顺畅的片段（通常跟随前一句的情绪表达）。
5. 必须输出为 JSON 列表格式，包含 "cn" (中文/正文) 和 "jp" (日文/翻译) 两个字段。
6. 如果某一段只有中文没有日文，jp字段留空字符串。如果只有日文（或颜文字）没有中文，cn字段留空。

输出格式示例：
[
    {{"cn": "中文片段1", "jp": "日文片段1"}},
    {{"cn": "中文片段2 (颜文字)", "jp": "日文片段2"}}
]

请确保 JSON 格式合法。
"""


class BilingualSplitter:
    def __init__(self):
        # Construct TaskConfig from s4u_config.models.motion
        motion_conf = s4u_config.models.motion
        logger.info(f"BilingualSplitter loaded motion config: {motion_conf}")
        model_name = motion_conf.get("name", "gpt-4.1-mini")  # Default fallback to gpt-4.1-mini

        # Create TaskConfig for the model
        task_config = TaskConfig(model_list=[model_name], temperature=0.1, max_tokens=2000)

        self.llm_model = LLMRequest(model_set=task_config, request_type="bilingual_split")

        # Register prompt if not exists (dynamic)
        # However, global_prompt_manager usually loads from DB or code.
        # Here we use direct prompt string construction or register safely.
        # For simplicity, we pass the prompt string directly to a simple template wrapper if needed,
        # but LLMRequest usually takes a formatted string. We don't strictly need PromptManager for internal tools
        # unless we want hot-reload. We will use direct string formatting here.

    async def split_text(self, text: str) -> List[str]:
        """
        Split text using LLM and return formatted "CN (JP)" strings.
        Returns list of strings.
        """
        if not text or not text.strip():
            return []

        prompt = SPLIT_PROMPT_TEMPLATE.format(text=text)

        try:
            content, (reasoning, model_name, _) = await self.llm_model.generate_response_async(
                prompt=prompt, raise_when_empty=True
            )

            if not content:
                logger.warning("LLM split returned empty content")
                return []

            # Parse JSON
            # Use json_repair to handle potential malformed JSON
            json_obj = repair_json(content)
            if isinstance(json_obj, str):
                parsed = json.loads(json_obj)
            else:
                parsed = json_obj

            if not isinstance(parsed, list):
                logger.warning(f"LLM split returned non-list format: {parsed}")
                return []

            result_sentences = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue

                cn = item.get("cn", "").strip()
                jp = item.get("jp", "").strip()

                if not cn and not jp:
                    continue

                if cn and jp:
                    # Combined format
                    # Ensure jp is bracketed if it's not already (though usually we format it ourselves)
                    # Our system expects "CnText (JpText)"
                    # Check if jp already has brackets?
                    # The prompt asks for raw strings. We add brackets.
                    # clean_jp = jp.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
                    # Keep formatted as source
                    result_sentences.append(f"{cn} ({jp})")
                elif cn:
                    result_sentences.append(cn)
                elif jp:
                    # Only JP? Treat as text with empty JP? Or just Text?
                    # If it's sound only, maybe just (JP)?
                    result_sentences.append(f"({jp})")

            logger.info(f"LLM Split success: {len(result_sentences)} chunks")
            return result_sentences

        except Exception as e:
            logger.error(f"LLM Split failed: {e}")
            raise e  # Raise to trigger fallback


# Global instance
bilingual_splitter = BilingualSplitter()
