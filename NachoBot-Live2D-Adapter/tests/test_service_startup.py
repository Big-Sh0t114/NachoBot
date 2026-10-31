"""Independent model consoles and repository-level launch orchestration."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "NachoBot-Multimodal-Adapter" / "scripts"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
        spec.loader.exec_module(module)
    return module


services = load("standalone_services_test", SCRIPTS / "service_launcher.py")
desktop = load("desktop_startup_test", ROOT / "NachoBot-Live2D-Adapter" / "scripts" / "launch_desktop_pet.py")


class ServiceStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="independent service ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "configs").mkdir()
        (self.root / "startup.toml").write_text("[components]\ntts=true\nperception=true\n")
        (self.root / "configs/base.toml").write_text('[server]\nhost="127.0.0.1"\nport=19880\n')
        (self.root / "configs/perception.toml").write_text(
            '[perception]\nhost="127.0.0.1"\nport=19874\n'
        )
        python = self.root / ".venv/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        self.repository = self.root / "repository"
        self.shared_startup = self.repository / "NachoBot-Multimodal-Adapter" / "startup.toml"
        self.shared_startup.parent.mkdir(parents=True)
        self.shared_startup.write_text("[components]\ncore=true\ntts=true\nperception=true\n")
        root_patch = patch.object(desktop, "ROOT", self.repository)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def test_settings_select_both_independent_services_and_preserve_model_configs(self):
        selected = services.settings(self.root)
        self.assertEqual([item["name"] for item in selected], ["tts", "perception"])
        self.assertEqual([item["port"] for item in selected], [19880, 19874])
        self.assertIn("scripts/container_tts_entrypoint.py", selected[0]["command"])
        self.assertIn("nachobot_multimodal.api_server", selected[1]["command"])
        (self.root / "startup.toml").write_text("[components]\ntts=false\nperception=false\n")
        shutil.rmtree(self.root / ".venv")
        shutil.rmtree(self.root / "configs")
        self.assertEqual(services.settings(self.root), [])

    def test_malformed_flags_and_missing_environments_fail_before_spawning(self):
        (self.root / "startup.toml").write_text('[components]\ntts="true"\n')
        with self.assertRaises(ValueError):
            services.settings(self.root)
        (self.root / "startup.toml").write_text(
            '[components]\nperception=true\n[runtime]\nprofile="cpu"\n'
        )
        with self.assertRaisesRegex(ValueError, "environment missing"):
            services.settings(self.root)

    def test_core_off_skips_all_model_environment_and_config_requirements(self):
        (self.root / "startup.toml").write_text(
            "[components]\ncore=false\ntts=true\nperception=true\n"
        )
        shutil.rmtree(self.root / ".venv")
        shutil.rmtree(self.root / "configs")
        self.assertEqual(services.settings(self.root), [])
        self.shared_startup.write_text("[components]\ncore=false\ntts=true\nperception=true\n")
        config = self.root / "pet.toml"
        config.write_text('[runtime]\nmode="desktop_pet"\n')
        with patch.object(
            desktop.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            self.assertEqual(desktop.main(["--config", str(config)]), 0)
        run.assert_called_once()
        self.assertIn("live2d_adapter.launcher", run.call_args.args[0])
        self.assertEqual(
            run.call_args.kwargs["env"]["NACHOBOT_DESKTOP_STARTUP_CONFIG"], str(self.shared_startup)
        )

    def test_loading_lock_and_external_service_are_reused_without_a_new_process(self):
        item = services.settings(self.root)[0]
        lock = services.lock_for(item, self.root)
        self.assertTrue(lock.acquire())
        try:
            with patch.object(services.subprocess, "Popen") as spawn:
                services.ensure_service(item, self.root)
            spawn.assert_not_called()
        finally:
            lock.close()
        with (
            patch.object(services, "probe", return_value="ready"),
            patch.object(services.subprocess, "Popen") as spawn,
        ):
            services.ensure_service(item, self.root, owner=Mock())
        spawn.assert_not_called()

    def test_occupied_incompatible_port_fails_without_launching_or_stopping_anything(self):
        item = services.settings(self.root)[0]
        with (
            patch.object(services, "probe", side_effect=RuntimeError("incompatible")),
            patch.object(services.subprocess, "Popen") as spawn,
            self.assertRaises(RuntimeError),
        ):
            services.ensure_service(item, self.root)
        spawn.assert_not_called()

    def test_repository_entry_starts_services_before_pet_and_skips_live_or_logs(self):
        config = self.root / "pet.toml"
        config.write_text('[runtime]\nmode="desktop_pet"\n')
        with patch.object(
            desktop.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            self.assertEqual(desktop.main(["--config", str(config)]), 0)
        self.assertIn("service_launcher.py", run.call_args_list[0].args[0][1])
        ensure_command = run.call_args_list[0].args[0]
        self.assertIn("--owner-pid", ensure_command)
        lease = Path(ensure_command[ensure_command.index("--owner-lease") + 1])
        self.assertFalse(lease.exists(), "normal desktop exit must revoke its service lease")
        self.assertIn("live2d_adapter.launcher", run.call_args_list[1].args[0])
        for extra in (["--mode", "live"], ["--show-logs", "ALL"]):
            with patch.object(
                desktop.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run:
                self.assertEqual(desktop.main(["--config", str(config), *extra]), 0)
            run.assert_called_once()
            self.assertIn("live2d_adapter.launcher", run.call_args.args[0])
        with patch.object(
            desktop.subprocess, "run", return_value=SimpleNamespace(returncode=4)
        ) as run:
            self.assertEqual(desktop.main(["--config", str(config)]), 4)
        run.assert_called_once()
        ensure_command = run.call_args.args[0]
        self.assertFalse(Path(ensure_command[ensure_command.index("--owner-lease") + 1]).exists())

    @unittest.skipUnless(os.name == "nt", "Windows standalone console lifecycle")
    def test_standalone_service_survives_unrelated_pet_exit(self):
        self._exercise_console_lifecycle()

    @unittest.skipUnless(os.name == "nt", "Windows desktop-owned service lifecycle")
    def test_desktop_owned_service_exits_on_normal_session_end(self):
        self._exercise_console_lifecycle("lease")

    @unittest.skipUnless(os.name == "nt", "Windows desktop-owned service lifecycle")
    def test_desktop_owned_service_exits_when_launcher_is_killed(self):
        self._exercise_console_lifecycle("process")

    def _exercise_console_lifecycle(self, owner_exit=None):
        # No model imports, devices or real model-service ports are used.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        fixture = self.root / "fixture.py"
        fixture.write_text(
            'import socket,time,sys\ns=socket.socket()\ns.bind(("127.0.0.1",int(sys.argv[1])))\ns.listen()\n'
            'print("service fixture ready",flush=True)\ntime.sleep(60)\n'
        )
        python = getattr(sys, "_base_executable", sys.executable)
        item = dict(
            name="tts", host="127.0.0.1", port=port, command=[python, "-u", str(fixture), str(port)]
        )
        result = self.root / "result.json"
        owner_process = None
        owner_argument = ""
        lease = self.root / "owner-active"
        if owner_exit:
            lease.touch()
            owner_process = subprocess.Popen(
                [python, "-c", "import time; time.sleep(60)"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            owner = services.SessionOwner(owner_process.pid, None, str(lease))
            self.assertTrue(owner.alive())
            wrong_identity = services.SessionOwner(owner_process.pid, owner.created + 1, str(lease))
            self.assertFalse(wrong_identity.alive())
            wrong_identity.close()
            owner_argument = (
                f", owner=SessionOwner({owner_process.pid},{owner.created},{str(lease)!r})"
            )
            owner.close()
        wrapper = self.root / "worker_fixture.py"
        wrapper.write_text(
            f"import sys\nsys.path.insert(0,{str(SCRIPTS)!r})\nfrom service_launcher import worker, SessionOwner\n"
            f"from pathlib import Path\nraise SystemExit(worker({item!r},Path({str(result)!r}),Path({str(self.root)!r}){owner_argument}))\n"
        )
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        worker = subprocess.Popen(
            [python, "-u", str(wrapper)],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup,
        )

        def listening():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return True
            except OSError:
                return False

        try:
            deadline = time.monotonic() + 8
            while not listening() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(listening())
            self.assertEqual(json.loads(result.read_text())["status"], "started (models loading)")
            subprocess.run([python, "-c", "pass"], check=True)  # pet session exits independently
            self.assertTrue(listening())
            self.assertIsNone(worker.poll())
            if owner_exit is None:
                # A desktop session reuses this standalone worker and ends;
                # its lease must never get attached to the existing service.
                lease.touch()
                desktop_owner = services.SessionOwner(os.getpid(), None, str(lease))
                try:
                    services.ensure_service(item, self.root, owner=desktop_owner)
                    lease.unlink()
                    time.sleep(0.3)
                    self.assertTrue(listening())
                    self.assertIsNone(worker.poll())
                finally:
                    desktop_owner.close()
            if owner_exit == "lease":
                lease.unlink()
                self.assertEqual(worker.wait(timeout=8), 0)
            elif owner_exit == "process":
                owner_process.terminate()
                owner_process.wait(timeout=5)
                self.assertEqual(worker.wait(timeout=8), 0)
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.wait(timeout=5)
            if owner_process is not None:
                if owner_process.poll() is None:
                    owner_process.terminate()
                owner_process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while listening() and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertFalse(listening(), "service child survived its independent console")
