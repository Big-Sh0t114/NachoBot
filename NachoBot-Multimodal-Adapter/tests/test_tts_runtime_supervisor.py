from pathlib import Path
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tts_runtime_manager as manager
from scripts.tts_runtime_manager import TTSRuntimeSupervisor, resolve_engine, resolve_private_port


class _Child:
    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


class _Pipeline:
    app = object()

    def __init__(self):
        self.alive = True
        self.stopped = False

    def set_backend_alive(self, alive):
        self.alive = alive

    def stop(self):
        self.stopped = True


class RuntimeSupervisorTests(unittest.TestCase):
    def test_exactly_one_backend_and_private_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base.toml"
            base.write_text('[enabled_tts]\nenabled=["Vox"]\n', encoding="utf-8")
            (root / "vox.toml").write_text('[tts]\nport=9881\n', encoding="utf-8")
            self.assertEqual(resolve_engine(base), "voxcpm")
            self.assertEqual(resolve_private_port(base, "voxcpm"), 9881)
            base.write_text('[enabled_tts]\nenabled=["Vox","GPT_Sovits"]\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                resolve_engine(base)

    def test_vox_health_and_fixed_pipeline_gate_public_start(self):
        child = _Child()
        pipeline = _Pipeline()
        with mock.patch.object(
            TTSRuntimeSupervisor,
            "_raw_ready",
            side_effect=[False, True],
        ):
            supervisor = TTSRuntimeSupervisor(
                PROJECT_ROOT / "configs" / "base.toml",
                engine="voxcpm",
                popen_factory=lambda *args, **kwargs: child,
                pipeline_factory=lambda *args, **kwargs: pipeline,
                startup_timeout=2,
            )
            with mock.patch.object(
                supervisor,
                "_build_raw_spec",
                return_value=(["fake-voxcpm", "--port", "9881"], PROJECT_ROOT, {}),
            ):
                result = supervisor.start()
            self.assertIs(result, pipeline)
            self.assertTrue(pipeline.alive)
            with mock.patch.object(manager.subprocess, "run") as kill_tree:
                supervisor.stop()
            if os.name == "nt":
                kill_tree.assert_called_once()
                self.assertEqual(kill_tree.call_args.args[0][:4], ["taskkill", "/PID", "4242", "/T"])
            else:
                self.assertGreaterEqual(len(child.wait_calls), 1)
            self.assertTrue(pipeline.stopped)

    def test_child_exit_marks_fixed_public_pipeline_unhealthy(self):
        child = _Child()
        pipeline = _Pipeline()
        supervisor = TTSRuntimeSupervisor(
            PROJECT_ROOT / "configs" / "base.toml",
            engine="voxcpm",
            popen_factory=lambda *args, **kwargs: child,
            pipeline_factory=lambda *args, **kwargs: pipeline,
            startup_timeout=1,
        )
        with mock.patch.object(supervisor, "wait_for_backend"), mock.patch.object(
            supervisor, "_validate_fixed_client", return_value=pipeline
        ), mock.patch.object(
            supervisor,
            "_build_raw_spec",
            return_value=(["fake-voxcpm", "--port", "9881"], PROJECT_ROOT, {}),
        ):
            supervisor.start()
        child.returncode = 17
        supervisor._monitor_stop.set()
        # The monitor's observable contract is also directly exercised to
        # avoid a timing-dependent sleep in the unit test.
        supervisor._monitor_stop.clear()
        supervisor._monitor_child()
        self.assertFalse(pipeline.alive)
        with mock.patch.object(manager.subprocess, "run"):
            supervisor.stop()

    def test_startup_failure_reaps_owned_backend_tree(self):
        child = _Child()
        supervisor = TTSRuntimeSupervisor(
            PROJECT_ROOT / "configs" / "base.toml",
            engine="voxcpm",
            popen_factory=lambda *args, **kwargs: child,
            startup_timeout=1,
        )
        with mock.patch.object(
            supervisor,
            "_build_raw_spec",
            return_value=(["fake-voxcpm", "--port", "9881"], PROJECT_ROOT, {}),
        ), mock.patch.object(
            supervisor,
            "wait_for_backend",
            side_effect=RuntimeError("readiness failed"),
        ), mock.patch.object(manager.subprocess, "run") as kill_tree:
            with self.assertRaisesRegex(RuntimeError, "readiness failed"):
                supervisor.start()

        if os.name == "nt":
            kill_tree.assert_called_once()
            command = kill_tree.call_args.args[0]
            self.assertEqual(command[:4], ["taskkill", "/PID", "4242", "/T"])
        self.assertGreaterEqual(len(child.wait_calls), 1)

    def test_public_setup_failure_stops_pipeline_and_reaps_owned_backend_tree(self):
        child = _Child()
        pipeline = _Pipeline()
        supervisor = TTSRuntimeSupervisor(
            PROJECT_ROOT / "configs" / "base.toml",
            engine="voxcpm",
            popen_factory=lambda *args, **kwargs: child,
            pipeline_factory=lambda *args, **kwargs: pipeline,
            startup_timeout=1,
        )
        fake_uvicorn = types.SimpleNamespace(
            Config=mock.Mock(side_effect=RuntimeError("public config failed before bind")),
            Server=mock.Mock(),
        )
        with mock.patch.object(
            supervisor,
            "_build_raw_spec",
            return_value=(['fake-voxcpm', '--port', '9881'], PROJECT_ROOT, {}),
        ), mock.patch.object(
            supervisor,
            "wait_for_backend",
        ), mock.patch.object(
            supervisor,
            "_validate_fixed_client",
            return_value=pipeline,
        ), mock.patch.dict(
            sys.modules,
            {"uvicorn": fake_uvicorn},
        ), mock.patch.object(manager.subprocess, "run") as kill_tree:
            with self.assertRaisesRegex(RuntimeError, "public config failed before bind"):
                supervisor.run()

        self.assertTrue(pipeline.stopped)
        self.assertGreaterEqual(len(child.wait_calls), 1)
        if os.name == "nt":
            kill_tree.assert_called_once()
            self.assertEqual(
                kill_tree.call_args.args[0][:4],
                ["taskkill", "/PID", "4242", "/T"],
            )

    def test_public_pipeline_failure_is_single_attempt_and_chains_original(self):
        child = _Child()
        original = ModuleNotFoundError("missing public dependency")
        pipeline_factory = mock.Mock(side_effect=original)
        supervisor = TTSRuntimeSupervisor(
            PROJECT_ROOT / "configs" / "base.toml",
            engine="voxcpm",
            popen_factory=lambda *args, **kwargs: child,
            pipeline_factory=pipeline_factory,
            startup_timeout=900,
        )
        with mock.patch.object(
            supervisor,
            "_build_raw_spec",
            return_value=(["fake-voxcpm", "--port", "9881"], PROJECT_ROOT, {}),
        ), mock.patch.object(supervisor, "wait_for_backend"), mock.patch.object(
            manager.subprocess, "run"
        ):
            with self.assertRaisesRegex(RuntimeError, "public pipeline initialization failed") as raised:
                supervisor.start()

        self.assertEqual(pipeline_factory.call_count, 1)
        self.assertIs(raised.exception.__cause__, original)
        self.assertGreaterEqual(len(child.wait_calls), 1)

    def test_direct_script_context_loads_repository_root_main_without_model(self):
        manager_path = PROJECT_ROOT / "scripts" / "tts_runtime_manager.py"
        main_path = PROJECT_ROOT / "main.py"
        probe = f"""
import runpy
import sys
import types
from pathlib import Path

sys.modules['main'] = types.ModuleType('main')
namespace = runpy.run_path({str(manager_path)!r}, run_name='probe')
module = namespace['_load_public_main']()
assert Path(module.__file__).resolve() == Path({str(main_path)!r}).resolve()
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_supervisor_command_is_actual_backend_not_serve_raw_wrapper(self):
        supervisor = TTSRuntimeSupervisor(
            PROJECT_ROOT / "configs" / "base.toml",
            engine="voxcpm",
        )
        with mock.patch.object(
            supervisor,
            "_build_raw_spec",
            return_value=(["python", "vox_api_server.py", "--port", "9881"], PROJECT_ROOT, {}),
        ):
            command = supervisor.child_command()
        self.assertNotIn("serve-raw", command)
        self.assertIn("vox_api_server.py", command)

    def test_gpt_module_never_imports_emotion_classifier(self):
        source = (
            PROJECT_ROOT / "src" / "tts" / "backends" / "GPT_Sovits" / "tts_model.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("emotion_classifier", source)


if __name__ == "__main__":
    unittest.main()
