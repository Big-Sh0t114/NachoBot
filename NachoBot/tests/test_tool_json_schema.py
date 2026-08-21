from __future__ import annotations

import unittest

from src.llm_models.model_client.gemini_client import _convert_tool_options as convert_gemini_tools
from src.llm_models.model_client.openai_client import _convert_tool_options as convert_openai_tools
from src.llm_models.utils_model import LLMRequest


class ToolJsonSchemaTests(unittest.TestCase):
    def test_raw_json_schema_survives_all_model_adapters(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "priority": {"type": "integer", "minimum": 1},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                }
            },
            "required": ["items"],
        }
        request = LLMRequest.__new__(LLMRequest)
        options = request._build_tool_options(
            [
                {
                    "name": "mcp_tasks_create",
                    "description": "Create structured tasks",
                    "input_schema": schema,
                }
            ]
        )
        self.assertIsNotNone(options)

        openai_tools = convert_openai_tools(options or [])
        self.assertEqual(openai_tools[0]["function"]["parameters"], schema)

        gemini_tools = convert_gemini_tools(options or [])
        self.assertEqual(gemini_tools[0].parameters_json_schema, schema)


if __name__ == "__main__":
    unittest.main()
