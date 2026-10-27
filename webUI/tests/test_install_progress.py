from __future__ import annotations

import asyncio
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from install_output import InstallSnapshot, run_install_command  # noqa: E402
from setup_deployment import DependencyInstaller  # noqa: E402
from multimodal_runtime import MultimodalRuntimeManager  # noqa: E402


class TerminalProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_redraw_streams_before_exit_and_preserves_exit_code(self):
        frames = []
        started = time.monotonic()

        async def on_frame(frame):
            self.assertIsInstance(frame, InstallSnapshot)
            self.assertNotIn("\x1b", frame)
            frames.append((time.monotonic() - started, str(frame)))

        script = (
            "import sys,time; "
            "print('tty=' + str(sys.stderr.isatty()),flush=True); "
            "print('torch 10/100 MiB',end='\\r',flush=True); time.sleep(.6); "
            "print('torch 50/100 MiB',end='\\r',flush=True); time.sleep(.6); "
            "print('torch 100/100 MiB\\n安装完成',flush=True); sys.exit(7)"
        )
        status = await asyncio.wait_for(run_install_command(
            [sys.executable, "-c", script], cwd=WEBUI_DIR,
            env={**os.environ, "PYTHONUTF8": "1"}, callback=on_frame,
        ), timeout=10)
        self.assertEqual(status, 7)
        self.assertTrue(any("tty=True" in frame for _, frame in frames))
        self.assertTrue(any("10/100" in frame for _, frame in frames), frames)
        self.assertTrue(any("50/100" in frame for _, frame in frames), frames)
        self.assertIn("安装完成", frames[-1][1])
        self.assertLess(frames[0][0], time.monotonic() - started - .5)

    async def test_history_and_cursor_redraw_are_plain_text(self):
        frames = []

        async def on_frame(frame):
            frames.append(frame)

        script = (
            "import sys,time; "
            "print('\\n'.join('line-'+str(i) for i in range(80)),flush=True); "
            "print('torch 10 MiB\\ntimm 1 MiB',flush=True); time.sleep(.3); "
            "print('\\x1b[2A\\r\\x1b[2Ktorch 50 MiB\\n\\x1b[2Ktimm 2 MiB',flush=True)"
        )
        status = await asyncio.wait_for(run_install_command(
            [sys.executable, "-c", script], cwd=WEBUI_DIR, env=os.environ.copy(), callback=on_frame,
        ), timeout=10)
        self.assertEqual(status, 0)
        self.assertIn("line-0", frames[-1])
        self.assertIn("line-79", frames[-1])
        self.assertIn("torch 50 MiB", frames[-1])
        self.assertNotIn("torch 10 MiB", frames[-1])
        self.assertNotIn("\x1b", frames[-1])

    async def test_missing_command_reports_error(self):
        with self.assertRaises(FileNotFoundError):
            await run_install_command(
                ["nachobot-missing-installer-command"], cwd=WEBUI_DIR, env=os.environ.copy(), callback=None,
            )

    async def test_cancel_stops_only_the_installer(self):
        import psutil

        with tempfile.TemporaryDirectory(prefix="install progress ") as temp:
            pid_path = Path(temp) / "pid.txt"
            ready = asyncio.Event()

            async def on_frame(frame):
                if "ready" in frame:
                    ready.set()

            script = (
                "import os,sys,time; from pathlib import Path; "
                "Path(sys.argv[1]).write_text(str(os.getpid())); "
                "print('ready',flush=True); time.sleep(30)"
            )
            task = asyncio.create_task(run_install_command(
                [sys.executable, "-c", script, str(pid_path)],
                cwd=Path(temp), env=os.environ.copy(), callback=on_frame,
            ))
            await asyncio.wait_for(ready.wait(), 10)
            pid = int(pid_path.read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for _ in range(50):
                if not psutil.pid_exists(pid):
                    break
                await asyncio.sleep(.05)
            self.assertFalse(psutil.pid_exists(pid))

    @unittest.skipUnless(shutil.which("uv"), "uv is required for the real download test")
    async def test_real_uv_download_reports_intermediate_byte_progress(self):
        # A valid wheel served slowly on loopback exercises uv's real renderer,
        # without touching a live environment, external network, or shared cache.
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("progress_fixture/payload.bin", b"x" * (2 * 1024 * 1024))
            archive.writestr("progress_fixture-1.0.dist-info/METADATA",
                             "Metadata-Version: 2.1\nName: progress-fixture\nVersion: 1.0\n")
            archive.writestr("progress_fixture-1.0.dist-info/WHEEL",
                             "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr("progress_fixture-1.0.dist-info/RECORD", "")
        payload = wheel.getvalue()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                start, end = 0, len(payload) - 1
                requested = self.headers.get("Range")
                if requested:
                    first, last = requested.removeprefix("bytes=").split("-")
                    if first:
                        start = int(first)
                        end = int(last) if last else end
                    else:
                        start = len(payload) - int(last)
                self.send_response(206 if requested else 200)
                self.send_header("Content-Length", str(end - start + 1))
                if requested:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                try:
                    for offset in range(start, end + 1, 16384):
                        self.wfile.write(payload[offset:min(offset + 16384, end + 1)])
                        self.wfile.flush()
                        time.sleep(.025)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        frames = []

        async def on_frame(frame):
            frames.append(str(frame))

        try:
            with tempfile.TemporaryDirectory(prefix="uv progress ") as temp:
                url = f"http://127.0.0.1:{server.server_port}/progress_fixture-1.0-py3-none-any.whl"
                status = await asyncio.wait_for(run_install_command(
                    ["uv", "pip", "install", "--target", str(Path(temp) / "site"),
                     "--python", sys.executable, "--no-deps", "--no-cache", url],
                    cwd=Path(temp), env={**os.environ, "UV_NO_PROGRESS": "1"}, callback=on_frame,
                ), timeout=20)
            self.assertEqual(status, 0, frames)
            byte_frames = {frame for frame in frames if "MiB/" in frame or "KiB/" in frame}
            self.assertGreaterEqual(len(byte_frames), 2, frames)
            self.assertTrue(any("Installed 1 package" in frame for frame in frames), frames)

            # A service's implicit uv run install uses the same live progress,
            # then its original application starts with synchronization disabled.
            from process_manager import ProcessManager

            with tempfile.TemporaryDirectory(prefix="service progress ") as temp:
                project = Path(temp)
                (project / "pyproject.toml").write_text(
                    '[project]\nname="service-progress"\nversion="1.0"\nrequires-python=">=3.11"\n'
                    f'dependencies=["progress-fixture @ {url}"]\n', encoding="utf-8",
                )
                service = ProcessManager(project)
                service.subscribe("fixture", on_frame)
                launch = await asyncio.wait_for(service._prepare_service_dependencies(
                    "fixture", ["uv", "run", "--python", sys.executable, "python", "-c", "import progress_fixture"],
                    project, {**os.environ, "UV_NO_CACHE": "1"},
                ), timeout=20)
                self.assertIn("--no-sync", launch)
                self.assertTrue(any(isinstance(frame, InstallSnapshot)
                                    for frame in service.get_log_history("fixture")))
                child = await asyncio.create_subprocess_exec(
                    *launch, cwd=project, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                output, _ = await child.communicate()
                self.assertEqual(child.returncode, 0, output.decode(errors="replace"))
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)

    async def test_cancel_also_stops_installer_build_children(self):
        import psutil

        ready = asyncio.Event()
        with tempfile.TemporaryDirectory(prefix="installer children ") as temp:
            pids_file = Path(temp) / "pids"

            async def on_frame(frame):
                if "ready" in frame:
                    ready.set()

            script = (
                "import os,sys,time,subprocess; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
                "Path(sys.argv[1]).write_text(str(os.getpid())+','+str(child.pid)); "
                "print('ready',flush=True);time.sleep(30)"
            )
            task = asyncio.create_task(run_install_command(
                [sys.executable, "-c", script, str(pids_file)],
                cwd=Path(temp), env=os.environ.copy(), callback=on_frame,
            ))
            await asyncio.wait_for(ready.wait(), 10)
            pids = [int(value) for value in pids_file.read_text().split(",")]
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for pid in pids:
                if psutil.pid_exists(pid):
                    self.assertEqual(psutil.Process(pid).status(), psutil.STATUS_ZOMBIE)


class InstallerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_marks_terminal_frames_for_replacement(self):
        import server

        messages = []
        ws = AsyncMock()
        ws.receive_text.return_value = json.dumps({"tasks": [{
            "id": "core", "name": "Core", "type": "uv", "dir": "NachoBot",
        }]})

        async def send(text):
            messages.append(json.loads(text))

        async def install(task, callback):
            await callback("preparing\n")
            await callback(InstallSnapshot("torch 10/100 MiB"))
            await callback(InstallSnapshot("torch 50/100 MiB"))
            return {"status": "ok", "message": "done"}

        ws.send_text.side_effect = send
        with (
            patch.object(server.webui_security, "authorize_websocket", new_callable=AsyncMock, return_value=True),
            patch.object(server.EnvironmentChecker, "check_git", return_value={"status": "ok"}),
            patch.object(server.DependencyInstaller, "install", side_effect=install),
            patch.object(server.bilibili_login_manager, "mark_dependency_task"),
        ):
            await server.ws_setup_install(ws)
        logs = [msg for msg in messages if msg["type"] == "log"]
        self.assertEqual([msg["replace"] for msg in logs], [False, True, True])
        self.assertEqual(logs[-1]["line"], "torch 50/100 MiB")
        self.assertEqual(messages[-1]["type"], "all_done")

    async def test_regular_and_locked_sync_keep_environment_isolation(self):
        for locked in (False, True):
            with self.subTest(locked=locked), patch(
                "setup_deployment.run_install_command", new_callable=AsyncMock, return_value=0,
            ) as run, patch.dict(os.environ, {"VIRTUAL_ENV": "wrong", "UV_PROJECT_ENVIRONMENT": "wrong"}):
                result = await DependencyInstaller._run_uv_sync(WEBUI_DIR, None, locked=locked)
                self.assertEqual(result["status"], "ok")
                command = run.call_args.args[0]
                self.assertEqual("--locked" in command, locked)
                env = run.call_args.kwargs["env"]
                self.assertNotIn("VIRTUAL_ENV", env)
                self.assertNotIn("UV_PROJECT_ENVIRONMENT", env)

    async def test_uv_failure_does_not_become_success(self):
        with patch("setup_deployment.run_install_command", new_callable=AsyncMock, return_value=9):
            result = await DependencyInstaller._run_uv_sync(WEBUI_DIR, None)
        self.assertEqual(result["status"], "error")
        self.assertIn("9", result["message"])

    async def test_multimodal_failure_does_not_validate_or_mark_installed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (
                patch.object(MultimodalRuntimeManager, "ADAPTER_DIR", root),
                patch.object(MultimodalRuntimeManager, "_prepare_project", return_value=root),
                patch.object(MultimodalRuntimeManager, "_validate_runtime") as validate,
                patch("multimodal_runtime.run_install_command", new_callable=AsyncMock, return_value=8) as run,
            ):
                for profile in ("gpu", "cpu"):
                    result = await MultimodalRuntimeManager.install(profile)
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(run.call_args.kwargs["env"]["UV_PROJECT_ENVIRONMENT"],
                                     str(MultimodalRuntimeManager.env_dir(profile)))
                    self.assertFalse(MultimodalRuntimeManager._marker_path(profile).exists())
                validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
