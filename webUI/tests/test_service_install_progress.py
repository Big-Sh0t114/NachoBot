from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from install_output import InstallSnapshot, PROGRESS_PREFIX, decode_install_progress, uv_service_install_plan  # noqa: E402
from process_manager import ProcessManager  # noqa: E402
import process_manager  # noqa: E402


class ServiceInstallProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_all_registered_uv_services_have_a_progress_plan(self):
        checked = []
        for service in process_manager.SERVICE_DEFS.values():
            if service.cmd[:2] == ["uv", "run"]:
                install, launch = uv_service_install_plan(service.cmd)
                self.assertEqual(install[:3], ["uv", "sync", "--inexact"])
                self.assertEqual(launch[3:], service.cmd[2:])
                self.assertIn("--no-sync", launch)
                checked.append(service.id)
        self.assertTrue({"nachobot", "napcat_adapter", "snowluma_adapter", "live2d", "universalvc",
                         "bilibili", "discord_adapter", "qqcall"}.issubset(checked))

    def test_optional_audio_and_cross_project_options_are_preserved(self):
        command = ["uv", "run", "--project", "../NachoBot", "--locked", "--extra", "qqcall-audio",
                   "python", "main.py", "--application-argument"]
        install, launch = uv_service_install_plan(command)
        self.assertEqual(install[3:], command[2:7])
        self.assertNotIn("--application-argument", install)
        self.assertEqual(launch[3:], command[2:])
        self.assertIsNone(uv_service_install_plan(["python", "service.py"]))

    async def test_failed_service_sync_blocks_launch_and_preserves_progress(self):
        manager = ProcessManager(WEBUI_DIR.parent)

        async def run(command, **kwargs):
            await kwargs["callback"](InstallSnapshot("torch 10/100 MiB", "sync-1"))
            return 9

        with patch("process_manager.run_install_command", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "9"):
                await manager._prepare_service_dependencies("universalvc", ["uv", "run", "python", "main.py"],
                                                            WEBUI_DIR, {})
        self.assertTrue(any("10/100" in line for line in manager.get_log_history("universalvc")))

    async def test_browser_preparation_streams_progress_and_keeps_optional_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "scripts" / "ensure_playwright.py"
            script.parent.mkdir()
            script.touch()
            manager = ProcessManager(root)

            async def run(command, **kwargs):
                await kwargs["callback"](InstallSnapshot("Chromium 50%", "browser-1"))
                return 1

            with patch("process_manager.run_install_command", side_effect=run):
                await manager._prepare_playwright_chromium("nachobot", root, {})
            history = manager.get_log_history("nachobot")
            self.assertTrue(any("Chromium 50%" in line for line in history))
            self.assertIn("HTTP fallback", history[-1])

    async def test_git_bootstrap_websocket_streams_progress_before_completion(self):
        import server

        messages = []
        ws = AsyncMock()
        ws.receive_text.return_value = json.dumps({"action": "bootstrap_git"})

        async def send(text):
            messages.append(json.loads(text))

        async def ensure(callback):
            await callback(InstallSnapshot("Git 50%", "git-1"))
            return {"status": "ok", "message": "Git ready"}

        ws.send_text.side_effect = send
        with (
            patch.object(server.webui_security, "authorize_websocket", new_callable=AsyncMock, return_value=True),
            patch.object(server.DependencyInstaller, "ensure_git", side_effect=ensure),
        ):
            await server.ws_setup_install(ws)
        self.assertEqual(messages[0]["type"], "log")
        self.assertTrue(messages[0]["replace"])
        self.assertEqual(messages[-1]["type"], "bootstrap_done")
        self.assertEqual(messages[-1]["status"], "ok")

    async def test_websocket_sends_progress_keys_for_live_and_replayed_logs(self):
        import server

        manager = ProcessManager(WEBUI_DIR.parent)
        await manager._broadcast("tts_runtime_lite", InstallSnapshot("torch 10/100 MiB", "command-1"))
        messages = []
        history_sent = asyncio.Event()
        ws = AsyncMock()

        async def send(text):
            payload = json.loads(text)
            messages.append(payload)
            if payload["type"] == "history":
                history_sent.set()
            elif payload["type"] == "log":
                raise server.WebSocketDisconnect()

        ws.send_text.side_effect = send
        with (
            patch.object(server, "process_mgr", manager),
            patch.object(server.webui_security, "authorize_websocket", new_callable=AsyncMock, return_value=True),
        ):
            connection = asyncio.create_task(server.ws_logs(ws, "tts_runtime_lite"))
            await asyncio.wait_for(history_sent.wait(), 2)
            await manager._broadcast("tts_runtime_lite", InstallSnapshot("torch 50/100 MiB", "command-1"))
            await asyncio.wait_for(connection, 2)
        self.assertEqual(messages[0]["progress_keys"], ["command-1"])
        self.assertEqual(messages[1]["progress_key"], "command-1")
        self.assertEqual(messages[1]["line"], "torch 50/100 MiB")

    async def test_environment_check_keeps_other_requests_responsive(self):
        import server

        release = threading.Event()

        def check():
            release.wait(timeout=1)
            return {"status": "ok"}

        with patch.object(server.EnvironmentChecker, "check_all", side_effect=check):
            request = asyncio.create_task(server.setup_check_env())
            try:
                await asyncio.sleep(.02)
                self.assertFalse(request.done())
            finally:
                release.set()
            self.assertEqual(await request, {"status": "ok"})

    async def test_progress_is_decoded_broadcast_and_coalesced_in_history(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ProcessManager(Path(temp))
            direct, combined = [], []

            async def on_direct(line):
                direct.append(line)

            async def on_combined(line):
                combined.append(line)

            manager.subscribe("tts_runtime_lite", on_direct)
            manager.subscribe("all", on_combined)
            stream = asyncio.StreamReader()
            stream.feed_data(b"[TTS Runtime] preparing\n")
            for amount in (10, 50):
                wire = PROGRESS_PREFIX + json.dumps({"key": "command-1", "line": f"torch {amount}/100 MiB"})
                stream.feed_data((wire + "\n").encode())
            stream.feed_data(b"[TTS Runtime] done\n")
            stream.feed_eof()
            await manager._read_output("tts_runtime_lite", SimpleNamespace(stdout=stream, returncode=0))
            history = manager.get_log_history("tts_runtime_lite")
            self.assertEqual(len(history), 3)
            self.assertEqual(history[1], "torch 50/100 MiB")
            self.assertIsInstance(history[1], InstallSnapshot)
            self.assertEqual(history[1].key, "command-1")
            self.assertIsInstance(combined[1], InstallSnapshot)
            self.assertEqual(combined[1].key, "tts_runtime_lite:command-1")
            self.assertEqual(direct[1], "torch 10/100 MiB")
            self.assertFalse(any(PROGRESS_PREFIX in line for line in direct))

    async def test_real_bridge_keeps_intermediate_frames_and_exit_code(self):
        script = (
            "import sys,time; print(sys.stderr.isatty(),flush=True); "
            "print('torch 10/100 MiB',end='\\r',flush=True); time.sleep(.5); "
            "print('torch 50/100 MiB',end='\\r',flush=True); time.sleep(.5); sys.exit(7)"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(WEBUI_DIR / "install_output.py"), "--", sys.executable, "-c", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        frames = []
        try:
            while True:
                wire = await asyncio.wait_for(proc.stdout.readline(), 10)
                if not wire:
                    break
                frames.append(decode_install_progress(wire.decode()))
            self.assertEqual(await proc.wait(), 7, frames)
            self.assertTrue(all(isinstance(frame, InstallSnapshot) for frame in frames), frames)
            self.assertTrue(any("10/100" in frame for frame in frames), frames)
            self.assertTrue(any("50/100" in frame for frame in frames), frames)
            self.assertEqual(len({frame.key for frame in frames}), 1)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    def test_malformed_progress_is_left_as_text(self):
        for text in ("ordinary log", PROGRESS_PREFIX + "{}", PROGRESS_PREFIX + "[]", PROGRESS_PREFIX + "invalid"):
            self.assertEqual(decode_install_progress(text), text)


if __name__ == "__main__":
    unittest.main()
