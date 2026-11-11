"""Shared bounded rendering for natural-language tool evidence."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.common.prompt_rendering import render_bounded_sections
from src.common.tool_results import is_tool_error_result


MAX_REPLY_EVIDENCE_CHARS = 16000
MAX_REPLY_SECTION_CHARS = 8000

_INTRO = "下面整理了网页和工具查询获得的信息，供你结合当前对话撰写回复时参考：\n"
_PAGE_LABEL = "网页浏览获取到的页面内容："
_SEARCH_LABEL = "联网搜索找到的信息："
_SEARCH_PAGE_LABEL = "从搜索结果页面提取出的正文："
_TOOL_LABEL = "工具查询得到的信息如下："
_TOOL_ERROR_LABEL = "工具调用未成功（以下为错误信息，不代表执行成功）："
_FOOTER = "请结合这些实时资料和对话上下文作答。"


def _tool_content_sections(tool_results: Any) -> Iterable[tuple[str, Any]]:
    for result in tool_results:
        if type(result) is dict:
            content = result.get("content", "")
            label = _TOOL_ERROR_LABEL if is_tool_error_result(result) else _TOOL_LABEL
        else:
            content = "[unsupported value]"
            label = _TOOL_LABEL
        yield label, content


def build_reply_tool_context(
    *,
    url_info: Any = "",
    search_info: Any = "",
    search_url_info: Any = "",
    tool_results: Any = (),
    max_chars: int = MAX_REPLY_EVIDENCE_CHARS,
    section_max_chars: int = MAX_REPLY_SECTION_CHARS,
) -> str:
    """Build the established replyer prose with a bounded evidence section."""

    if not (tool_results or search_info or url_info or search_url_info):
        return ""

    def sections() -> Iterable[tuple[str, Any]]:
        if url_info:
            yield _PAGE_LABEL, url_info
        if search_info:
            yield _SEARCH_LABEL, search_info
        if search_url_info:
            yield _SEARCH_PAGE_LABEL, search_url_info
        if tool_results:
            yield from _tool_content_sections(tool_results)

    return render_bounded_sections(
        sections(),
        prefix=_INTRO,
        suffix=_FOOTER,
        max_chars=max_chars,
        section_max_chars=section_max_chars,
    )
