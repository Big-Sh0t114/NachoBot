[CmdletBinding()]
param(
    [ValidateRange(10,600)][int]$TimeoutSeconds = 120,
    [switch]$CheckOnly,
    [switch]$AttachOnly,
    [long]$ExpectedRunGeneration = 0,
    [long]$ExpectedConnectionGeneration = 0,
    [int]$ExpectedSupervisorPid = 0,
    [string]$ExpectedSupervisorFileTime = ''
)
$ErrorActionPreference = 'Stop'

function Read-QQCallJson([string]$Path) {
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.Length -lt 1 -or $item.Length -gt 65536 -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'runtime_file_invalid' }
    return Get-Content -LiteralPath $Path -Encoding UTF8 -Raw | ConvertFrom-Json
}

function Assert-QQCallProcess([int]$ProcessId, [string]$FileTime) {
    try { $process = [Diagnostics.Process]::GetProcessById($ProcessId) }
    catch { throw 'service_process_unavailable' }
    if ([string]$process.StartTime.ToUniversalTime().ToFileTimeUtc() -ne $FileTime) {
        throw 'service_process_changed'
    }
}

function Test-QQCallSupervisor([string]$RuntimeDir) {
    $path = Join-Path $RuntimeDir 'manager.private.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    $manager = Read-QQCallJson $path
    try {
        Assert-QQCallProcess ([int]$manager.pid) ([string]$manager.creationTime100ns)
        return $true
    } catch {
        if ($_.Exception.Message -in @('service_process_unavailable','service_process_changed')) {
            return $false
        }
        throw
    }
}

function Start-QQCallSupervisor([string]$RuntimeDir) {
    # Reuse either a WebUI-owned or independently started manager, including
    # startup/waiting phases that do not yet have a controller or QQ target.
    if (Test-QQCallSupervisor $RuntimeDir) { return $null }
    $entry = Join-Path $PSScriptRoot 'qqcall-service.ps1'
    $item = Get-Item -LiteralPath $entry -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'service_entry_invalid' }
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # The existing entry reads canonical configuration and uses the same
    # manager/service.lock as WebUI. It does not restart Core, adapters or QQ.
    return Start-Process -FilePath $shell -ArgumentList @(
        '-NoLogo','-NoProfile','-File',('"{0}"' -f $entry),'run'
    ) -WorkingDirectory (Split-Path $PSScriptRoot) -WindowStyle Normal -PassThru
}

function Get-QQCallSnapshot([string]$RuntimeDir) {
    $statusPath = Join-Path $RuntimeDir 'status.json'
    if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
        return [pscustomobject]@{ Phase = 'not_running' }
    }
    $status = Read-QQCallJson $statusPath
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ($status.schema -ne 1 -or [long]$status.updatedAtMs -gt $now -or
        $now - [long]$status.updatedAtMs -gt 60000) { throw 'service_status_stale' }
    Assert-QQCallProcess ([int]$status.supervisorPid) ([string]$status.supervisorCreationTime100ns)
    if ($status.phase -notin @('ready','needs_attachment','waiting_identity','waiting_login')) {
        return [pscustomobject]@{ Phase = [string]$status.phase }
    }
    if ($status.ready -isnot [bool] -or
        (($status.phase -eq 'ready') -ne ($status.ready -eq $true))) { throw 'service_status_invalid' }
    $managerPath = Join-Path $RuntimeDir 'manager.private.json'
    if (-not (Test-Path -LiteralPath $managerPath -PathType Leaf)) { throw 'service_generation_changed' }
    $manager = Read-QQCallJson $managerPath
    if ($manager.pid -ne $status.supervisorPid -or
        [string]$manager.creationTime100ns -ne [string]$status.supervisorCreationTime100ns -or
        [long]$manager.runGeneration -lt 1 -or [long]$manager.controllerGeneration -lt 1) {
        throw 'service_generation_changed'
    }
    Assert-QQCallProcess ([int]$manager.pid) ([string]$manager.creationTime100ns)
    Assert-QQCallProcess ([int]$manager.controllerPid) ([string]$manager.controllerCreationTime100ns)
    Assert-QQCallProcess ([int]$manager.kernelPid) ([string]$manager.kernelCreationTime100ns)
    $generation = [long]$manager.controllerGeneration
    $directory = Join-Path $RuntimeDir ("generations\connection-{0}" -f $generation)
    $marker = Join-Path $directory 'attach-attempt.private.json'
    if ([IO.Path]::GetFullPath([string]$manager.attachAttemptPath) -ne [IO.Path]::GetFullPath($marker)) {
        throw 'attachment_path_invalid'
    }
    return [pscustomobject]@{
        Phase = [string]$status.phase
        RunGeneration = [long]$manager.runGeneration
        ConnectionGeneration = $generation
        SupervisorPid = [int]$manager.pid
        SupervisorFileTime = [string]$manager.creationTime100ns
        ScriptPath = Join-Path $directory 'admin\attach.ps1'
        MarkerPath = $marker
    }
}

