from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loguru import logger

from live2d_adapter.__main__ import _configure_logging


class ConsoleLoggingTests(unittest.TestCase):
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

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows reader")
    def test_reader_preserves_all_lines_split_utf8_ansi_and_file_reset(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "launch_live2d.ps1"
        prefix = "".join(f"line {index}\n" for index in range(20)) + "\x1b[32m"
        message = "中文完整日志🙂\x1b[0m\n"
        initial = prefix.encode("utf-8") + message.encode("utf-8")[:1]
        remaining = message.encode("utf-8")[1:]
        reset = "reset log\n"
        with tempfile.TemporaryDirectory(prefix="console stream ") as directory:
            path = Path(directory) / "console.log"
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            script = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(LAUNCHER, [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$definition = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Read-ConsoleLog'}, $false)
Invoke-Expression $definition.Extent.Text
$path = LOGPATH
$state = @{Position=0L;Created=0L;Decoder=[Text.Encoding]::UTF8.GetDecoder()}
[IO.File]::WriteAllBytes($path, [Convert]::FromBase64String(INITIAL))
Read-ConsoleLog -Path $path -State $state
$stream = [IO.File]::Open($path, [IO.FileMode]::Append, [IO.FileAccess]::Write)
try {
    $bytes = [Convert]::FromBase64String(REMAINING)
    $stream.Write($bytes, 0, $bytes.Length)
} finally { $stream.Dispose() }
Read-ConsoleLog -Path $path -State $state
Read-ConsoleLog -Path $path -State $state
[IO.File]::WriteAllBytes($path, [Convert]::FromBase64String(RESET))
Read-ConsoleLog -Path $path -State $state
"""
            for key, value in {
                "LAUNCHER": quote(launcher), "LOGPATH": quote(path),
                "INITIAL": quote(base64.b64encode(initial).decode()),
                "REMAINING": quote(base64.b64encode(remaining).decode()),
                "RESET": quote(base64.b64encode(reset.encode()).decode()),
            }.items():
                script = script.replace(key, value)
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-EncodedCommand",
                 base64.b64encode(script.encode("utf-16le")).decode()],
                capture_output=True, encoding="utf-8", timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, prefix + message + reset)
