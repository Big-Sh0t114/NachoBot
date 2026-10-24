"""Exercise the Windows launcher with isolated real child processes."""

from __future__ import annotations

import base64
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

from live2d_adapter.config import ConfigError, load_config


ROOT = Path(__file__).resolve().parents[1]


def ps_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class ConsoleConfigTests(unittest.TestCase):
    def test_console_defaults_and_independent_switches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            for extra, expected in (
                ("", (False, False)),
                ("show_desktop_console = true", (True, False)),
                ("show_core_console = true", (False, True)),
                ("show_desktop_console = true\nshow_core_console = true", (True, True)),
            ):
                config_path.write_text(
                    '[renderer]\nmodel_path = "avatar.model3.json"\n[desktop_pet]\n' + extra,
                    encoding="utf-8",
                )
                config = load_config(config_path)
                self.assertEqual(
                    (config.desktop_pet.show_desktop_console, config.desktop_pet.show_core_console),
                    expected,
                )
            config_path.write_text(
                '[renderer]\nmodel_path = "avatar.model3.json"\n'
                '[desktop_pet]\nshow_desktop_console = "false"', encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(config_path)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows launcher")
class LauncherLifecycleTests(unittest.TestCase):
    def exercise(self, scenario: str, reuse: bool = False) -> dict:
        with tempfile.TemporaryDirectory(prefix="live2d launcher ") as directory:
            fixture = Path(directory) / "service fixture.py"
            fixture.write_text(
                "import socket,sys,time\n"
                "port=int(sys.argv[1])\n"
                "if port:\n"
                " s=socket.socket();s.bind(('127.0.0.1',port));s.listen()\n"
                "time.sleep(float(sys.argv[2]))\n", encoding="utf-8",
            )
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            existing = None
            try:
                if reuse:
                    existing = subprocess.Popen(
                        [sys.executable, str(fixture), str(port), "30"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    for _ in range(100):
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                                break
                        except OSError:
                            time.sleep(0.05)
                    else:
                        self.fail("Fixture did not start")
                script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(LAUNCHER, [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($definition in $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false)) {
    Invoke-Expression $definition.Extent.Text
}
$script:ownedCoreProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$script:views = @()
$script:ownedId = 0
$script:scenario = SCENARIO
$script:fixture = FIXTURE
$script:python = PYTHON
$script:port = PORT
$script:testRoot = TESTROOT
function Start-DesktopLogWindow {
    param($SourceMode, $OwnerProcessId)
    if ($OwnerProcessId -le 0) { throw 'Viewer has no desktop owner' }
    $script:views += $SourceMode
}
function Start-DesktopChatServices {
    param($LaunchConfig)
    $entry = $script:fixture + '" ' + $script:port + ' 30 "'
    if ($script:scenario -eq 'core_failure') { $entry = $script:fixture + '" 0 0 "' }
    Start-ManagedPythonService -Name 'fixture Core' -Port $script:port -WorkingDirectory $script:testRoot -PythonPath $script:python -EntryPoint $entry -OutputLog (Join-Path $script:testRoot 'core.out') -ErrorLog (Join-Path $script:testRoot 'core.err') -TimeoutSeconds 1 -OwnCore
    if ($script:ownedCoreProcesses.Count) { $script:ownedId = $script:ownedCoreProcesses[0].Id }
}
function Start-DesktopPet {
    param($ResolvedConfig, $Mode, $LaunchConfig)
    if ($script:scenario -eq 'pet_failure') { throw 'expected startup failure' }
    $pet = Start-Process -FilePath $script:python -ArgumentList @(('"' + $script:fixture + '"'), '0', '0.5') -WindowStyle Hidden -PassThru
    $null = $pet.Handle
    if ($script:scenario -eq 'forced_exit') { $pet.Kill() }
    return $pet
}
$failed = $false
$failureText = ''
try {
    Invoke-DesktopSession -ResolvedConfig 'unused' -LaunchConfig ([pscustomobject]@{show_desktop_console=$true;show_core_console=$true})
}
catch { $failed = $true; $failureText = ($_ | Out-String) }
finally { Stop-OwnedCoreProcesses }
$listener = Get-ListeningProcess -Port $script:port
[pscustomobject]@{failed=$failed;error=$failureText;listening=($null -ne $listener);owned_count=$script:ownedCoreProcesses.Count;views=@($script:views)} | ConvertTo-Json -Compress
"""
                for key, value in {
                    "LAUNCHER": ps_quote(ROOT / "launch_live2d.ps1"),
                    "SCENARIO": ps_quote(scenario), "FIXTURE": ps_quote(fixture),
                    "PYTHON": ps_quote(sys.executable), "PORT": str(port),
                    "TESTROOT": ps_quote(directory),
                }.items():
                    script = script.replace(key, value)
                encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                data = json.loads(result.stdout.strip().splitlines()[-1])
                data["error"] += result.stdout
                if existing:
                    self.assertIsNone(existing.poll(), "Reused Core was terminated")
                return data
            finally:
                if existing:
                    existing.terminate()
                    existing.wait(timeout=5)

    def test_owned_core_exits_with_pet_and_reuses_log_viewer(self) -> None:
        result = self.exercise("normal")
        self.assertFalse(result["failed"], result.get("error"))
        self.assertFalse(result["listening"])
        self.assertEqual(result["owned_count"], 0)
        self.assertEqual(result["views"], ["LIVE2D", "CORE"])

    def test_forced_pet_exit_stops_owned_core(self) -> None:
        result = self.exercise("forced_exit")
        self.assertFalse(result["failed"], result.get("error"))
        self.assertFalse(result["listening"])

    def test_pet_start_failure_stops_owned_core(self) -> None:
        result = self.exercise("pet_failure")
        self.assertTrue(result["failed"])
        self.assertFalse(result["listening"])

    def test_core_start_failure_is_cleaned_up(self) -> None:
        result = self.exercise("core_failure")
        self.assertTrue(result["failed"])
        self.assertFalse(result["listening"])
        self.assertEqual(result["owned_count"], 0)

    def test_existing_core_survives_pet_exit(self) -> None:
        result = self.exercise("normal", reuse=True)
        self.assertFalse(result["failed"])
        self.assertTrue(result["listening"])
        self.assertEqual(result["owned_count"], 0)