function Test-QQCallAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-QQCallGeneration($Snapshot, [long]$Run, [long]$Connection, [int]$Supervisor, [string]$FileTime) {
    if ($Snapshot.Phase -notin @('ready','needs_attachment','waiting_identity','waiting_login') -or
        $Snapshot.RunGeneration -ne $Run -or $Snapshot.ConnectionGeneration -ne $Connection -or
        $Snapshot.SupervisorPid -ne $Supervisor -or $Snapshot.SupervisorFileTime -ne $FileTime) {
        throw 'service_generation_changed_retry_startup'
    }
}

try {
    $runtimeDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\.runtime\service'))
    if ($CheckOnly) {
        $snapshot = Get-QQCallSnapshot $runtimeDir
        [pscustomobject]@{
            phase = $snapshot.Phase
            needsElevation = ($snapshot.Phase -eq 'needs_attachment' -and -not (Test-QQCallAdministrator))
        } | ConvertTo-Json -Compress
        exit 0
    }
    if ($AttachOnly) {
        if (-not (Test-QQCallAdministrator)) { throw 'administrator_required' }
    } else {
        $launcher = Start-QQCallSupervisor $runtimeDir
    }
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $attempted = $false
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        if (-not $AttachOnly -and $null -ne $launcher) {
            $launcher.Refresh()
            if (-not (Test-QQCallSupervisor $runtimeDir)) {
                if ($launcher.HasExited) { throw ('service_start_failed_exit_' + $launcher.ExitCode) }
                Start-Sleep -Milliseconds 500
                continue
            }
        }
        try { $snapshot = Get-QQCallSnapshot $runtimeDir }
        catch {
            if (-not $AttachOnly -and -not $attempted -and $_.Exception.Message -in @(
                'service_status_stale','service_generation_changed','service_process_unavailable','service_process_changed')) {
                Start-Sleep -Milliseconds 500
                continue
            }
            throw
        }
        if ($AttachOnly -or $attempted) {
            Assert-QQCallGeneration $snapshot $ExpectedRunGeneration $ExpectedConnectionGeneration `
                $ExpectedSupervisorPid $ExpectedSupervisorFileTime
        }
        if ($snapshot.Phase -eq 'ready') {
            Write-Output 'QQCall ready.'
            exit 0
        }
        if ($snapshot.Phase -in @('disabled','error','quarantined','waiting_kernel_restart')) {
            throw ('service_not_startable_' + $snapshot.Phase)
        }
        if ($snapshot.Phase -eq 'needs_attachment' -and -not $attempted) {
            if (Test-Path -LiteralPath $snapshot.MarkerPath) { throw 'attachment_already_attempted' }
            $null = Read-QQCallJson (Join-Path (Split-Path $snapshot.MarkerPath) 'broker-config.private.json')
            $scriptItem = Get-Item -LiteralPath $snapshot.ScriptPath -ErrorAction Stop
            if ($scriptItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'attachment_path_invalid' }
            if (-not (Test-QQCallAdministrator)) {
                # UAC is requested once. The elevated child is pinned to the
                # observed generation and never selects a newer target itself.
                $shell = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
                $arguments = @('-NoProfile','-File',('"{0}"' -f $PSCommandPath),'-AttachOnly',
                    '-TimeoutSeconds',"$TimeoutSeconds",'-ExpectedRunGeneration',"$($snapshot.RunGeneration)",
                    '-ExpectedConnectionGeneration',"$($snapshot.ConnectionGeneration)",
                    '-ExpectedSupervisorPid',"$($snapshot.SupervisorPid)",
                    '-ExpectedSupervisorFileTime',$snapshot.SupervisorFileTime)
                try {
                    $child = Start-Process -FilePath $shell -ArgumentList $arguments -Verb RunAs `
                        -WindowStyle Normal -PassThru -Wait
                } catch { throw 'elevation_cancelled_or_unavailable' }
                $current = Get-QQCallSnapshot $runtimeDir
                Assert-QQCallGeneration $current $snapshot.RunGeneration $snapshot.ConnectionGeneration `
                    $snapshot.SupervisorPid $snapshot.SupervisorFileTime
                # ShellExecute/RunAs can leave ExitCode unavailable in Windows
                # PowerShell. A fresh, identity-checked ready report from this
                # exact generation is the service's authoritative outcome.
                if ($current.Phase -ne 'ready') {
                    if ($null -eq $child.ExitCode) { throw 'elevated_attachment_result_unavailable' }
                    if ($child.ExitCode -ne 0) { throw 'elevated_attachment_failed' }
                    throw 'attachment_not_ready'
                }
                Write-Output 'QQCall ready.'
                exit 0
            }
            $ExpectedRunGeneration = $snapshot.RunGeneration
            $ExpectedConnectionGeneration = $snapshot.ConnectionGeneration
            $ExpectedSupervisorPid = $snapshot.SupervisorPid
            $ExpectedSupervisorFileTime = $snapshot.SupervisorFileTime
            $attempted = $true
            # The generated helper retains the original PID/FILETIME/image/hash
            # gates and exclusive attempt marker. Do not inline or relax it.
            & $snapshot.ScriptPath
        }
        Start-Sleep -Milliseconds 500
    }
    throw 'startup_timeout_check_core_adapter_and_robot_login'
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 2
}
