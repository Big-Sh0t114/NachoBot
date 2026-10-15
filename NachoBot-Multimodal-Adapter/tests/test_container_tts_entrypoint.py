import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = PROJECT_ROOT / "scripts" / "container_tts_entrypoint.py"


def load_entrypoint():
    spec = importlib.util.spec_from_file_location("container_tts_entrypoint_lifecycle_test", ENTRYPOINT)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load the TTS entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContainerTtsEntrypointTests(unittest.TestCase):
    def test_windows_entrypoint_stays_foreground_and_forwards_child_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = root / "scripts" / "tts_runtime_manager.py"
            manager.parent.mkdir()
            manager.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    import sys
                    import time
                    from pathlib import Path

                    root = Path(os.environ["ENTRYPOINT_TEST_ROOT"])
                    stdin_line = sys.stdin.readline().rstrip("\\r\\n")
                    (root / "child.json").write_text(json.dumps({
                        "argv": sys.argv,
                        "stdin": stdin_line,
                        "inherited": os.environ.get("ENTRYPOINT_TEST_ENV"),
                    }), encoding="utf-8")
                    print("entrypoint-child-stdout-marker", flush=True)
                    print("entrypoint-child-stderr-marker", file=sys.stderr, flush=True)
                    (root / "child.started").write_text("started", encoding="utf-8")
                    deadline = time.monotonic() + 2.5
                    while not (root / "release-child").exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not (root / "release-child").exists():
                        raise SystemExit(93)
                    raise SystemExit(23)
                    """
                ),
                encoding="utf-8",
            )
            config = root / "base.toml"
            config.write_text('[enabled_tts]\nenabled = ["Vox"]\n', encoding="utf-8")
            wrapper = textwrap.dedent(
                """
                import importlib.util
                import os
                import sys
                import types
                from pathlib import Path

                script = Path(sys.argv[1])
                spec = importlib.util.spec_from_file_location("entrypoint_under_test", script)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.ADAPTER_ROOT = Path(sys.argv[2])
                module.os = types.SimpleNamespace(name="nt", environ=os.environ)
                sys.argv = [str(script), "--host", "127.0.0.1", "--port", "19880",
                            "--backend-port", "19881", "--config", sys.argv[3]]
                module.main()
                """
            )
            env = os.environ.copy()
            env["ENTRYPOINT_TEST_ROOT"] = str(root)
            env["ENTRYPOINT_TEST_ENV"] = "forwarded-environment-marker"
            parent = subprocess.Popen(
                [sys.executable, "-c", wrapper, str(ENTRYPOINT), str(root), str(config)],
                cwd=PROJECT_ROOT,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout = ""
            stderr = ""
            try:
                assert parent.stdin is not None
                parent.stdin.write("forwarded-stdin-marker\n")
                parent.stdin.close()
                deadline = time.monotonic() + 2.0
                started = root / "child.started"
                while not started.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started.exists(), "the bounded manager child did not start")
                self.assertIsNone(parent.poll(), "the entrypoint exited while its manager child was alive")
            finally:
                (root / "release-child").write_text("release", encoding="utf-8")
                stdout, stderr = parent.communicate(timeout=3)

            self.assertEqual(parent.returncode, 23, stderr or stdout)
            self.assertIn("entrypoint-child-stdout-marker", stdout)
            self.assertIn("entrypoint-child-stderr-marker", stderr)
            child = json.loads((root / "child.json").read_text(encoding="utf-8"))
            self.assertEqual(
                child["argv"],
                [
                    str(manager),
                    "serve",
                    "--engine",
                    "voxcpm",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "19880",
                    "--backend-port",
                    "19881",
                ],
            )
            self.assertEqual(child["stdin"], "forwarded-stdin-marker")
            self.assertEqual(child["inherited"], "forwarded-environment-marker")

    def test_unix_entrypoint_keeps_execv_command(self):
        module = load_entrypoint()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = root / "scripts" / "tts_runtime_manager.py"
            config = root / "base.toml"
            config.write_text('[enabled_tts]\nenabled = ["Vox"]\n', encoding="utf-8")
            execv = mock.Mock()
            module.ADAPTER_ROOT = root
            module.os = types.SimpleNamespace(name="posix", environ={}, execv=execv)
            argv = [
                str(ENTRYPOINT),
                "--host",
                "127.0.0.1",
                "--port",
                "19880",
                "--backend-port",
                "19881",
                "--config",
                str(config),
            ]
            with mock.patch.object(sys, "argv", argv):
                module.main()

            command = [
                sys.executable,
                str(manager),
                "serve",
                "--engine",
                "voxcpm",
                "--host",
                "127.0.0.1",
                "--port",
                "19880",
                "--backend-port",
                "19881",
            ]
            execv.assert_called_once_with(sys.executable, command)


if __name__ == "__main__":
    unittest.main()
