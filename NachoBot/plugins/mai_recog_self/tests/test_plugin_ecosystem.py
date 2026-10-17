"""Regression coverage for this plugin's Git, host, and config lifecycle contracts.

The config tests compile the current PluginBase method nodes and the plugin's
current config_schema expression from source. This avoids importing Bot startup
while exercising the host implementation and schema without copied method bodies.
"""

from __future__ import annotations

import ast
import datetime
import importlib.util
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import toml
import typing
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PLUGIN_ROOT.parents[1]
PLUGIN_PATH = PLUGIN_ROOT / "plugin.py"
PLUGIN_GITIGNORE = PLUGIN_ROOT / ".gitignore"
PLUGIN_BASE_PATH = CORE_ROOT / "src" / "plugin_system" / "base" / "plugin_base.py"
CONFIG_TYPES_PATH = CORE_ROOT / "src" / "plugin_system" / "base" / "config_types.py"


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _load_config_field_type():
    spec = importlib.util.spec_from_file_location("_mai_recog_host_config_types", CONFIG_TYPES_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ConfigField


def _host_config_harness_type_and_schema():
    config_field = _load_config_field_type()

    host_tree = ast.parse(PLUGIN_BASE_PATH.read_text(encoding="utf-8"))
    host_class = next(
        node for node in host_tree.body if isinstance(node, ast.ClassDef) and node.name == "PluginBase"
    )
    method_names = {
        "_generate_and_save_default_config",
        "_get_expected_config_version",
        "_get_current_config_version",
        "_migrate_config_values",
        "_generate_config_from_schema",
        "_save_config_to_file",
        "_load_plugin_config",
    }
    method_nodes = [
        node
        for node in host_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in method_names
    ]
    assert {node.name for node in method_nodes} == method_names
    harness_ast = ast.Module(
        body=[
            ast.ClassDef(
                name="HostConfigMethods",
                bases=[],
                keywords=[],
                body=method_nodes,
                decorator_list=[],
            )
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(harness_ast)
    host_globals = {
        "Any": typing.Any,
        "ConfigField": config_field,
        "Dict": typing.Dict,
        "Union": typing.Union,
        "datetime": datetime,
        "inspect": inspect,
        "logger": _Logger(),
        "os": os,
        "shutil": shutil,
        "toml": toml,
    }
    exec(compile(harness_ast, str(PLUGIN_BASE_PATH), "exec"), host_globals)

    plugin_tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    plugin_class = next(
        node for node in plugin_tree.body if isinstance(node, ast.ClassDef) and node.name == "MaiRecogSelfPlugin"
    )
    schema_assignment = next(
        node
        for node in plugin_class.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "config_schema" for target in node.targets)
    )
    schema = eval(
        compile(ast.Expression(schema_assignment.value), str(PLUGIN_PATH), "eval"),
        {
            "__builtins__": {},
            "ConfigField": config_field,
            "DEFAULT_BUDGET_SECONDS": 2.5,
            "bool": bool,
            "float": float,
            "str": str,
        },
    )
    return host_globals["HostConfigMethods"], schema


def _config_harness(plugin_dir: Path):
    harness_type, schema = _host_config_harness_type_and_schema()
    harness = harness_type()
    harness.plugin_dir = str(plugin_dir)
    harness.plugin_name = "mai_recog_self"
    harness.config_file_name = "config.toml"
    harness.config_section_descriptions = {"plugin": "插件基础配置", "recognition": "自身角色图片识别配置"}
    harness.config_schema = schema
    harness.config = {}
    harness.log_prefix = "[Plugin:mai_recog_self]"
    harness.enable_plugin = True
    harness.get_manifest_info = lambda key, default=None: "自身角色识别" if key == "description" else default
    return harness


class PluginGitignoreTests(unittest.TestCase):
    def test_standalone_gitignore_hides_only_local_outputs_and_configs(self):
        ignored_paths = (
            "config.toml",
            "config.toml.bak.20261017T010203Z.deadbeef",
            "config.toml.backup_20261017_010203",
            "__pycache__/plugin.cpython-313.pyc",
            "tests/__pycache__/test_plugin.cpython-313.pyc",
            ".pytest_cache/v/cache/nodeids",
            ".ruff_cache/0.12/cache/entry",
            ".cache/tool/entry",
            ".coverage",
            ".coverage.local",
            "coverage.xml",
            "htmlcov/index.html",
            "logs/worker.log",
            "logs/plugin.log",
            "worker-debug.log",
            ".venv/Scripts/python.exe",
            "venv/Scripts/python.exe",
            ".self-recognition-worker/Scripts/python.exe",
            "data/catalog.sqlite3",
            "data/models/v1/weights.pt",
            "localdata/catalog.sqlite3",
            "models/current/weights.pt",
            "uploads/pending/image.png",
            "jobs/job.json",
            "raw/image.png",
            "worker_deps/numpy/__init__.py",
        )
        visible_paths = (
            "_manifest.json",
            "plugin.py",
            "backend/worker.py",
            "backend/requirements-worker.txt",
            "backend/assets/mobilenet_v3_small-047dcff4.pth",
            "README.md",
            "BACKEND.md",
            "tests/test_plugin_ecosystem.py",
            "config.example.toml",
            "config.toml.example",
            "examples/config.toml",
        )

        with tempfile.TemporaryDirectory(prefix="mai-recog-gitignore-") as temporary:
            repo = Path(temporary)
            shutil.copyfile(PLUGIN_GITIGNORE, repo / ".gitignore")
            initialized = subprocess.run(
                ["git", "-C", str(repo), "init", "--quiet"], capture_output=True, text=True, check=False
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            git_env = os.environ.copy()
            git_env["GIT_CONFIG_NOSYSTEM"] = "1"

            for relative in ignored_paths + visible_paths:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"local fixture")

            for relative in ignored_paths:
                with self.subTest(path=relative):
                    result = subprocess.run(
                        [
                            "git",
                            "-c",
                            "core.excludesFile=",
                            "-C",
                            str(repo),
                            "check-ignore",
                            "--no-index",
                            "--quiet",
                            "--",
                            relative,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        env=git_env,
                    )
                    self.assertEqual(result.returncode, 0, f"expected ignored: {relative}; {result.stderr}")

            for relative in visible_paths:
                with self.subTest(path=relative):
                    result = subprocess.run(
                        [
                            "git",
                            "-c",
                            "core.excludesFile=",
                            "-C",
                            str(repo),
                            "check-ignore",
                            "--no-index",
                            "--quiet",
                            "--",
                            relative,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        env=git_env,
                    )
                    self.assertEqual(result.returncode, 1, f"expected visible: {relative}; {result.stderr}")


class PluginHostCompatibilityTests(unittest.TestCase):
    def test_context_import_guard_is_actionable_and_keeps_healthy_host_torch_free(self):
        script = r"""
import importlib.util
import sys
import types
from plugins.mai_recog_self.tests.test_self_recognition_plugin import PLUGIN_PATH, isolated_plugin

case = sys.argv[1]
target = "src.plugin_system.base.tool_execution_context"
with isolated_plugin() as (loaded, _manager, _service_holder):
    if case == "healthy":
        assert loaded.ToolExecutionPreparation.__name__ == "Preparation"
        assert "torch" not in sys.modules
        print("healthy context import torch-free")
    else:
        sys.modules.pop(loaded.__name__, None)
        if case == "missing-module":
            sys.modules.pop(target, None)
        elif case == "missing-type":
            sys.modules[target] = types.ModuleType(target)
        else:
            raise AssertionError(case)

        spec = importlib.util.spec_from_file_location(loaded.__name__, PLUGIN_PATH)
        candidate = importlib.util.module_from_spec(spec)
        sys.modules[loaded.__name__] = candidate
        try:
            spec.loader.exec_module(candidate)
        except ImportError as exc:
            message = str(exc)
            for expected in ("mai_recog_self", "ToolExecutionPreparation", "prepare_execution_context", "execute_with_context", "Update NachoBot", "README"):
                assert expected in message, message
            assert "torch" not in sys.modules
            print(case + " actionable and torch-free")
        else:
            raise AssertionError("the plugin import unexpectedly succeeded")
"""
        for case in ("missing-module", "missing-type", "healthy"):
            with self.subTest(case=case):
                result = subprocess.run(
                    [sys.executable, "-c", script, case],
                    cwd=CORE_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertIn("torch-free", result.stdout)


class PluginConfigLifecycleTests(unittest.TestCase):
    def test_fresh_config_uses_all_nine_current_schema_defaults(self):
        harness_type, schema = _host_config_harness_type_and_schema()
        field_names = {(section, name) for section, fields in schema.items() for name in fields}
        self.assertEqual(len(field_names), 9)

        with tempfile.TemporaryDirectory(prefix="mai-recog-config-fresh-") as temporary:
            harness = _config_harness(Path(temporary))
            harness._load_plugin_config()

            self.assertEqual(harness.config["plugin"], {
                "enabled": True,
                "version": "1.0.0",
                "config_version": "1.0.0",
            })
            self.assertEqual(harness.config["recognition"], {
                "character_name": "",
                "self_character_id": "",
                "worker_python": "",
                "data_dir": "",
                "auto_recognize": True,
                "budget_seconds": 2.5,
            })
            self.assertEqual(len(harness.config["plugin"]) + len(harness.config["recognition"]), len(field_names))
            self.assertTrue((Path(temporary) / "config.toml").is_file())

    def test_matching_config_version_preserves_exact_bytes_and_custom_values(self):
        content = '''[plugin]
enabled = false
version = "1.0.0"
config_version = "1.0.0"
custom_note = "preserve this field"

[recognition]
character_name = "Custom Bot"
self_character_id = "Moli"
worker_python = "/env/recognition/python"
data_dir = "/models/custom"
auto_recognize = false
budget_seconds = 1.75
'''
        with tempfile.TemporaryDirectory(prefix="mai-recog-config-same-") as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_bytes(content.encode("utf-8"))
            before = config_path.read_bytes()
            harness = _config_harness(Path(temporary))
            harness._load_plugin_config()

            self.assertEqual(config_path.read_bytes(), before)
            self.assertFalse(harness.config["plugin"]["enabled"])
            self.assertEqual(harness.config["plugin"]["custom_note"], "preserve this field")
            self.assertEqual(harness.config["recognition"]["self_character_id"], "Moli")
            self.assertEqual(harness.config["recognition"]["worker_python"], "/env/recognition/python")
            self.assertEqual(harness.config["recognition"]["data_dir"], "/models/custom")
            self.assertFalse(harness.config["recognition"]["auto_recognize"])

    def test_old_version_migrates_known_values_and_fills_missing_schema_fields(self):
        content = '''[plugin]
enabled = false
version = "2.7.1"
config_version = "0.8.0"

[recognition]
character_name = "Custom Bot"
data_dir = "D:/recognition"
auto_recognize = false
legacy_setting = "removed"
'''
        with tempfile.TemporaryDirectory(prefix="mai-recog-config-old-") as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(content, encoding="utf-8")
            harness = _config_harness(Path(temporary))
            harness._load_plugin_config()

            self.assertFalse(harness.config["plugin"]["enabled"])
            self.assertEqual(harness.config["plugin"]["version"], "2.7.1")
            self.assertEqual(harness.config["plugin"]["config_version"], "1.0.0")
            self.assertEqual(harness.config["recognition"]["character_name"], "Custom Bot")
            self.assertEqual(harness.config["recognition"]["data_dir"], "D:/recognition")
            self.assertFalse(harness.config["recognition"]["auto_recognize"])
            self.assertEqual(harness.config["recognition"]["self_character_id"], "")
            self.assertEqual(harness.config["recognition"]["worker_python"], "")
            self.assertEqual(harness.config["recognition"]["budget_seconds"], 2.5)
            self.assertNotIn("legacy_setting", harness.config["recognition"])
            self.assertEqual(toml.load(config_path), harness.config)

    def test_malformed_toml_raises_without_changing_config_bytes(self):
        content = b"[plugin\nenabled = false\n"
        with tempfile.TemporaryDirectory(prefix="mai-recog-config-malformed-") as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_bytes(content)
            harness = _config_harness(Path(temporary))
            with self.assertRaises(toml.TomlDecodeError):
                harness._load_plugin_config()
            self.assertEqual(config_path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
