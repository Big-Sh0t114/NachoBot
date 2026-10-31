from __future__ import annotations

import io
import json
import subprocess
import queue
import threading
import time
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loguru import logger

from live2d_adapter.__main__ import _configure_logging
from live2d_adapter.launcher import LogTailer, main


class ConsoleLoggingTests(unittest.TestCase):
    def test_log_viewer_rebinds_console_before_any_output(self) -> None:
        calls = []
        with (
            patch(
                "live2d_adapter.launcher._bind_viewer_console",
                side_effect=lambda: calls.append("bind"),
            ),
            patch(
                "live2d_adapter.launcher.show_logs",
                side_effect=lambda *_: calls.append("show") or 0,
            ),
        ):
            self.assertEqual(main(["--_view-logs", "CORE"]), 0)
        self.assertEqual(calls, ["bind", "show"])

    @unittest.skipUnless(os.name == "nt", "Windows console buffer integration")
    def test_both_new_consoles_print_despite_inherited_file_handles(self) -> None:
        observer = r"""
import ctypes,json,sys,time
from ctypes import wintypes as w
from pathlib import Path
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.FreeConsole()
assert k.AttachConsole(int(sys.argv[1]))
k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.c_void_p]
k.CreateFileW.restype=w.HANDLE
handle=k.CreateFileW('CONOUT$',0xC0000000,3,None,3,0,None)
class Coord(ctypes.Structure):
    _fields_=[('x',w.SHORT),('y',w.SHORT)]
class Rect(ctypes.Structure):
    _fields_=[('l',w.SHORT),('t',w.SHORT),('r',w.SHORT),('b',w.SHORT)]
class Info(ctypes.Structure):
    _fields_=[('size',Coord),('cursor',Coord),('attributes',w.WORD),('rect',Rect),('max',Coord)]
k.GetConsoleScreenBufferInfo.argtypes=[w.HANDLE,ctypes.POINTER(Info)]
k.ReadConsoleOutputCharacterW.argtypes=[w.HANDLE,w.LPWSTR,w.DWORD,Coord,ctypes.POINTER(w.DWORD)]
k.CloseHandle.argtypes=[w.HANDLE]
try:
    deadline=time.monotonic()+4
    while True:
        info=Info(); assert k.GetConsoleScreenBufferInfo(handle,ctypes.byref(info))
        count=min(32768,info.size.x*info.size.y)
        buf=ctypes.create_unicode_buffer(count+1); read=w.DWORD()
        assert k.ReadConsoleOutputCharacterW(handle,buf,count,Coord(0,0),ctypes.byref(read))
        text=buf[:read.value]
        if (sys.argv[3] in text and sys.argv[4] in text) or time.monotonic()>deadline:
            Path(sys.argv[2]).write_text(json.dumps(text),encoding='utf-8'); break
        time.sleep(.05)
finally:
    k.CloseHandle(handle); k.FreeConsole()
"""
        python = getattr(sys, "_base_executable", sys.executable)
        adapter = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "logs").mkdir()
            for source in ("CORE", "LIVE2D"):
                (root / "logs" / f"{source.lower()}.out.log").write_text(
                    f"{source}-initial-日志\n", encoding="utf-8"
                )
                (root / "logs" / f"{source.lower()}.err.log").write_text(
                    f"{source}-stderr-日志\n", encoding="utf-8"
                )
            for source in ("CORE", "LIVE2D"):
                with (
                    self.subTest(source=source),
                    (root / "redirected.log").open("wb") as redirected,
                ):
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
                    startup.wShowWindow = 0
                    viewer = subprocess.Popen(
                        [
                            python,
                            "-u",
                            "-m",
                            "live2d_adapter.launcher",
                            "--_view-logs",
                            source,
                            "--_adapter-dir",
                            str(root),
                        ],
                        cwd=adapter,
                        startupinfo=startup,
                        creationflags=subprocess.CREATE_NEW_CONSOLE,
                        stdin=subprocess.DEVNULL,
                        stdout=redirected,
                        stderr=redirected,
                    )
                    try:
                        for suffix in ("initial-日志", "appended-日志"):
                            if suffix.startswith("appended"):
                                with (root / "logs" / f"{source.lower()}.out.log").open(
                                    "a", encoding="utf-8"
                                ) as log:
                                    log.write(f"{source}-{suffix}\n")
                            result = root / "buffer.json"
                            subprocess.run(
                                [
                                    python,
                                    "-c",
                                    observer,
                                    str(viewer.pid),
                                    str(result),
                                    f"{source}-{suffix}",
                                    f"{source}-stderr-日志",
                                ],
                                check=True,
                                timeout=7,
                                creationflags=subprocess.CREATE_NO_WINDOW,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                            )
                            text = json.loads(result.read_text(encoding="utf-8"))
                            self.assertIn(f"{source}-{suffix}", text)
                            self.assertIn(f"{source}-stderr-日志", text)
                            self.assertIn("实时日志", text)
                            other = "LIVE2D" if source == "CORE" else "CORE"
                            self.assertNotIn(f"{other}-initial-日志", text)
                    finally:
                        viewer.terminate()
                        viewer.wait(timeout=5)

    def test_cursor_snapshot_keeps_early_startup_lines_but_skips_old_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "core.out.log"
            path.write_text("old session\n", encoding="utf-8")
            parent = LogTailer(path)
            parent.seek_end()
            cursor = parent.cursor()
            with path.open("a", encoding="utf-8", newline="") as stream:
                stream.write("viewer has not started yet\n")
            child = LogTailer(path, cursor)
            self.assertEqual(child.read_available(), "viewer has not started yet\n")
            self.assertEqual(child.read_available(), "")

    def test_live_lines_arrive_before_the_next_record_and_old_session_viewer_exits(self) -> None:
        adapter = Path(__file__).resolve().parents[1]
        python = getattr(sys, "_base_executable", sys.executable)
        code = (
            "import json,sys;from pathlib import Path;"
            "from live2d_adapter.launcher import show_logs;"
            "show_logs(Path(sys.argv[1]),'CORE',session_id='timed',cursors=json.loads(sys.argv[2]))"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            source = logs / "core.out.log"
            source.write_text("previous-session-history\n", encoding="utf-8")
            cursor = LogTailer(source)
            cursor.seek_end()
            state = logs / "launcher-state.json"
            state.write_text(
                json.dumps({"status": "starting", "request_id": "timed"}), encoding="utf-8"
            )
            process = subprocess.Popen(
                [python, "-u", "-c", code, str(root), json.dumps([cursor.cursor()])],
                cwd=adapter,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            received = queue.Queue()

            def read_lines():
                for line in process.stdout:
                    received.put((time.monotonic(), line))

            reader = threading.Thread(target=read_lines, daemon=True)
            reader.start()
            try:
                deadline = time.monotonic() + 5
                while True:
                    _, line = received.get(timeout=max(0.01, deadline - time.monotonic()))
                    if "等待日志" in line:
                        break
                for index in range(3):
                    marker = f"实时日志-{index}"
                    with source.open("a", encoding="utf-8") as stream:
                        stream.write(marker + "\n")
                        stream.flush()
                    deadline = time.monotonic() + 1
                    while True:
                        _, line = received.get(timeout=max(0.01, deadline - time.monotonic()))
                        self.assertNotIn("previous-session-history", line)
                        if marker in line:
                            break
                    # The next record is deliberately not produced until this one arrives.
                state.write_text(
                    json.dumps({"status": "starting", "request_id": "next-session"}),
                    encoding="utf-8",
                )
                self.assertEqual(process.wait(timeout=3), 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                reader.join(timeout=1)
                process.stdout.close()
                process.stderr.close()

    def test_loguru_keeps_console_colors_and_plain_file_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live2d.log"
            console = io.StringIO()
            try:
                with patch.dict(os.environ, {"NACHOBOT_LIVE2D_CONSOLE_COLOR": "1"}):
                    with patch("sys.stderr", console):
                        _configure_logging("INFO", path)
                        logger.info("info color")
                        logger.warning("warning color")
                        logger.complete()
                        logger.remove()
                self.assertIn("\x1b[", console.getvalue())
                self.assertIn("info color", console.getvalue())
                self.assertIn("warning color", console.getvalue())
                self.assertNotIn("\x1b[", path.read_text(encoding="utf-8"))
            finally:
                logger.remove()
                logger.add(sys.stderr)

    def test_reader_preserves_all_lines_split_utf8_ansi_and_file_reset(self) -> None:
        prefix = "".join(f"line {index}\n" for index in range(20)) + "\x1b[32m"
        message = "中文完整日志🙂\x1b[0m\n"
        initial = prefix.encode("utf-8") + message.encode("utf-8")[:1]
        remaining = message.encode("utf-8")[1:]
        reset = "reset log\n"
        with tempfile.TemporaryDirectory(prefix="console stream ") as directory:
            path = Path(directory) / "console.log"
            path.write_bytes(initial)
            reader = LogTailer(path)
            self.assertEqual(reader.read_available(), prefix)
            self.assertEqual(reader.read_available(), "")
            with path.open("ab") as stream:
                stream.write(remaining)
            self.assertEqual(reader.read_available(), message)
            self.assertEqual(reader.read_available(), "")
            path.write_bytes(reset.encode("utf-8"))
            self.assertEqual(reader.read_available(), reset)
