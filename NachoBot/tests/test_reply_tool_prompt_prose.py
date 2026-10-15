"""Exercise Replyer tool prompt formatting without importing NachoBot modules."""

from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPLYER_DIR = Path(__file__).resolve().parents[1] / "src" / "chat" / "replyer"
GENERATOR_FILES = ("group_generator.py", "private_generator.py")


@dataclass
class _ToolInfoResult:
    text: str = ""
    sandbox_candidate: object | None = None


class _SilentLogger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def _is_tool_result_branch(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.BoolOp):
        return False
    if not isinstance(node.test.op, ast.Or):
        return False
    return [item.id for item in node.test.values if isinstance(item, ast.Name)] == [
        "tool_results",
        "search_info",
        "url_info",
    ]


def _load_source_extracted_renderer(generator_file: str):
    source_path = REPLYER_DIR / generator_file
    parsed = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    replyer_class = next(
        node
        for node in parsed.body
        if isinstance(node, ast.ClassDef) and node.name in {"DefaultReplyer", "PrivateReplyer"}
    )
    method = next(
        node
        for node in replyer_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build_tool_info"
    )
    branch = next(node for node in ast.walk(method) if _is_tool_result_branch(node))

    renderer = ast.FunctionDef(
        name="render_tool_info",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg="tool_results"),
                ast.arg(arg="search_info"),
                ast.arg(arg="url_info"),
                ast.arg(arg="search_url_info"),
                ast.arg(arg="sandbox_candidate"),
            ],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        ),
        body=[branch],
        decorator_list=[],
    )
    extracted = ast.fix_missing_locations(ast.Module(body=[renderer], type_ignores=[]))
    namespace = {"ToolInfoResult": _ToolInfoResult, "logger": _SilentLogger()}
    exec(compile(extracted, str(source_path), "exec"), namespace)
    return namespace["render_tool_info"]


class ReplyToolPromptProseTests(unittest.TestCase):
    def test_tool_results_keep_raw_content_and_candidate_identity(self) -> None:
        raw_tool_content = '原始工具正文： 【网页内容】 {"item": 1} **保留格式**'
        raw_search_info = "原始搜索摘要 [保留括号]"
        raw_page_content = "原始网页正文\n第二行"
        raw_search_page = "原始搜索页正文 <保留尖括号>"
        candidate = type("Candidate", (), {"query": "整理 reports 目录"})()

        for generator_file in GENERATOR_FILES:
            with self.subTest(generator=generator_file):
                renderer = _load_source_extracted_renderer(generator_file)
                result = renderer(
                    [
                        {
                            "tool_name": "internal_tool_label",
                            "type": "internal_result_type",
                            "content": raw_tool_content,
                        }
                    ],
                    raw_search_info,
                    raw_page_content,
                    raw_search_page,
                    candidate,
                )

                self.assertIs(result.sandbox_candidate, candidate)
                for raw_content in (
                    raw_tool_content,
                    raw_search_info,
                    raw_page_content,
                    raw_search_page,
                ):
                    self.assertIn(raw_content, result.text)

                prose = result.text
                for raw_content in (
                    raw_tool_content,
                    raw_search_info,
                    raw_page_content,
                    raw_search_page,
                ):
                    prose = prose.replace(raw_content, "")

                self.assertNotIn("internal_tool_label", prose)
                self.assertNotIn("internal_result_type", prose)
                self.assertNotIn("【网页内容】", prose)
                self.assertNotIn("【联网搜索】", prose)
                self.assertNotIn("【搜索结果正文】", prose)
                self.assertNotIn("SANDBOX_EDIT_CONFIRMATION_REQUIRED", prose)
                self.assertNotIn('{"sandbox_edit_decision":true', prose)
                self.assertIn("待确认的操作内容：整理 reports 目录", prose)
                self._assert_sandbox_json_instructions(prose)

    def test_candidate_without_tool_results_keeps_typed_candidate(self) -> None:
        candidate = type("Candidate", (), {"query": "更新 notes.txt"})()

        for generator_file in GENERATOR_FILES:
            with self.subTest(generator=generator_file):
                renderer = _load_source_extracted_renderer(generator_file)
                result = renderer([], "", "", "", candidate)

                self.assertIs(result.sandbox_candidate, candidate)
                self.assertIn("待确认的操作内容：更新 notes.txt", result.text)
                self.assertNotIn("SANDBOX_EDIT_CONFIRMATION_REQUIRED", result.text)
                self.assertNotIn('{"sandbox_edit_decision":true', result.text)
                self._assert_sandbox_json_instructions(result.text)

    def _assert_sandbox_json_instructions(self, text: str) -> None:
        self.assertIn("只有在用户明确要求执行这项文件操作时", text)
        self.assertIn("才只输出一个 JSON 对象", text)
        self.assertIn("不得添加其他字段或 Markdown", text)
        self.assertIn("sandbox_edit_decision 必须是布尔值", text)
        self.assertIn("只有用户明确要求这项文件操作时才设为 true", text)
        self.assertIn("reply_to_user 填写面向用户的确认文本", text)
        self.assertIn("file_edit_query 简洁描述这项文件操作", text)
        for field in ("sandbox_edit_decision", "reply_to_user", "file_edit_query"):
            self.assertEqual(text.count(field), 2)


if __name__ == "__main__":
    unittest.main()
