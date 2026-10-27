"""DeepSeek deletion must repair displayed and generated model group references."""

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

WEBUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WEBUI))
import setup_deployment as deployment  # noqa: E402
from setup_model_groups import reconcile_deepseek_groups, siliconflow_free_text_ids  # noqa: E402

TEMPLATE = WEBUI.parent / "NachoBot/template/model_config_template.toml"


class ModelGroupTests(unittest.TestCase):
    def setUp(self):
        self.template = tomlkit.parse(TEMPLATE.read_text(encoding="utf-8"))
        self.models = [dict(model, model_name=model["name"]) for model in self.template["models"]]
        self.providers = list(self.template["api_providers"])
        self.groups = {name: list(group["model_list"]) for name, group in self.template["model_task_config"].items()}
        self.free_ids = siliconflow_free_text_ids(self.template)

    def reconcile(self, models, providers=None):
        return reconcile_deepseek_groups(
            self.groups, self.models, models,
            self.providers if providers is None else providers, self.free_ids,
        )

    def test_no_deletion_keeps_every_group(self):
        self.assertEqual(self.reconcile(self.models), self.groups)

    def test_deleted_paid_models_use_only_free_text_models_in_all_groups(self):
        models = [model for model in self.models if model["api_provider"] != "Deepseek"]
        result = self.reconcile(models)
        by_name = {model["name"]: model for model in models}
        for group in ("replyer0", "advanced_replyer", "file_edit", "realtime_replyer"):
            self.assertTrue(result[group])
            for name in result[group]:
                self.assertEqual(by_name[name]["api_provider"], "SiliconFlow")
                self.assertIn(by_name[name]["model_identifier"], self.free_ids)
        for group in ("planner", "utils", "tool_use", "embedding", "vlm", "voice", "replyer1"):
            self.assertEqual(result[group], self.groups[group])

    def test_partial_deletion_preserves_remaining_deepseek_references(self):
        models = [model for model in self.models if model["name"] != "deepseek-v4-flash"]
        result = self.reconcile(models)
        self.assertEqual(result["advanced_replyer"], ["deepseek-v4-pro"])
        self.assertIn("deepseek-v4-pro", result["file_edit"])
        self.assertNotIn("deepseek-v4-flash", result["replyer0"])

    def test_free_aliases_and_removed_fallback_candidates(self):
        model = {"model_identifier": self.free_ids[0], "model_name": "my-free-alias", "api_provider": "siliconflow"}
        result = self.reconcile([model], [{"name": "siliconflow"}])
        self.assertEqual(result["replyer0"], ["my-free-alias"])

    def test_no_text_fallback_fails_instead_of_using_embedding_or_asr(self):
        models = [model for model in self.models if model["model_identifier"] not in self.free_ids
                  and model["api_provider"] != "Deepseek"]
        with self.assertRaisesRegex(ValueError, "免费文本模型"):
            self.reconcile(models)

    def test_generate_writes_hidden_groups_and_keeps_other_group_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template_path = root / "NachoBot/template/model_config_template.toml"
            template_path.parent.mkdir(parents=True)
            template_path.write_bytes(TEMPLATE.read_bytes())
            target = root / "NachoBot/config/model_config.toml"
            target.parent.mkdir(parents=True)
            baseline = copy.deepcopy(self.template)
            baseline["models"][-1]["name"] = "custom-deepseek"
            baseline["model_task_config"]["advanced_replyer"]["model_list"] = ["custom-deepseek"]
            target.write_text(tomlkit.dumps(baseline), encoding="utf-8")
            data = {
                "components": ["core"],
                "providers": [provider for provider in self.providers if provider["name"] != "Deepseek"],
                "models": [model for model in self.models if model["api_provider"] != "Deepseek"],
                "model_groups": {"planner": ["Qwen/Qwen3-8B/Silicon"]},
            }
            with patch.object(deployment, "ROOT_DIR", root), \
                 patch.object(deployment, "BACKUP_DIR", root / "backups"), \
                 patch.object(deployment, "TEMPLATE_MAP", {
                     "NachoBot/template/model_config_template.toml": "NachoBot/config/model_config.toml",
                 }):
                defaults = deployment.ConfigInitializer.get_defaults()
                self.assertEqual(defaults["model_groups"]["advanced_replyer"], "custom-deepseek")
                self.assertEqual(defaults["siliconflow_free_text_ids"], self.free_ids)
                self.assertIn("file_edit", defaults["model_groups"])
                result = deployment.ConfigInitializer.generate_configs(data)
                self.assertEqual(result["errors"], [])
                generated = tomlkit.parse(target.read_text(encoding="utf-8"))
                groups = generated["model_task_config"]
                self.assertNotIn("custom-deepseek", groups["advanced_replyer"]["model_list"])
                self.assertNotIn("deepseek-v4-pro", groups["file_edit"]["model_list"])
                self.assertEqual(groups["embedding"], self.template["model_task_config"]["embedding"])
                self.assertEqual(groups["file_edit"]["max_tokens"], 8192)
                self.assertEqual(groups["planner"]["model_list"], ["Qwen/Qwen3-8B/Silicon"])
                data["models"] = [model for model in data["models"] if model["model_identifier"] not in self.free_ids]
                # Recreate a live DeepSeek reference to test preflight before any write.
                generated["model_task_config"]["replyer0"]["model_list"] = ["deepseek-v4-pro"]
                target.write_text(tomlkit.dumps(generated), encoding="utf-8")
                before = target.read_bytes()
                rejected = deployment.ConfigInitializer.generate_configs(data)
                self.assertTrue(rejected["errors"])
                self.assertEqual(rejected["generated"], [])
                self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
