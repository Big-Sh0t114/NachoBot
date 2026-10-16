import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.windows import ProcessRecord, WindowsServiceError, WindowsSystem
from service.manager import ServiceError, Supervisor, _RECOVERY_SHIM_SHA256


class QQExecutableDiscoveryTests(unittest.TestCase):
    def discover(self, images):
        system = WindowsSystem.__new__(WindowsSystem)
        records = {i: ProcessRecord(i, 0, image, "", 100) for i, image in enumerate(images, 1)}
        with patch.object(system, "process_table", return_value=records):
            return system.discover_qq_executable()

    def test_many_instances_of_one_installation_share_path(self):
        path = r"C:\QQ\QQ.exe"
        self.assertEqual(self.discover([path, path, r"C:\Python\python.exe"]), path)

    def test_multiple_installations_are_not_guessed(self):
        with self.assertRaisesRegex(WindowsServiceError, "^qq_executable_ambiguous$"):
            self.discover([r"C:\QQ\QQ.exe", r"D:\QQ\QQ.exe"])

    def test_missing_or_relative_image_is_rejected(self):
        for images in ([], ["QQ.exe"], [r"C:\QQ\helper.exe"]):
            with self.subTest(images=images):
                with self.assertRaisesRegex(WindowsServiceError, "^qq_executable_not_found$"):
                    self.discover(images)

    def make_supervisor(self, executable=""):
        supervisor = Supervisor.__new__(Supervisor)
        supervisor.config = {"paths": {"qq_exe": executable, "loader_root": "loader", "python_exe": "python", "node_exe": "node"}}
        supervisor._discovery_config = copy.deepcopy(supervisor.config)
        supervisor.project_root = Path("project")
        supervisor.system = Mock()
        supervisor.system.discover_qq_executable.return_value = r"C:\QQ\QQ.exe"
        return supervisor

    def test_supervisor_uses_discovered_path_in_adapter_selection(self):
        supervisor = self.make_supervisor()
        with patch.object(Path, "is_file", return_value=True), patch("service.manager.shutil.which", return_value="node"), patch("service.manager._sha256_file", return_value=_RECOVERY_SHIM_SHA256.lower()):
            supervisor._validate_static_dependencies()
        supervisor.system.discover_qq_executable.assert_called_once()
        self.assertEqual(supervisor.config["paths"]["qq_exe"], r"C:\QQ\QQ.exe")
        self.assertEqual(supervisor.config, supervisor._discovery_config)

    def test_explicit_path_skips_discovery(self):
        supervisor = self.make_supervisor(r"D:\QQ\QQ.exe")
        with patch.object(Path, "is_file", return_value=True), patch("service.manager.shutil.which", return_value="node"), patch("service.manager._sha256_file", return_value=_RECOVERY_SHIM_SHA256.lower()):
            supervisor._validate_static_dependencies()
        supervisor.system.discover_qq_executable.assert_not_called()

    def test_discovery_failure_does_not_modify_binding_configuration(self):
        supervisor = self.make_supervisor()
        supervisor.system.discover_qq_executable.side_effect = WindowsServiceError("qq_executable_ambiguous")
        with self.assertRaises(ServiceError) as failure:
            supervisor._validate_static_dependencies()
        self.assertEqual(failure.exception.phase, "waiting_target")
        self.assertEqual(supervisor.config["paths"]["qq_exe"], "")


if __name__ == "__main__":
    unittest.main()
