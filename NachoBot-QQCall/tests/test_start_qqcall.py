"""Validate startup decisions without starting services, UAC, or injection."""

import os
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/start-qqcall.ps1"
SHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@unittest.skipUnless(os.name == "nt" and SHELL.is_file(), "Windows PowerShell required")
class StartupTests(unittest.TestCase):
    def test_cold_start_without_webui_and_launcher_failure(self):
        # Real visible PowerShell child, isolated ready fixture: no HTTP, QQ,
        # models, UAC or injection. Also proves the child outlives the launcher.
        for success in (True, False):
            with self.subTest(success=success), tempfile.TemporaryDirectory(prefix="qqcall cold ") as directory:
                root = Path(directory)
                scripts = root / "scripts"
                scripts.mkdir()
                source = scripts / SCRIPT.name
                shutil.copyfile(SCRIPT, source)
                entry = scripts / "qqcall-service.ps1"
                entry.write_text(r'''
$ErrorActionPreference='Stop'
$runtime=Join-Path (Split-Path $PSScriptRoot) '.runtime\service'
$null=New-Item -ItemType Directory -Path $runtime -Force
$ft=[string][Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc()
$generation=Join-Path $runtime 'generations\connection-7'
$manager=@{pid=$PID;creationTime100ns=$ft;runGeneration=3;controllerGeneration=7;
    controllerPid=$PID;controllerCreationTime100ns=$ft;kernelPid=$PID;kernelCreationTime100ns=$ft;
    attachAttemptPath=(Join-Path $generation 'attach-attempt.private.json')}
$manager|ConvertTo-Json|Set-Content (Join-Path $runtime 'manager.private.json') -Encoding UTF8
@{schema=1;phase='ready';ready=$true;updatedAtMs=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds();
    supervisorPid=$PID;supervisorCreationTime100ns=$ft}|ConvertTo-Json|
    Set-Content (Join-Path $runtime 'status.json') -Encoding UTF8
$deadline=[DateTime]::UtcNow.AddSeconds(15)
while([DateTime]::UtcNow -lt $deadline -and -not (Test-Path (Join-Path $runtime 'stop-fixture'))) {
    Start-Sleep -Milliseconds 100
}
'stopped'|Set-Content (Join-Path $runtime 'fixture-stopped')
''' if success else "exit 17\n", encoding="ascii")
                runtime = root / ".runtime/service"
                try:
                    result = subprocess.run(
                        [str(SHELL), "-NoProfile", "-File", str(source), "-TimeoutSeconds", "10"],
                        capture_output=True, text=True, timeout=20,
                    )
                    if success:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), "QQCall ready.")
                        manager = json.loads((runtime / "manager.private.json").read_text(encoding="utf-8-sig"))
                        self.assertGreater(manager["pid"], 0)
                        self.assertFalse((runtime / "fixture-stopped").exists())
                        self.assertFalse(list(runtime.rglob("attach-attempt.private.json")))
                    else:
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn("service_start_failed_exit_17", result.stderr)
                finally:
                    if runtime.exists():
                        (runtime / "stop-fixture").touch()
                        deadline = time.monotonic() + 5
                        while success and not (runtime / "fixture-stopped").exists() and time.monotonic() < deadline:
                            time.sleep(0.1)
                        # The final marker precedes PowerShell process exit;
                        # its working directory remains open until exit on NT.
                        manager_path = runtime / "manager.private.json"
                        if success and manager_path.exists():
                            fixture_pid = int(json.loads(manager_path.read_text(encoding="utf-8-sig"))["pid"])
                            subprocess.run(
                                [str(SHELL), "-NoProfile", "-Command",
                                 f"Wait-Process -Id {fixture_pid} -Timeout 5 -ErrorAction SilentlyContinue"],
                                capture_output=True, timeout=8,
                            )

    def test_elevation_cancel_and_generation_change_without_real_uac(self):
        for mode, expected in (("success", 0), ("warming", 0), ("null_ready", 0),
                               ("failed_ready", 0), ("null_waiting", 2), ("failed_waiting", 2),
                               ("cancel", 2), ("changed", 2), ("bootstrap_pending", 0), ("ready", 0)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                scripts = root / "scripts"
                scripts.mkdir()
                harness = scripts / "startup-test.ps1"
                harness.write_text(r'''
param([string]$Source,[string]$Mode)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
foreach($node in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)){
    if($node.Name -ne 'Test-QQCallAdministrator'){. ([ScriptBlock]::Create($node.Extent.Text))}
}
function Test-QQCallAdministrator { return ($Mode -eq 'bootstrap_pending') }
$TimeoutSeconds=10;$CheckOnly=$false;$AttachOnly=$false
$ExpectedRunGeneration=0;$ExpectedConnectionGeneration=0;$ExpectedSupervisorPid=0;$ExpectedSupervisorFileTime=''
$runtime=Join-Path (Split-Path $PSScriptRoot) '.runtime\service'
$generation=Join-Path $runtime 'generations\connection-7'
$null=New-Item -ItemType Directory -Path (Join-Path $generation 'admin') -Force
'{}'|Set-Content (Join-Path $generation 'broker-config.private.json') -Encoding UTF8
"throw 'must_never_inject_in_test'"|Set-Content (Join-Path $generation 'admin\attach.ps1') -Encoding UTF8
if($Mode -eq 'bootstrap_pending') {
    @'
$pendingPath = Join-Path $runtime 'status.json'
$pending = Get-Content -Raw $pendingPath | ConvertFrom-Json
$pending.phase = 'waiting_identity'
$pending.ready = $false
$pending.updatedAtMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$pending | ConvertTo-Json | Set-Content $pendingPath -Encoding UTF8
'@ | Set-Content (Join-Path $generation 'admin\attach.ps1') -Encoding UTF8
}
$ft=[string][Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc()
$status=[ordered]@{schema=1;phase='needs_attachment';ready=$false;updatedAtMs=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds();supervisorPid=$PID;supervisorCreationTime100ns=$ft}
if($Mode -eq 'ready'){$status.phase='ready';$status.ready=$true}
$manager=[ordered]@{pid=$PID;creationTime100ns=$ft;runGeneration=3;controllerGeneration=7;attachAttemptPath=(Join-Path $generation 'attach-attempt.private.json')}
$manager.controllerPid=$PID;$manager.controllerCreationTime100ns=$ft
$manager.kernelPid=$PID;$manager.kernelCreationTime100ns=$ft
if($Mode -eq 'warming'){$manager.kernelCreationTime100ns='1'}
function Save-State {
    $status|ConvertTo-Json|Set-Content (Join-Path $runtime 'status.json') -Encoding UTF8
    $manager|ConvertTo-Json|Set-Content (Join-Path $runtime 'manager.private.json') -Encoding UTF8
}
Save-State
$script:pendingWaits=0
function Start-Sleep {
    param($Milliseconds)
    if($Mode -eq 'bootstrap_pending') {
        $script:pendingWaits++
        $status.phase=if($script:pendingWaits -eq 1){'waiting_login'}else{'ready'}
        $status.ready=($status.phase -eq 'ready')
        $status.updatedAtMs=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        Save-State
        return
    }
    if($Mode -ne 'warming'){throw 'unexpected_wait'}
    $manager.kernelCreationTime100ns=$ft
    Save-State
}
function Invoke-RestMethod { throw 'webui_must_not_be_called' }
function Start-Process {
    param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$PassThru,[switch]$Wait)
    if($Verb -ne 'RunAs' -or $WindowStyle -ne 'Normal' -or -not $Wait -or
       $ArgumentList -notcontains '-AttachOnly' -or $ArgumentList -notcontains '-ExpectedConnectionGeneration'){
        throw 'invalid_elevation_request'
    }
    $ci=[Array]::IndexOf($ArgumentList,'-ExpectedConnectionGeneration')
    if($ArgumentList[$ci+1] -ne '7'){throw 'unpinned_generation'}
    'uac-request-pinned'|Set-Content (Join-Path $runtime 'mock-uac.txt')
    if($Mode -eq 'cancel'){throw 'cancelled_fixture'}
    if($Mode -notin @('null_waiting','failed_waiting')){
        $status.phase='ready';$status.ready=$true
    }
    if($Mode -eq 'changed'){
        $manager.controllerGeneration=8
        $manager.attachAttemptPath=Join-Path $runtime 'generations\connection-8\attach-attempt.private.json'
    }
    Save-State
    if($Mode -like 'null_*'){return [pscustomobject]@{ExitCode=$null}}
    if($Mode -like 'failed_*'){return [pscustomobject]@{ExitCode=2}}
    return [pscustomobject]@{ExitCode=0}
}
$main=@($ast.EndBlock.Statements|Where-Object {$_ -is [Management.Automation.Language.TryStatementAst]})
if($main.Count -ne 1){throw 'main_not_unique'}
$startupRoot=$PSScriptRoot;$startupCommand=$PSCommandPath
$body=$main[0].Extent.Text.Replace('$PSScriptRoot','$startupRoot').Replace('$PSCommandPath','$startupCommand')
. ([ScriptBlock]::Create($body))
''', encoding="ascii")
                result = subprocess.run(
                    [str(SHELL), "-NoProfile", "-File", str(harness), "-Source", str(SCRIPT), "-Mode", mode],
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                runtime = root / ".runtime/service"
                self.assertEqual((runtime / "mock-uac.txt").is_file(), mode not in ("bootstrap_pending", "ready"))
                self.assertFalse((runtime / "generations/connection-7/attach-attempt.private.json").exists())
                if mode in ("success", "warming", "null_ready", "failed_ready", "bootstrap_pending", "ready"):
                    self.assertEqual(result.stdout.strip(), "QQCall ready.")
                elif mode == "cancel":
                    self.assertIn("elevation_cancelled_or_unavailable", result.stderr)
                elif mode == "null_waiting":
                    self.assertIn("elevated_attachment_result_unavailable", result.stderr)
                elif mode == "failed_waiting":
                    self.assertIn("elevated_attachment_failed", result.stderr)
                else:
                    self.assertIn("service_generation_changed_retry_startup", result.stderr)

    def test_independent_launch_and_process_identity_reuse(self):
        with tempfile.TemporaryDirectory(prefix="qqcall path ") as directory:
            root = Path(directory)
            (root / "qqcall-service.ps1").write_text("throw 'never_execute_fixture'", encoding="ascii")
            harness = root / "launch-test.ps1"
            harness.write_text(r'''
param([string]$Source)
$ErrorActionPreference='Stop'
$fixtureRoot=$PSScriptRoot
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
foreach($node in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)){
    . ([ScriptBlock]::Create($node.Extent.Text.Replace('$PSScriptRoot','$fixtureRoot')))
}
$runtime=Join-Path $PSScriptRoot 'runtime'
$null=New-Item -ItemType Directory -Path $runtime
$script:launches=0
function Invoke-RestMethod {throw 'webui_must_not_be_called'}
function Start-Process {
    param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru)
    if($WindowStyle -ne 'Normal' -or -not $PassThru -or
       $ArgumentList[-1] -ne 'run' -or
       $ArgumentList[3] -ne ('"'+(Join-Path $PSScriptRoot 'qqcall-service.ps1')+'"')) {
        throw 'invalid_independent_launch'
    }
    $script:launches++
    return [pscustomobject]@{Id=123}
}
if((Start-QQCallSupervisor $runtime).Id -ne 123){throw 'cold_start_failed'}
$manager=@{pid=$PID;creationTime100ns=[string][Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc()}
$manager|ConvertTo-Json|Set-Content (Join-Path $runtime 'manager.private.json') -Encoding UTF8
if($null -ne (Start-QQCallSupervisor $runtime)){throw 'live_manager_not_reused'}
$manager.creationTime100ns='1'
$manager|ConvertTo-Json|Set-Content (Join-Path $runtime 'manager.private.json') -Encoding UTF8
if((Start-QQCallSupervisor $runtime).Id -ne 123){throw 'reused_pid_not_rejected'}
$manager.pid=2147483647
$manager|ConvertTo-Json|Set-Content (Join-Path $runtime 'manager.private.json') -Encoding UTF8
if((Start-QQCallSupervisor $runtime).Id -ne 123){throw 'dead_manager_not_replaced'}
if($script:launches -ne 3){throw 'duplicate_launch'}
'independent-launch-ok'
''', encoding="ascii")
            result = subprocess.run(
                [str(SHELL), "-NoProfile", "-File", str(harness), "-Source", str(SCRIPT)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "independent-launch-ok")

    def test_parser_and_fresh_generation_gates_without_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = root / "test.ps1"
            harness.write_text(r'''
param([string]$Source,[string]$Fixture)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'syntax_failed'}
# Import function definitions only; main startup/UAC/injection is never run.
foreach($node in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)){
    . ([ScriptBlock]::Create($node.Extent.Text))
}
$null=New-Item -ItemType Directory -Path $Fixture -Force
$process=[Diagnostics.Process]::GetCurrentProcess()
$ft=[string]$process.StartTime.ToUniversalTime().ToFileTimeUtc()
function Write-Fixture {
    $script:status=[ordered]@{schema=1;phase='needs_attachment';ready=$false;updatedAtMs=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds();supervisorPid=$PID;supervisorCreationTime100ns=$ft}
    $script:manager=[ordered]@{pid=$PID;creationTime100ns=$ft;runGeneration=3;controllerGeneration=7;attachAttemptPath=(Join-Path $Fixture 'generations\connection-7\attach-attempt.private.json')}
    $script:manager.controllerPid=$PID;$script:manager.controllerCreationTime100ns=$ft
    $script:manager.kernelPid=$PID;$script:manager.kernelCreationTime100ns=$ft
}
function Save-Fixture {
    $status|ConvertTo-Json|Set-Content (Join-Path $Fixture 'status.json') -Encoding UTF8
    $manager|ConvertTo-Json|Set-Content (Join-Path $Fixture 'manager.private.json') -Encoding UTF8
}
function Reject([string]$Reason) {
    $caught=$false
    try{$null=Get-QQCallSnapshot $Fixture}catch{$caught=$_.Exception.Message -eq $Reason}
    if(-not $caught){throw ('gate_failed_'+$Reason)}
}
Write-Fixture;Save-Fixture
$s=Get-QQCallSnapshot $Fixture
if($s.Phase -ne 'needs_attachment' -or $s.ConnectionGeneration -ne 7){throw 'fresh_failed'}
Assert-QQCallGeneration $s 3 7 $PID $ft
$caught=$false
try{Assert-QQCallGeneration $s 3 8 $PID $ft}catch{$caught=$true}
if(-not $caught){throw 'generation_pin_failed'}
Write-Fixture;$status.updatedAtMs-=61000;Save-Fixture;Reject 'service_status_stale'
Write-Fixture;$status.updatedAtMs+=100000;Save-Fixture;Reject 'service_status_stale'
Write-Fixture;$status.ready='true';Save-Fixture;Reject 'service_status_invalid'
Write-Fixture;$status.phase='ready';Save-Fixture;Reject 'service_status_invalid'
Write-Fixture;$manager.creationTime100ns='1';Save-Fixture;Reject 'service_generation_changed'
Write-Fixture;$manager.controllerCreationTime100ns='1';Save-Fixture;Reject 'service_process_changed'
Write-Fixture;$manager.kernelPid=2147483647;Save-Fixture;Reject 'service_process_unavailable'
Write-Fixture;$manager.attachAttemptPath=Join-Path $Fixture 'elsewhere\attach.ps1';Save-Fixture;Reject 'attachment_path_invalid'
Write-Fixture;$status.phase='ready';$status.ready=$true;Save-Fixture
if((Get-QQCallSnapshot $Fixture).Phase -ne 'ready'){throw 'ready_failed'}
Write-Fixture;$status.phase='waiting_login';Save-Fixture
if((Get-QQCallSnapshot $Fixture).Phase -ne 'waiting_login'){throw 'waiting_failed'}
Assert-QQCallGeneration (Get-QQCallSnapshot $Fixture) 3 7 $PID $ft
Write-Fixture;$status.phase='waiting_identity';Save-Fixture
Assert-QQCallGeneration (Get-QQCallSnapshot $Fixture) 3 7 $PID $ft
Write-Fixture;$status.phase='waiting_identity';$manager.kernelCreationTime100ns='1';Save-Fixture;Reject 'service_process_changed'
'startup-gates-ok'
''', encoding="ascii")
            result = subprocess.run(
                [str(SHELL), "-NoProfile", "-File", str(harness), "-Source", str(SCRIPT),
                 "-Fixture", str(root / "runtime")],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "startup-gates-ok")


if __name__ == "__main__":
    unittest.main()
