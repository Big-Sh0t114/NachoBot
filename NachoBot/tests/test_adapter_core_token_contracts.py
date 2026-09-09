from __future__ import annotations

import ast
import unittest
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ENTRYPOINTS = (
    "NachoBot-Napcat-Adapter/src/mmc_com_layer.py",
    "NachoBot-Bilibili-Adapter/adapter.py",
    "NachoBot-Multimodal-Adapter/main.py",
    "NachoBot-Koishi-Adapter/adapter.py",
    "NachoBot-DiscordVC-Adapter/adapter.py",
    "NachoBot-UniversalVC-Adapter/adapter.py",
)


class AdapterCoreTokenContractTests(unittest.TestCase):
    def test_every_core_router_target_uses_the_shared_environment_token(self) -> None:
        for relative_path in ADAPTER_ENTRYPOINTS:
            with self.subTest(adapter=relative_path):
                source = (WORKSPACE_ROOT / relative_path).read_text(encoding="utf-8")
                tree = ast.parse(source)
                calls = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "TargetConfig"
                ]
                self.assertTrue(calls, "adapter has no Core TargetConfig")
                for call in calls:
                    token_keyword = next(
                        (keyword for keyword in call.keywords if keyword.arg == "token"),
                        None,
                    )
                    self.assertIsNotNone(token_keyword)
                    self.assertIsInstance(token_keyword.value, ast.Call)
                    self.assertIsInstance(token_keyword.value.func, ast.Name)
                    self.assertEqual(
                        token_keyword.value.func.id,
                        "get_core_token_from_env",
                    )


if __name__ == "__main__":
    unittest.main()
