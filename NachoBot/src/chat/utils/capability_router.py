"""Shared capability routing for optional web-search and MCP branches.

The router deliberately stays independent from the actual executors.  It only
decides whether a branch is useful and supplies a normalized task.  Permission
checks and tool execution remain the responsibility of their owning layers.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, Mapping, Optional, Tuple


CAPABILITY_DECISION_PROMPT = """
You are a capability-routing assistant for {bot_name}. Current time: {time_now}.

Chat history:
{chat_history}

Now, {sender} said:
{target_message}

Available MCP tools (name: short description):
{mcp_catalog}

Decide whether either optional capability is required.

Web search rules:
- Set need_web_search=true only for current/public information, explicit web
  lookup requests, prices, weather, news, schedules, official links, or facts
  likely to have changed.
- Do not use web search for ordinary conversation or stable knowledge.

MCP rules:
- Set need_mcp=true only when the request needs data or an action that one of
  the listed MCP tools can actually provide.
- Typical MCP requests read private/account state or create, update, delete,
  send, upload, download, control, or inspect something in an external system.
- Merely discussing MCP, a platform, or a tool does not require MCP execution.
- Do not choose MCP as a generic substitute for public web search.
- If MCP is needed, return a concise imperative mcp_task and up to 12 exact
  tool names from the catalog that may help. Never invent tool names.

