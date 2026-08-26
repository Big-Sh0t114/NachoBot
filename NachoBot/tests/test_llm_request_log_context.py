from __future__ import annotations

import importlib
import pathlib
import sys
import types
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

api_ada_configs = importlib.import_module("src.config.api_ada_configs")
utils_model = importlib.import_module("src.llm_models.utils_model")
TaskConfig = api_ada_configs.TaskConfig
LLMRequest = utils_model.LLMRequest


class LLMRequestLogContextTests(unittest.TestCase):
    def setUp(self):
        self.original_model_config = utils_model.model_config

        self.utils = TaskConfig(model_list=["utils-model"])
        self.replyer0 = TaskConfig(model_list=["replyer0-model"])
        self.replyer1 = TaskConfig(model_list=["replyer1-model"])
        self.replyer2 = TaskConfig(model_list=["replyer2-model"])

        task_config = types.SimpleNamespace(
            utils=self.utils,
            replyer0=self.replyer0,
            replyer1=self.replyer1,
            replyer2=self.replyer2,
            replyer=self.replyer1,
            _active_replyer_group=1,
        )
        utils_model.model_config = types.SimpleNamespace(model_task_config=task_config)

    def tearDown(self):
        utils_model.model_config = self.original_model_config

    def test_fixed_model_group_is_included_in_log_context(self):
        request = LLMRequest(self.utils, "test.utils")

        self.assertEqual(request.log_context, "[test.utils/utils]")

    def test_active_replyer_alias_resolves_to_concrete_group(self):
        request = LLMRequest(self.replyer1, "expression.learner")

        self.assertEqual(request.log_context, "[expression.learner/replyer1]")


if __name__ == "__main__":
    unittest.main()
