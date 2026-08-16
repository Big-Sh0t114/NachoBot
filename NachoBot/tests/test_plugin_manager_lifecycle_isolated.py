"""PluginManager 卸载钩子的隔离单元测试。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class PluginBase:
    pass


class ComponentType:
    pass


class ComponentRegistry:
    pass


@contextmanager
def isolated_plugin_manager_module():
    package_name = "_nachobot_plugin_manager_test"
    module_name = f"{package_name}.plugin_manager"
    module_names = {
        package_name,
        module_name,
        f"{package_name}.component_registry",
        "src",
        "src.common",
        "src.common.logger",
        "src.plugin_system",
        "src.plugin_system.base",
        "src.plugin_system.base.plugin_base",
        "src.plugin_system.base.component_types",
        "src.plugin_system.utils",
        "src.plugin_system.utils.manifest_utils",
        "src.plugin_system.core",
        "src.plugin_system.core.component_registry",
        "src.plugins",
        "src.plugins.built_in",
        "src.plugins.built_in.synthetic_demo",
        "src.plugins.built_in.synthetic_demo.plugin",
        "src.plugins.built_in.knowledge",
    }
    previous = {name: sys.modules.get(name) for name in module_names}

    def install(name: str, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        sys.modules[name] = module
        return module

    for stub_package_name in (
        "src",
        "src.common",
        "src.plugin_system",
        "src.plugin_system.base",
        "src.plugin_system.utils",
        "src.plugin_system.core",
    ):
        package = install(stub_package_name)
        package.__path__ = []
    install("src.common.logger", get_logger=lambda _name: _Logger())
    install("src.plugin_system.base.plugin_base", PluginBase=PluginBase)
    install("src.plugin_system.base.component_types", ComponentType=ComponentType)
    install("src.plugin_system.utils.manifest_utils", VersionComparator=object)
    install("src.plugin_system.core.component_registry", component_registry=ComponentRegistry())
    test_package = install(package_name)
    test_package.__path__ = []
    sys.modules[f"{package_name}.component_registry"] = install(
        f"{package_name}.component_registry",
        component_registry=ComponentRegistry(),
    )

    spec = importlib.util.spec_from_file_location(
        module_name,
        PROJECT_ROOT / "src/plugin_system/core/plugin_manager.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load plugin_manager")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class PluginUnloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_v1_decorator_scan_records_directory_and_loads_manifest(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            module.plugin_manager = manager
            manager._record_plugin_requirements = lambda _path: None
            manager._show_plugin_components = lambda _name: None
            aliases = {
                "src.plugin_system.core.plugin_manager": sys.modules.get(
                    "src.plugin_system.core.plugin_manager"
                ),
                "src.plugin_system.base.base_plugin": sys.modules.get(
                    "src.plugin_system.base.base_plugin"
                ),
                "src.plugin_system.apis": sys.modules.get("src.plugin_system.apis"),
                "src.plugin_system.apis.plugin_register_api": sys.modules.get(
                    "src.plugin_system.apis.plugin_register_api"
                ),
            }

            class DecoratorBase:
                pass

            try:
                sys.modules["src.plugin_system.core.plugin_manager"] = module
                base_plugin_module = types.ModuleType("src.plugin_system.base.base_plugin")
                base_plugin_module.BasePlugin = DecoratorBase
                sys.modules["src.plugin_system.base.base_plugin"] = base_plugin_module
                api_package = types.ModuleType("src.plugin_system.apis")
                api_package.__path__ = []
                sys.modules["src.plugin_system.apis"] = api_package
                spec = importlib.util.spec_from_file_location(
                    "src.plugin_system.apis.plugin_register_api",
                    PROJECT_ROOT / "src/plugin_system/apis/plugin_register_api.py",
                )
                if spec is None or spec.loader is None:
                    raise AssertionError("cannot load plugin register API")
                register_api = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = register_api
                spec.loader.exec_module(register_api)

                with tempfile.TemporaryDirectory() as temp_dir:
                    plugin_root = Path(temp_dir)
                    plugin_dir = plugin_root / "decorated_v1"
                    plugin_dir.mkdir()
                    (plugin_dir / "_manifest.json").write_text(
                        json.dumps({"name": "Decorated", "description": "isolated"}),
                        encoding="utf-8",
                    )
                    (plugin_dir / "plugin.py").write_text(
                        "from pathlib import Path\n"
                        "import json\n"
                        "from src.plugin_system.apis.plugin_register_api import register_plugin\n"
                        "from src.plugin_system.base.base_plugin import BasePlugin\n"
                        "@register_plugin\n"
                        "class DecoratedPlugin(BasePlugin):\n"
                        "    plugin_name = 'decorated_v1'\n"
                        "    enable_plugin = True\n"
                        "    def __init__(self, plugin_dir):\n"
                        "        self.plugin_dir = plugin_dir\n"
                        "        self.manifest_data = json.loads((Path(plugin_dir) / '_manifest.json').read_text())\n"
                        "    def register_plugin(self):\n"
                        "        return True\n",
                        encoding="utf-8",
                    )

                    loaded, failed = manager._load_plugin_modules_from_directory(str(plugin_root))
                    self.assertEqual((loaded, failed), (1, 0))
                    self.assertTrue(manager.plugin_classes, repr(manager.plugin_classes))
                    self.assertEqual(manager.plugin_paths["decorated_v1"], str(plugin_dir.resolve()))
                    status, count = manager.load_registered_plugin_classes("decorated_v1")
                    self.assertEqual((status, count), (True, 1))
                    instance = manager.loaded_plugins["decorated_v1"]
                    self.assertEqual(instance.plugin_dir, str(plugin_dir.resolve()))
                    self.assertEqual(instance.manifest_data["name"], "Decorated")
            finally:
                for name, old_module in aliases.items():
                    if old_module is None:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = old_module

    async def test_absolute_plugin_path_uses_project_relative_package_name(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            plugin_file = PROJECT_ROOT / "plugins" / "demo" / "plugin.py"

            class Loader:
                def exec_module(self, loaded_module):
                    self.package = loaded_module.__package__

            loader = Loader()
            spec = types.SimpleNamespace(loader=loader)
            with mock.patch.object(
                module,
                "spec_from_file_location",
                return_value=spec,
            ), mock.patch.object(
                module,
                "module_from_spec",
                return_value=types.ModuleType("plugins.demo"),
            ), mock.patch.object(
                manager,
                "_record_plugin_requirements",
            ):
                self.assertTrue(manager._load_plugin_module_file(str(plugin_file)))

            self.assertEqual(loader.package, "plugins.demo")

    async def test_builtin_plugin_parent_path_keeps_sibling_packages_importable(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            plugin_file = PROJECT_ROOT / "src" / "plugins" / "built_in" / "synthetic_demo" / "plugin.py"

            class Loader:
                def exec_module(self, _loaded_module):
                    return None

            spec = types.SimpleNamespace(loader=Loader())
            with mock.patch.object(
                module,
                "spec_from_file_location",
                return_value=spec,
            ), mock.patch.object(
                module,
                "module_from_spec",
                return_value=types.ModuleType("src.plugins.built_in.synthetic_demo.plugin"),
            ), mock.patch.object(
                manager,
                "_record_plugin_requirements",
            ):
                self.assertTrue(manager._load_plugin_module_file(str(plugin_file)))

            built_in_paths = {Path(path).resolve() for path in sys.modules["src.plugins.built_in"].__path__}
            self.assertIn((PROJECT_ROOT / "src" / "plugins" / "built_in").resolve(), built_in_paths)
            self.assertIsNotNone(importlib.util.find_spec("src.plugins.built_in.knowledge"))

    async def test_absolute_plugin_reload_supports_sibling_relative_import(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            with tempfile.TemporaryDirectory() as temp_dir:
                plugin_dir = Path(temp_dir) / "relative_demo"
                plugin_dir.mkdir()
                (plugin_dir / "sibling.py").write_text("VALUE = 42\n", encoding="utf-8")
                plugin_file = plugin_dir / "plugin.py"
                plugin_file.write_text(
                    "from .sibling import VALUE\nLOADED_VALUE = VALUE\nPLUGIN_MARKER = 'A'\n",
                    encoding="utf-8",
                )
                sibling_stat = (plugin_dir / "sibling.py").stat()
                plugin_stat = plugin_file.stat()
                with mock.patch.object(manager, "_record_plugin_requirements"):
                    self.assertTrue(manager._load_plugin_module_file(str(plugin_file)))

                package_name = manager._package_name_for_plugin_file(str(plugin_file))
                module_name = f"{package_name}.plugin"
                first_plugin = sys.modules[module_name]
                first_sibling = sys.modules[f"{package_name}.sibling"]
                self.assertEqual(first_plugin.LOADED_VALUE, 42)
                self.assertEqual(first_plugin.PLUGIN_MARKER, "A")

                # Keep both source size and mtime unchanged: a timestamp-pyc
                # implementation would otherwise incorrectly reuse A/42.
                (plugin_dir / "sibling.py").write_text("VALUE = 43\n", encoding="utf-8")
                os.utime(plugin_dir / "sibling.py", ns=(sibling_stat.st_atime_ns, sibling_stat.st_mtime_ns))
                plugin_file.write_text(
                    "from .sibling import VALUE\nLOADED_VALUE = VALUE\nPLUGIN_MARKER = 'B'\n",
                    encoding="utf-8",
                )
                os.utime(plugin_file, ns=(plugin_stat.st_atime_ns, plugin_stat.st_mtime_ns))
                with mock.patch.object(manager, "_record_plugin_requirements"):
                    self.assertTrue(manager._load_plugin_module_file(str(plugin_file)))

                second_plugin = sys.modules[module_name]
                second_sibling = sys.modules[f"{package_name}.sibling"]
                self.assertEqual(second_plugin.LOADED_VALUE, 43)
                self.assertEqual(second_plugin.PLUGIN_MARKER, "B")
                self.assertEqual(second_sibling.VALUE, 43)
                self.assertIsNot(second_plugin, first_plugin)
                self.assertIsNot(second_sibling, first_sibling)

    async def test_failed_plugin_exec_restores_previous_sys_modules_entry(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            with tempfile.TemporaryDirectory() as temp_dir:
                plugin_dir = Path(temp_dir) / "failing_demo"
                plugin_dir.mkdir()
                (plugin_dir / "sibling.py").write_text("VALUE = 7\n", encoding="utf-8")
                plugin_file = plugin_dir / "plugin.py"
                plugin_file.write_text(
                    "from .sibling import VALUE\nraise RuntimeError(f'boom {VALUE}')\n",
                    encoding="utf-8",
                )
                package_name = manager._package_name_for_plugin_file(str(plugin_file))
                module_name = f"{package_name}.plugin"
                previous = types.ModuleType(module_name)
                sys.modules[module_name] = previous
                with mock.patch.object(manager, "_record_plugin_requirements"):
                    self.assertFalse(manager._load_plugin_module_file(str(plugin_file)))
                self.assertIs(sys.modules.get(module_name), previous)
                self.assertNotIn(
                    f"{package_name}.sibling",
                    sys.modules,
                )

    async def test_external_same_basename_packages_are_distinct_and_preserve_each_other(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                plugin_a = root / "one" / "shared_name"
                plugin_b = root / "two" / "shared_name"
                plugin_a.mkdir(parents=True)
                plugin_b.mkdir(parents=True)
                for plugin_dir, value in ((plugin_a, "A"), (plugin_b, "B")):
                    (plugin_dir / "sibling.py").write_text(
                        f"VALUE = {value!r}\n", encoding="utf-8"
                    )
                    (plugin_dir / "plugin.py").write_text(
                        "from .sibling import VALUE\nLOADED_VALUE = VALUE\n",
                        encoding="utf-8",
                    )

                package_a = manager._package_name_for_plugin_file(str(plugin_a / "plugin.py"))
                package_b = manager._package_name_for_plugin_file(str(plugin_b / "plugin.py"))
                self.assertNotEqual(package_a, package_b)
                with mock.patch.object(manager, "_record_plugin_requirements"):
                    self.assertTrue(manager._load_plugin_module_file(str(plugin_a / "plugin.py")))
                    first_a = sys.modules[f"{package_a}.sibling"]
                    self.assertTrue(manager._load_plugin_module_file(str(plugin_b / "plugin.py")))

                self.assertEqual(sys.modules[f"{package_a}.sibling"].VALUE, "A")
                self.assertIs(sys.modules[f"{package_a}.sibling"], first_a)
                self.assertEqual(sys.modules[f"{package_b}.sibling"].VALUE, "B")

    async def test_registry_delta_rolls_back_sibling_defined_classes_and_paths(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            with tempfile.TemporaryDirectory() as temp_dir:
                plugin_dir = Path(temp_dir) / "delta_plugin"
                plugin_dir.mkdir()
                package_name = manager._package_name_for_plugin_file(str(plugin_dir / "plugin.py"))
                sibling_module = f"{package_name}.sibling"
                old_sibling_class = type(
                    "OldSiblingPlugin",
                    (),
                    {"__module__": sibling_module},
                )
                new_sibling_class = type(
                    "NewSiblingPlugin",
                    (),
                    {"__module__": sibling_module},
                )
                newly_registered_class = type(
                    "NewSiblingComponent",
                    (),
                    {"__module__": sibling_module},
                )
                unrelated_class = type(
                    "UnrelatedPlugin",
                    (),
                    {"__module__": "unrelated_plugin.module"},
                )
                manager.plugin_classes["sibling_entry"] = old_sibling_class
                manager.plugin_paths["sibling_entry"] = str(plugin_dir.resolve())
                manager.plugin_classes["unrelated_entry"] = unrelated_class
                manager.plugin_paths["unrelated_entry"] = str((plugin_dir.parent / "unrelated").resolve())
                snapshot = manager._snapshot_plugin_registries()

                manager.plugin_classes["sibling_entry"] = new_sibling_class
                manager.plugin_classes["new_sibling_entry"] = newly_registered_class
                manager.plugin_paths["sibling_entry"] = str((plugin_dir / "changed").resolve())
                manager.plugin_paths["new_sibling_entry"] = str((plugin_dir / "new").resolve())

                manager._restore_plugin_registry_delta(
                    snapshot,
                    plugin_name=None,
                    package_name=package_name,
                    plugin_dir=str(plugin_dir.resolve()),
                )

                self.assertIs(manager.plugin_classes["sibling_entry"], old_sibling_class)
                self.assertNotIn("new_sibling_entry", manager.plugin_classes)
                self.assertNotIn("new_sibling_entry", manager.plugin_paths)
                self.assertIs(manager.plugin_classes["unrelated_entry"], unrelated_class)
                self.assertEqual(
                    manager.plugin_paths["sibling_entry"],
                    str(plugin_dir.resolve()),
                )
                self.assertEqual(
                    manager.plugin_paths["unrelated_entry"],
                    str((plugin_dir.parent / "unrelated").resolve()),
                )

    async def test_reload_candidate_instance_failure_restores_relative_import_subtree(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            old_instance = types.SimpleNamespace(plugin_name="demo")
            old_class = type("OldPlugin", (), {})
            manager.loaded_plugins["demo"] = old_instance
            manager.plugin_classes["demo"] = old_class
            manager.plugin_paths["demo"] = "C:/plugins/demo"
            manager.plugin_classes["unrelated"] = type("Unrelated", (), {})
            manager.plugin_paths["unrelated"] = "C:/plugins/unrelated"

            with tempfile.TemporaryDirectory() as temp_dir:
                plugin_dir = Path(temp_dir) / "candidate_instance_failure"
                plugin_dir.mkdir()
                (plugin_dir / "sibling.py").write_text("VALUE = 42\n", encoding="utf-8")
                plugin_file = plugin_dir / "plugin.py"
                plugin_file.write_text(
                    "from .sibling import VALUE\nLOADED_VALUE = VALUE\n",
                    encoding="utf-8",
                )
                old_plugin_path = str(plugin_dir.resolve())
                manager.plugin_paths["demo"] = old_plugin_path
                package_name = manager._package_name_for_plugin_file(str(plugin_file))
                previous_package = types.ModuleType(package_name)
                previous_package.__path__ = [str(plugin_dir)]
                previous_plugin = types.ModuleType(f"{package_name}.plugin")
                previous_sibling = types.ModuleType(f"{package_name}.sibling")
                previous_sibling.VALUE = 99
                sys.modules[package_name] = previous_package
                sys.modules[previous_plugin.__name__] = previous_plugin
                sys.modules[previous_sibling.__name__] = previous_sibling
                seen_values = []

                class Candidate:
                    def __init__(self, **_kwargs):
                        raise RuntimeError("candidate init failed")

                original_loader = manager._load_plugin_module_file

                def load_candidate(path, **kwargs):
                    result = original_loader(path, **kwargs)
                    if result:
                        seen_values.append(sys.modules[f"{package_name}.plugin"].LOADED_VALUE)
                        manager.plugin_classes["demo"] = Candidate
                        manager.plugin_paths["demo"] = str(plugin_dir.resolve())
                    return result

                manager._load_plugin_module_file = load_candidate
                self.assertFalse(await manager.reload_registered_plugin("demo"))

                self.assertIs(sys.modules[package_name], previous_package)
                self.assertIs(sys.modules[f"{package_name}.plugin"], previous_plugin)
                self.assertIs(sys.modules[f"{package_name}.sibling"], previous_sibling)
                self.assertEqual(seen_values, [42])
                self.assertEqual(previous_sibling.VALUE, 99)
                self.assertIs(manager.plugin_classes["demo"], old_class)
                self.assertEqual(manager.plugin_paths["demo"], old_plugin_path)
                self.assertIn("unrelated", manager.plugin_classes)
                self.assertIs(manager.loaded_plugins["demo"], old_instance)

    async def test_reload_register_failure_restores_modules_and_unrelated_registry(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            old_instance = types.SimpleNamespace(plugin_name="demo")
            old_class = type("OldPlugin", (), {})
            manager.loaded_plugins["demo"] = old_instance
            manager.plugin_classes["demo"] = old_class
            manager.plugin_paths["demo"] = "C:/plugins/demo"
            unrelated_class = type("Unrelated", (), {})
            manager.plugin_classes["unrelated"] = unrelated_class
            manager.plugin_paths["unrelated"] = "C:/plugins/unrelated"

            with tempfile.TemporaryDirectory() as temp_dir:
                plugin_dir = Path(temp_dir) / "candidate_register_failure"
                plugin_dir.mkdir()
                (plugin_dir / "sibling.py").write_text("VALUE = 42\n", encoding="utf-8")
                plugin_file = plugin_dir / "plugin.py"
                plugin_file.write_text(
                    "from .sibling import VALUE\nLOADED_VALUE = VALUE\n",
                    encoding="utf-8",
                )
                old_plugin_path = str(plugin_dir.resolve())
                manager.plugin_paths["demo"] = old_plugin_path
                package_name = manager._package_name_for_plugin_file(str(plugin_file))
                previous_package = types.ModuleType(package_name)
                previous_package.__path__ = [str(plugin_dir)]
                previous_plugin = types.ModuleType(f"{package_name}.plugin")
                previous_sibling = types.ModuleType(f"{package_name}.sibling")
                previous_sibling.VALUE = 11
                sys.modules[package_name] = previous_package
                sys.modules[previous_plugin.__name__] = previous_plugin
                sys.modules[previous_sibling.__name__] = previous_sibling

                class Candidate:
                    plugin_name = "demo"
                    enable_plugin = True
                    manifest_data = {}

                    def __init__(self, **_kwargs):
                        self.plugin_name = "demo"
                        self.enable_plugin = True
                        self.manifest_data = {}

                    def register_plugin(self):
                        raise RuntimeError("candidate register failed")

                original_loader = manager._load_plugin_module_file

                def load_candidate(path, **kwargs):
                    result = original_loader(path, **kwargs)
                    if result:
                        manager.plugin_classes["demo"] = Candidate
                        manager.plugin_paths["demo"] = str(plugin_dir.resolve())
                    return result

                manager._load_plugin_module_file = load_candidate
                manager._deactivate_plugin_registry = mock.AsyncMock(return_value=True)
                manager._restore_plugin_registry = mock.Mock(return_value=True)
                self.assertFalse(await manager.reload_registered_plugin("demo"))

                self.assertIs(sys.modules[package_name], previous_package)
                self.assertIs(sys.modules[f"{package_name}.plugin"], previous_plugin)
                self.assertIs(sys.modules[f"{package_name}.sibling"], previous_sibling)
                self.assertIs(manager.plugin_classes["demo"], old_class)
                self.assertEqual(manager.plugin_paths["demo"], old_plugin_path)
                self.assertIs(manager.plugin_classes["unrelated"], unrelated_class)
                self.assertEqual(manager.plugin_paths["unrelated"], "C:/plugins/unrelated")
                self.assertIs(manager.loaded_plugins["demo"], old_instance)

    async def test_reload_scan_failure_keeps_old_instance_and_module_registries(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            old_class = type("OldPlugin", (), {})
            old_instance = types.SimpleNamespace(plugin_name="demo")
            manager.loaded_plugins["demo"] = old_instance
            manager.plugin_classes["demo"] = old_class
            manager.plugin_paths["demo"] = "C:/plugins/demo"

            def failed_scan(_plugin_file, **_kwargs):
                manager.plugin_classes["unrelated_from_failed_scan"] = object
                manager.plugin_paths["unrelated_from_failed_scan"] = "C:/plugins/other"
                return False

            manager._load_plugin_module_file = failed_scan
            self.assertFalse(await manager.reload_registered_plugin("demo"))
            self.assertIs(manager.loaded_plugins["demo"], old_instance)
            self.assertEqual(
                manager.plugin_classes,
                {"demo": old_class, "unrelated_from_failed_scan": object},
            )
            self.assertEqual(
                manager.plugin_paths,
                {
                    "demo": "C:/plugins/demo",
                    "unrelated_from_failed_scan": "C:/plugins/other",
                },
            )

    async def test_v2_unload_is_skipped_until_on_load_was_called(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()

            class V2:
                def __init__(self):
                    self._on_load_called = False
                    self.unload_count = 0

                async def on_unload(self):
                    self.unload_count += 1

            plugin = types.SimpleNamespace(plugin_name="v2", v2_plugin=V2())
            self.assertTrue(await manager._call_plugin_unload(plugin))
            self.assertEqual(plugin.v2_plugin.unload_count, 0)
            plugin.v2_plugin._on_load_called = True
            self.assertTrue(await manager._call_plugin_unload(plugin))
            self.assertEqual(plugin.v2_plugin.unload_count, 1)

    async def test_legacy_sync_unload_hook_is_supported(self) -> None:
        with isolated_plugin_manager_module() as module:
            manager = module.PluginManager()
            plugin = types.SimpleNamespace(plugin_name="legacy", unload_count=0)

            def unload():
                plugin.unload_count += 1

            plugin.on_plugin_unload = unload
            self.assertTrue(await manager._call_plugin_unload(plugin))
            self.assertEqual(plugin.unload_count, 1)


if __name__ == "__main__":
    unittest.main()
