import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

from prompt_template import normalize_prompt_template


ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ROOT = ROOT / "NachoBot-Discord-Adapter"
CORE_ROOT = ROOT / "NachoBot"
_CORE_FORMAT_SCRIPT = r'''
import ast
import json
import sys
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Union
import re

source_path = Path(sys.argv[1]) / "src" / "chat" / "utils" / "prompt_builder.py"
source = source_path.read_text(encoding="utf-8")
tree = ast.parse(source, filename=str(source_path))
prompt_node = next(
    node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Prompt"
)
namespace = {
    "Any": Any,
    "Dict": Dict,
    "List": List,
    "Optional": Optional,
    "Union": Union,
    "re": re,
    "logger": SimpleNamespace(error=lambda *_args, **_kwargs: None),
    "global_prompt_manager": SimpleNamespace(
        _context=SimpleNamespace(_current_context=None),
        register=lambda _prompt: None,
    ),
}
prompt_module = ast.Module(body=[prompt_node], type_ignores=[])
exec(compile(prompt_module, str(source_path), "exec"), namespace)
Prompt = namespace["Prompt"]

template = json.loads(sys.stdin.read())["template"]
values = {}
for _, field_name, _, _ in Formatter().parse(template):
    if field_name:
        root = field_name.split(".", 1)[0].split("[", 1)[0]
        values[root] = "CORE_" + root
print("PROMPT_RESULT:" + json.dumps(Prompt(template).format(**values), ensure_ascii=False))
'''


def _format_with_core_prompt(template: str) -> str:
    environment = os.environ.copy()
    adapter_path = str(ADAPTER_ROOT)
    if environment.get("PYTHONPATH"):
        adapter_path += os.pathsep + environment["PYTHONPATH"]
    environment["PYTHONPATH"] = adapter_path
    with tempfile.TemporaryDirectory(prefix="discord-core-prompt-") as working_directory:
        result = subprocess.run(
            [sys.executable, "-c", _CORE_FORMAT_SCRIPT, str(CORE_ROOT)],
            input=json.dumps({"template": template}, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=working_directory,
            env=environment,
            check=False,
        )
    if result.returncode:
        raise AssertionError(
            f"Core Prompt formatter failed in isolated cwd: {result.stderr or result.stdout}"
        )
    line = next(
        (line for line in result.stdout.splitlines() if line.startswith("PROMPT_RESULT:")),
        None,
    )
    if line is None:
        raise AssertionError(f"Core Prompt formatter returned no result: {result.stdout}")
    return json.loads(line.removeprefix("PROMPT_RESULT:"))


class DiscordPromptTemplateTests(unittest.TestCase):
    def test_example_and_legacy_prompts_format_through_core(self):
        example = tomllib.loads(
            (ADAPTER_ROOT / "config.toml.example").read_text(encoding="utf-8")
        )["prompts"]
        legacy = (
            'Core fields: {identity} and {custom_slot}.\n'
            'Raw nested JSON:\n{"reply":"{identity}","nested":{"tts_text":"{custom_slot}"}}\n'
            'Pre-escaped JSON: {{"outer":{{"inner":true}}}}\n'
            'Partially escaped legacy JSON: {{"outer":{"inner":false}}}\n'
            r'Backslash-escaped JSON: \{"outer":{"inner":"literal"}\}'
        )
        templates = {
            "planner_prompt": example["planner_prompt"],
            "replyer_prompt": example["replyer_prompt"],
            "legacy_prompt": legacy,
        }

        for name, raw_template in templates.items():
            with self.subTest(name=name):
                normalized = normalize_prompt_template(
                    raw_template,
                    {"interest": "披萨", "name_block": "我是 Nacho"},
                )
                self.assertEqual(normalized, normalize_prompt_template(normalized))
                formatted = _format_with_core_prompt(normalized)
                if name == "planner_prompt":
                    self.assertIn("披萨", formatted)
                if name in {"replyer_prompt", "legacy_prompt"}:
                    self.assertIn("CORE_identity", formatted)

                if name == "replyer_prompt":
                    self.assertIn("只输出一段自然、口语化的中文回复", formatted)
                    self.assertIn("CORE_background_dialogue_prompt", formatted)
                    self.assertNotIn('{"reply"', formatted)
                    self.assertNotIn('"tts_text"', formatted)
                elif name == "legacy_prompt":
                    raw_json = next(
                        line for line in formatted.splitlines() if line.startswith('{"reply"')
                    )
                    try:
                        parsed_json = json.loads(raw_json)
                    except json.JSONDecodeError as error:
                        self.fail(f"Core Prompt left invalid JSON ({error}): {formatted!r}")
                    self.assertEqual(
                        parsed_json,
                        {
                            "reply": "CORE_identity",
                            "nested": {"tts_text": "CORE_custom_slot"},
                        },
                        formatted,
                    )
                    self.assertIn('{"outer":{"inner":true}}', formatted)
                    self.assertIn('{"outer":{"inner":false}}', formatted)
                    self.assertIn('{"outer":{"inner":"literal"}}', formatted)


if __name__ == "__main__":
    unittest.main()
