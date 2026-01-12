import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

from src.common.logger import get_logger
from src.config.config import model_config
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

SEARCH_PROMPT = """
You are a web search assistant. Use online search to fetch public information.
Query: {query}
Context: {chat_history}

Requirements:
- Return no more than {max_results} results
- Each result includes title, url, snippet
- Output JSON only, no extra text

JSON format:
{{"query": "...", "results": [{{"title": "", "url": "", "snippet": ""}}], "note": ""}}
"""


class WebSearchManager:
    def __init__(self, chat_id: str, enable_cache: bool = True, cache_ttl: int = 2, max_results: int = 5):
        self.chat_id = chat_id
        self.enable_cache = enable_cache
        self.cache_ttl = cache_ttl
        self.max_results = max_results
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._warned_disabled = False
        self._warned_decider = False

        model_set_search = getattr(model_config.model_task_config, "web_search", None)
        model_set_decider = getattr(model_config.model_task_config, "tool_use", None)
        self._search_enabled = bool(model_set_search and model_set_search.model_list)
        self._decider_enabled = bool(model_set_decider and model_set_decider.model_list)
        self._decider = LLMRequest(model_set=model_set_decider, request_type="web_search_decider") if self._decider_enabled else None
        self._searcher = LLMRequest(model_set=model_set_search, request_type="web_search") if self._search_enabled else None

    async def build_search_info(self, chat_history: str, sender: str, target: str, bot_name: str) -> str:
        if not target:
            return ""
        if not self._search_enabled:
            if not self._warned_disabled:
                logger.warning("联网搜索未启用：model_task_config.web_search 为空或未配置")
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
        if not self._searcher:
            return []

        prompt = SEARCH_PROMPT.format(
            query=query,
            chat_history=chat_history,
            max_results=self.max_results,
        )

        try:
            content, _detail = await self._searcher.generate_response_async(prompt)
        except Exception as e:
            logger.error(f"Web search execution failed: {e}")
            return []

        payload = self._load_json(content)
        if not payload:
            fallback_results = self._fallback_plain_text_results(content)
            if fallback_results:
                return fallback_results
            logger.warning("Web search returned non-JSON content and no fallback extracted")
            return []

        return self._normalize_results(payload.get("results"))

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

    def _fallback_plain_text_results(self, content: str) -> List[Dict[str, str]]:
        if not content:
            return []
        cleaned = content.strip()
        if not cleaned:
            return []
        return [{"title": "", "url": "", "snippet": cleaned}]

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

    @staticmethod
    def _normalize_results(raw_results: Any) -> List[Dict[str, str]]:
        if not isinstance(raw_results, list):
            return []

        results: List[Dict[str, str]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            snippet = str(item.get("snippet", "")).strip()
            if not (title or snippet or url):
                continue
            results.append({"title": title, "url": url, "snippet": snippet})
        return results

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
