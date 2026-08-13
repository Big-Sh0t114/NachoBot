import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

from src.chat.utils.playwright_search import PlaywrightSearchProvider
from src.common.logger import get_logger
from src.config.config import global_config, model_config
from src.llm_models.utils_model import LLMRequest

logger = get_logger("web_search")


DECISION_PROMPT = """
You are a web search decision assistant. Your name is {bot_name}. Current time: {time_now}.
Chat history:
{chat_history}

Now, {sender} said: {target_message}
Decide if web search is needed to answer. Only return need_search=true when:
1) The question needs real-time info (prices, weather, news, events, etc.)
2) The user explicitly asks to search/check/official link/latest info
3) The answer is likely outdated or beyond your knowledge

If not needed, set need_search=false and leave query empty.
Output JSON only:
{{"need_search": true/false, "query": "...", "reason": "..."}}
"""

class WebSearchManager:
    def __init__(
        self,
        chat_id: str,
        enable_cache: bool = True,
        cache_ttl: int = 2,
        max_results: int = 5,
        search_provider: Optional[PlaywrightSearchProvider] = None,
    ):
        self.chat_id = chat_id
        self.enable_cache = enable_cache
        self.cache_ttl = cache_ttl
        self.max_results = max_results
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._warned_disabled = False
        self._warned_decider = False

        model_set_decider = getattr(model_config.model_task_config, "tool_use", None)
        self._decider_enabled = bool(model_set_decider and model_set_decider.model_list)
        self._decider = (
            LLMRequest(model_set=model_set_decider, request_type="web_search_decider")
            if self._decider_enabled
            else None
        )

        tool_config = getattr(global_config, "tool", None)
        engines = getattr(tool_config, "web_search_engines", ["bing", "duckduckgo"])
        timeout_seconds = getattr(tool_config, "web_search_timeout_seconds", 20)
        self._search_provider = search_provider or PlaywrightSearchProvider(
            engines=engines,
            timeout_seconds=timeout_seconds,
        )
        self._search_enabled = search_provider is not None or self._search_provider.is_available()

    async def build_search_info(self, chat_history: str, sender: str, target: str, bot_name: str) -> str:
        if not target:
            return ""
        if not self._search_enabled:
            if not self._warned_disabled:
                logger.warning("联网搜索未启用：Playwright 不可用")
                self._warned_disabled = True
            return ""
        if not self._decider_enabled and not self._warned_decider:
            logger.warning("联网搜索判定未启用：model_task_config.tool_use 为空或未配置，将仅使用关键词触发")
            self._warned_decider = True

        target_preview = target.replace("\n", " ")[:80]
        logger.info(f"联网搜索检查: target={target_preview}")

        keyword_hit = self._keyword_hit(target)
        decision_task = asyncio.create_task(self._decide_need_search(chat_history, sender, target, bot_name))
        decision = await decision_task
        if decision is None:
            decision = {"need_search": False, "query": "", "reason": ""}
        if self._decider_enabled:
            need_search = bool(decision.get("need_search"))
        else:
            need_search = bool(keyword_hit)
        if not need_search:
            logger.info(
                f"联网搜索跳过: keyword_hit={keyword_hit}, model_need={decision.get('need_search')}"
            )
            return ""

        query = (decision.get("query") or "").strip()
        if not query:
            query = target.strip()
        if not query:
            return ""

        if cached := self._get_cache(query):
            return cached

        results = await self._search(query, chat_history)
        if not results:
            logger.info(f"联网搜索无结果: query={query}")
            return ""

        reason = ""
        if isinstance(decision, dict):
            reason = decision.get("reason", "") or ""
        if not reason and keyword_hit:
            reason = "keyword_trigger"
        formatted = self._format_results(query, results, reason)
        self._set_cache(query, formatted)
        logger.info(f"联网搜索结果: {self._truncate_for_log(formatted)}")
        return formatted

    async def _decide_need_search(
        self, chat_history: str, sender: str, target: str, bot_name: str
    ) -> Optional[Dict[str, Any]]:
        if not self._decider:
            return None

        prompt = DECISION_PROMPT.format(
            bot_name=bot_name,
            time_now=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            chat_history=chat_history,
            sender=sender,
            target_message=target,
        )

        try:
            content, _detail = await self._decider.generate_response_async(prompt)
        except Exception as e:
            logger.error(f"Web search decision failed: {e}")
            return None

        decision = self._load_json(content)
        if not decision:
            logger.warning("Web search decision returned non-JSON content, skip decision model")
            return None

        need_search = decision.get("need_search")
        if isinstance(need_search, str):
            decision["need_search"] = need_search.lower() in ("true", "yes", "1")
        elif not isinstance(need_search, bool):
            decision["need_search"] = False

        return decision

    async def _search(self, query: str, chat_history: str) -> List[Dict[str, str]]:
        if not self._search_enabled:
            return []

        try:
            return await self._search_provider.search(query=query, max_results=self.max_results)
        except Exception as e:
            logger.error(f"Playwright web search execution failed: {e}")
            return []

    def _format_results(self, query: str, results: List[Dict[str, str]], reason: str) -> str:
        lines = [f"Query: {query}"]
        for idx, item in enumerate(results[: self.max_results], 1):
            title = item.get("title", "").strip()
            url = item.get("url", "").strip()
            snippet = item.get("snippet", "").strip()
            if not title and not snippet:
                continue
            line = f"{idx}. {title}" if title else f"{idx}."
            if snippet:
                line += f" - {snippet}"
            if url:
                line += f" ({url})"
            lines.append(line)
        if reason:
            lines.append(f"Reason: {reason}")
        return "\n".join(lines)

    @staticmethod
    def _truncate_for_log(text: str, max_len: int = 800) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len].rstrip() + "...[truncated]"

    @staticmethod
    def _keyword_hit(text: str) -> bool:
        keywords = (
            "\u65b0\u95fb",
            "\u70ed\u641c",
            "\u70ed\u70b9",
            "\u5929\u6c14",
            "\u6c14\u6e29",
            "\u9884\u62a5",
            "\u4ef7\u683c",
            "\u591a\u5c11\u94b1",
            "\u6c47\u7387",
            "\u80a1\u4ef7",
            "\u884c\u60c5",
            "\u6700\u65b0",
            "\u5b9e\u65f6",
            "\u521a\u521a",
            "news",
            "price",
            "weather",
            "exchange rate",
            "stock",
        )
        return any(kw in text for kw in keywords)


    def _load_json(self, content: str) -> Optional[Dict[str, Any]]:
        if not content:
            return None

        cleaned = content.strip()
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

        for candidate in (cleaned, self._extract_json(cleaned)):
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        return match.group(0) if match else None

    def _get_cache(self, query: str) -> Optional[str]:
        if not self.enable_cache:
            return None

        cache_item = self._cache.get(query)
        if not cache_item:
            return None
        if cache_item["ttl"] <= 0:
            del self._cache[query]
            return None

        cache_item["ttl"] -= 1
        return cache_item["content"]

    def _set_cache(self, query: str, content: str) -> None:
        if not self.enable_cache:
            return

        self._cache[query] = {"content": content, "ttl": self.cache_ttl, "timestamp": time.time()}