Return JSON only:
{{
  "need_web_search": true/false,
  "web_query": "...",
  "web_reason": "...",
  "need_mcp": true/false,
  "mcp_task": "...",
  "mcp_tool_names": ["exact_tool_name"],
  "mcp_reason": "..."
}}
"""


_AUTO_DECIDER = object()
_WEB_KEYWORDS = (
    "新闻",
    "热搜",
    "热点",
    "天气",
    "气温",
    "预报",
    "价格",
    "多少钱",
    "汇率",
    "股价",
    "行情",
    "最新",
    "实时",
    "刚刚",
    "官网链接",
    "官方链接",
    "news",
    "price",
    "weather",
    "exchange rate",
    "stock",
)


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    """Normalized routing decision shared by all chat entry points."""

    need_web_search: bool = False
    web_query: str = ""
    web_reason: str = ""
    need_mcp: bool = False
    mcp_task: str = ""
    mcp_tool_names: Tuple[str, ...] = ()
    mcp_reason: str = ""

    def to_web_search_decision(self) -> Dict[str, Any]:
        return {
            "need_search": self.need_web_search,
            "query": self.web_query,
            "reason": self.web_reason,
        }


class CapabilityRouter:
    """Use one lightweight decision call for web-search and MCP routing."""

    def __init__(
        self,
        chat_id: str,
        *,
        decider: Any = _AUTO_DECIDER,
        auto_mcp: Optional[bool] = None,
        logger_instance: Optional[Any] = None,
    ) -> None:
        self.chat_id = chat_id
        self._warned_decider = False
        self._logger = logger_instance or logging.getLogger("capability_router")

        if decider is _AUTO_DECIDER:
            # Keep this module import-safe for pure unit tests. Runtime-owned
            # dependencies are loaded only when a real router is constructed.
            from src.common.logger import get_logger
            from src.config.config import global_config, model_config
            from src.llm_models.utils_model import LLMRequest

            self._logger = logger_instance or get_logger("capability_router")
            model_set = getattr(model_config.model_task_config, "tool_use", None)
            self._decider_enabled = bool(model_set and model_set.model_list)
            self._decider = (
                LLMRequest(model_set=model_set, request_type="capability_router") if self._decider_enabled else None
            )
            if auto_mcp is None:
                tool_config = getattr(global_config, "tool", None)
                auto_mcp = bool(getattr(tool_config, "mcp_auto_detect", True))
        else:
            self._decider = decider
            self._decider_enabled = decider is not None

        self.auto_mcp = True if auto_mcp is None else bool(auto_mcp)

    async def decide(
        self,
        *,
        chat_history: str,
        sender: str,
        target: str,
        bot_name: str,
        allow_web_search: bool,
        allow_mcp: bool,
        mcp_catalog: str = "",
    ) -> CapabilityDecision:
        target = str(target or "").strip()
        if not target:
            return CapabilityDecision()

        catalog = str(mcp_catalog or "").strip()
        allow_web_search = bool(allow_web_search)
        allow_mcp = bool(allow_mcp and catalog)
        explicit_mcp = allow_mcp and is_explicit_mcp_request(target)
        route_mcp = bool(allow_mcp and (self.auto_mcp or explicit_mcp))

        if not allow_web_search and not route_mcp:
            return CapabilityDecision()

        payload: Optional[Dict[str, Any]] = None
        if self._decider_enabled and self._decider:
            prompt = CAPABILITY_DECISION_PROMPT.format(
                bot_name=bot_name,
                time_now=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                chat_history=_tail(chat_history, 6000),
                sender=sender,
                target_message=_tail(target, 3000),
                mcp_catalog=_tail(catalog, 6000) if route_mcp else "(none available)",
            )
            try:
                content, _detail = await self._decider.generate_response_async(prompt)
                payload = load_json_object(content)
                if payload is None:
                    self._logger.warning("能力路由返回了非 JSON 内容，使用安全降级规则")
            except Exception as exc:
                self._logger.error(f"能力路由判定失败: {exc}")
        elif not self._warned_decider:
            self._logger.warning("能力路由模型未配置，将仅使用显式 MCP 和联网关键词触发")
            self._warned_decider = True

        decision = decision_from_payload(
            payload or {},
            target=target,
            allow_web_search=allow_web_search,
            allow_mcp=route_mcp,
            catalog=catalog,
            fallback_web=payload is None and has_web_search_keyword(target),
            fallback_mcp=payload is None and explicit_mcp,
        )

        if allow_mcp and explicit_mcp and not decision.need_mcp:
            decision = CapabilityDecision(
                need_web_search=decision.need_web_search,
                web_query=decision.web_query,
                web_reason=decision.web_reason,
                need_mcp=True,
                mcp_task=target,
                mcp_tool_names=decision.mcp_tool_names,
                mcp_reason="explicit_mcp_request",
            )
        self._logger.info(
            "能力路由结果: "
            f"web={decision.need_web_search}, mcp={decision.need_mcp}, "
            f"mcp_tools={list(decision.mcp_tool_names)}"
        )
        return decision


async def build_search_after_decision(
    decision: Awaitable[CapabilityDecision],
    web_search_manager: Any,
    *,
    chat_history: str,
    sender: str,
    target: str,
    bot_name: str,
) -> str:
    route = await decision
    if not route.need_web_search:
        return ""
    return await web_search_manager.build_search_info(
        chat_history=chat_history,
        sender=sender,
        target=target,
        bot_name=bot_name,
        decision=route.to_web_search_decision(),
    )


async def execute_mcp_after_decision(
    decision: Awaitable[CapabilityDecision],
    mcp_executor: Any,
    *,
    chat_history: str,
    sender: str,
    target: str,
    return_details: bool = False,
) -> Any:
    route = await decision
    if not route.need_mcp:
        return [], [], ""
    return await mcp_executor.execute_from_chat_message(
        sender=sender,
        target_message=route.mcp_task or target,
        chat_history=chat_history,
        return_details=return_details,
        candidate_tool_names=route.mcp_tool_names,
    )


def decision_from_payload(
    payload: Mapping[str, Any],
    *,
    target: str,
    allow_web_search: bool,
    allow_mcp: bool,
    catalog: str,
    fallback_web: bool = False,
    fallback_mcp: bool = False,
) -> CapabilityDecision:
    need_web = allow_web_search and (_as_bool(payload.get("need_web_search")) or fallback_web)
    need_mcp = allow_mcp and (_as_bool(payload.get("need_mcp")) or fallback_mcp)

    available_names = extract_catalog_names(catalog)
    requested_names = payload.get("mcp_tool_names")
    normalized_names = []
    if isinstance(requested_names, (list, tuple)):
        for item in requested_names:
            name = str(item or "").strip()
            if name and name in available_names and name not in normalized_names:
                normalized_names.append(name)
            if len(normalized_names) >= 12:
                break

    web_query = _limited_text(payload.get("web_query") or payload.get("query"), 1000)
    mcp_task = _limited_text(payload.get("mcp_task"), 2000)
    return CapabilityDecision(
        need_web_search=need_web,
        web_query=(web_query or target) if need_web else "",
        web_reason=(
            _limited_text(payload.get("web_reason") or payload.get("reason"), 300)
            or ("keyword_trigger" if fallback_web else "")
        ),
        need_mcp=need_mcp,
        mcp_task=(mcp_task or target) if need_mcp else "",
        mcp_tool_names=tuple(normalized_names) if need_mcp else (),
        mcp_reason=(_limited_text(payload.get("mcp_reason"), 300) or ("explicit_mcp_request" if fallback_mcp else "")),
    )


def load_json_object(content: Any) -> Optional[Dict[str, Any]]:
    if not content:
        return None
    cleaned = str(content).strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    candidates = (cleaned, _extract_json(cleaned))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def extract_catalog_names(catalog: str) -> set[str]:
    names: set[str] = set()
    for line in str(catalog or "").splitlines():
        normalized = line.strip().lstrip("- ").strip()
        if not normalized or normalized.startswith("..."):
            continue
        name = normalized.split(":", 1)[0].strip()
        if name:
            names.add(name)
    return names


def has_web_search_keyword(text: str) -> bool:
    normalized = str(text or "").lower()
    return any(keyword.lower() in normalized for keyword in _WEB_KEYWORDS)


def is_explicit_mcp_request(text: str) -> bool:
    normalized = str(text or "").strip()
    conceptual = re.search(
        r"(?:什么是|解释|介绍|原理|教程|文档|如何|怎么|怎样).{0,12}mcp|\bhow\s+to\s+use\s+mcp\b",
        normalized,
        flags=re.IGNORECASE,
    )
    explicit_action = any(
        action in normalized
        for action in (
            "查询我的",
            "查看我的",
            "读取我的",
            "创建",
            "新增",
            "修改",
            "删除",
            "发送",
            "上传",
            "下载",
            "操作",
        )
    )
    if conceptual and not explicit_action:
        return False

    patterns = (
        r"(?:^|\s)/mcp(?:\s|$)",
        r"(?:请|帮我|尝试)?(?:使用|调用|通过|用)\s*mcp(?:工具|服务|服务器)?",
        r"\b(?:use|call|invoke|via)\s+(?:the\s+)?mcp\b",
    )
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def _limited_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    return text[:max_chars]


def _tail(value: Any, max_chars: int) -> str:
    text = str(value or "")
    return text if len(text) <= max_chars else text[-max_chars:]


def _extract_json(text: str) -> Optional[str]:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    return match.group(0) if match else None
