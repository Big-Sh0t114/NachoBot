import ast
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRINTF_PLACEHOLDER = re.compile(r"%[-+#0-9.]*[sdifro]")
LOG_METHODS = {
    "trace",
    "debug",
    "info",
    "success",
    "warning",
    "error",
    "critical",
    "exception",
}
LOGURU_SOURCES = (
    PROJECT_ROOT / "main.py",
    PROJECT_ROOT / "src" / "tts" / "backends" / "Vox" / "tts_model.py",
)


def _shared_logger_names(tree: ast.AST) -> set[str]:
    """Return names bound to nachobot_multimodal.logger.logger in a module."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "nachobot_multimodal.logger":
            continue
        for alias in node.names:
            if alias.name == "logger":
                names.add(alias.asname or alias.name)
    return names


def _printf_style_log_calls(path: Path) -> list[tuple[int, str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    logger_names = _shared_logger_names(tree)
    calls: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id not in logger_names or node.func.attr not in LOG_METHODS:
            continue
        if not node.args:
            continue
        message = node.args[0]
        if not isinstance(message, ast.Constant) or not isinstance(message.value, str):
            continue
        if PRINTF_PLACEHOLDER.search(message.value):
            calls.append((node.lineno, node.func.attr, message.value))
    return calls


class LoguruFormattingTests(unittest.TestCase):
    def test_shared_loguru_calls_do_not_use_printf_placeholders(self) -> None:
        malformed = []
        for path in LOGURU_SOURCES:
            malformed.extend(
                (path.relative_to(PROJECT_ROOT), line, method, message)
                for line, method, message in _printf_style_log_calls(path)
            )

        self.assertEqual(
            malformed,
            [],
            "printf-style placeholders remain in shared Loguru calls: "
            + repr(malformed),
        )


if __name__ == "__main__":
    unittest.main()
