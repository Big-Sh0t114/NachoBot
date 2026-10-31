param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "config.toml"),
    [string]$ModeOverride = "",
    [switch]$SuperviseDesktop,
    [ValidateSet("ALL", "LIVE2D", "CORE")][string]$ShowLogs = "",
    [int]$OwnerProcessId = 0
)

$ErrorActionPreference = "Stop"
function Read-ConsoleLog {
    param([string]$Path, [hashtable]$State)
    $stream = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        $created = [IO.File]::GetCreationTimeUtc($Path).Ticks
        if ($stream.Length -lt $State.Position -or $created -ne $State.Created) {
            $State.Position = 0L
            $State.Created = $created
            $State.Decoder.Reset()
        }
        $null = $stream.Seek($State.Position, [IO.SeekOrigin]::Begin)
        $bytes = [byte[]]::new(8192)
        $characters = [char[]]::new([Text.Encoding]::UTF8.GetMaxCharCount($bytes.Length))
        while (($count = $stream.Read($bytes, 0, $bytes.Length)) -gt 0) {
            # A poll boundary is not the end of a UTF-8 stream. Retain partial
            # characters until the next write, and forward ANSI/newlines intact.
            $characterCount = $State.Decoder.GetChars($bytes, 0, $count, $characters, 0, $false)
            [Console]::Write($characters, 0, $characterCount)
            $State.Position = $stream.Position
        }
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Show-Live2DLogs {
    param(
        [string]$SourceMode = "ALL",
        [int]$OwnerProcessId = 0
    )
    $ErrorActionPreference = "Continue"
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $Host.UI.RawUI.WindowTitle = "NachoBot $SourceMode Logs"
    # Forward ANSI emitted by the original logger rather than recoloring lines.
    if (-not ("NachoBotLive2D.ConsoleMode" -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace NachoBotLive2D {
    public static class ConsoleMode {
        [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int id);
        [DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr handle, out uint mode);
        [DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr handle, uint mode);
    }
}
'@
    }
    $consoleHandle = [NachoBotLive2D.ConsoleMode]::GetStdHandle(-11)
    $consoleMode = [uint32]0
    if ([NachoBotLive2D.ConsoleMode]::GetConsoleMode($consoleHandle, [ref]$consoleMode)) {
        $null = [NachoBotLive2D.ConsoleMode]::SetConsoleMode($consoleHandle, $consoleMode -bor 4)
    }

    $ownerProcess = $null
    if ($OwnerProcessId -gt 0) {
        try {
            $ownerProcess = [System.Diagnostics.Process]::GetProcessById($OwnerProcessId)
            $null = $ownerProcess.Handle
        }
        catch {
            exit 0
        }
    }

    $sources = @(
        [pscustomobject]@{
            Name = "CORE"
            Path = Join-Path $PSScriptRoot "logs\core.out.log"
        },
        [pscustomobject]@{
            Name = "CORE"
            Path = Join-Path $PSScriptRoot "logs\core.err.log"
        },
        [pscustomobject]@{
            Name = "LIVE2D"
            Path = Join-Path $PSScriptRoot "logs\live2d.out.log"
        },
        [pscustomobject]@{
            Name = "LIVE2D"
            Path = Join-Path $PSScriptRoot "logs\live2d.err.log"
        }
    )
    if ($SourceMode -ne "ALL") {
        $sources = @($sources | Where-Object { $_.Name -eq $SourceMode })
    }

    $states = @{}
    Clear-Host
    Write-Host "NachoBot Desktop Pet - Live Logs" -ForegroundColor White
    Write-Host "Closing this window does not stop the desktop pet." -ForegroundColor DarkGray
    Write-Host "Sources: $SourceMode (LIVE2D includes chat and TTS)" -ForegroundColor DarkGray
    Write-Host ""

    foreach ($source in $sources) {
        $states[$source.Path] = @{
            Position = 0L
            Created = 0L
            Decoder = [Text.Encoding]::UTF8.GetDecoder()
        }
    }

    while ($null -eq $ownerProcess -or -not $ownerProcess.HasExited) {
        foreach ($source in $sources) {
                if (-not (Test-Path -LiteralPath $source.Path -PathType Leaf)) {
                    continue
                }
                try {
                    Read-ConsoleLog -Path $source.Path -State $states[$source.Path]
                }
                catch {
                    Write-Host "Log is temporarily unavailable." -ForegroundColor DarkYellow
                }
        }
        Start-Sleep -Milliseconds 300
    }
}

if ($ShowLogs) {
    Show-Live2DLogs -SourceMode $ShowLogs -OwnerProcessId $OwnerProcessId
    exit 0
}

$adapterDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$repositoryDirectory = Split-Path -Parent $adapterDirectory
$coreDirectory = Join-Path $repositoryDirectory "NachoBot"
$localHostDirectory = Join-Path $repositoryDirectory "NachoBot-Local-Host-Adapter"
$ttsLauncher = Join-Path $repositoryDirectory "launch_local_neural_tts.ps1"
$logDirectory = Join-Path $adapterDirectory "logs"
$modeStatePath = Join-Path $logDirectory "live2d-mode.txt"
$standardOutputLog = Join-Path $logDirectory "live2d.out.log"
$standardErrorLog = Join-Path $logDirectory "live2d.err.log"
$structuredLog = Join-Path $logDirectory "live2d.log"
$desktopLogViewer = $PSCommandPath
$uvCommand = Get-Command uv -ErrorAction Stop
$script:ownedCoreProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()

if ($ModeOverride -and $ModeOverride -notin @("desktop_pet", "live")) {
    throw "ModeOverride must be desktop_pet or live"
}

function Get-ListeningProcess {
    param([int]$Port)
    Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Get-OwnedListenerProcess {
    param([object]$Listener, [System.Diagnostics.Process]$RootProcess)
    if ($null -eq $Listener -or $RootProcess.HasExited) { return $null }
    $candidate = [System.Diagnostics.Process]::GetProcessById($Listener.OwningProcess)
    $null = $candidate.Handle
    if ($candidate.StartTime -lt $RootProcess.StartTime) {
        $candidate.Dispose()
        return $null
    }
    $ancestorId = $candidate.Id
    for ($depth = 0; $depth -lt 16; $depth++) {
        if ($ancestorId -eq $RootProcess.Id -and -not $RootProcess.HasExited) { return $candidate }
        $ancestor = Get-CimInstance Win32_Process -Filter "ProcessId = $ancestorId" -ErrorAction SilentlyContinue
        if ($null -eq $ancestor -or $ancestor.ParentProcessId -eq $ancestorId) { break }
        $ancestorId = $ancestor.ParentProcessId
    }
    $candidate.Dispose()
    return $null
}

function Stop-ManagedProcessTree {
    param([System.Diagnostics.Process]$RootProcess)
    if ($RootProcess.HasExited) { return }
    # Windows venv python.exe can be a redirector with a second Python process.
    # Capture handles before stopping children, then stop their waiting parent.
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($RootProcess.Id)" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        $childProcess = $null
        try {
            $childProcess = [System.Diagnostics.Process]::GetProcessById($child.ProcessId)
            $null = $childProcess.Handle
            if ($childProcess.StartTime -ge $RootProcess.StartTime -and -not $RootProcess.HasExited) {
                Stop-ManagedProcessTree -RootProcess $childProcess
            }
        }
        catch [System.ArgumentException] { }
        finally { if ($null -ne $childProcess) { $childProcess.Dispose() } }
    }
    if (-not $RootProcess.HasExited) { $RootProcess.Kill() }
    $null = $RootProcess.WaitForExit(5000)
}

function Start-ManagedPythonService {
    param(
        [string]$Name,
        [int]$Port,
        [string]$WorkingDirectory,
        [string]$PythonPath,
        [string]$EntryPoint,
        [string]$OutputLog,
        [string]$ErrorLog,
        [int]$TimeoutSeconds = 75,
        [switch]$OwnCore
    )

    $listener = Get-ListeningProcess -Port $Port
    if ($null -ne $listener) {
        Write-Host "[OK] $Name is already listening on port $Port (PID $($listener.OwningProcess))." -ForegroundColor Green
        return
    }
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "$Name environment is missing: $PythonPath"
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $OutputLog) -Force | Out-Null
    $processOptions = @{
        FilePath = $PythonPath
        ArgumentList = @('"' + $EntryPoint + '"')
        WorkingDirectory = $WorkingDirectory
        WindowStyle = "Hidden"
        RedirectStandardOutput = $OutputLog
        RedirectStandardError = $ErrorLog
        PassThru = $true
    }
    $previousIoEncoding = $env:PYTHONIOENCODING
    $previousUnbuffered = $env:PYTHONUNBUFFERED
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUNBUFFERED = "1"
    try {
        $serviceProcess = Start-Process @processOptions
    }
    finally {
        $env:PYTHONIOENCODING = $previousIoEncoding
        $env:PYTHONUNBUFFERED = $previousUnbuffered
    }
    # Keep the actual process handle, never infer ownership from a port/PID later.
    $null = $serviceProcess.Handle
    if ($OwnCore) {
        $script:ownedCoreProcesses.Add($serviceProcess)
    }

    $checks = [Math]::Max(1, $TimeoutSeconds * 2)
    for ($check = 0; $check -lt $checks; $check++) {
        Start-Sleep -Milliseconds 500
        $listener = Get-ListeningProcess -Port $Port
        $listenerProcess = Get-OwnedListenerProcess -Listener $listener -RootProcess $serviceProcess
        if ($null -ne $listenerProcess) {
            if ($OwnCore -and $listenerProcess.Id -ne $serviceProcess.Id) {
                $script:ownedCoreProcesses.Add($listenerProcess)
            }
            else { $listenerProcess.Dispose() }
            Write-Host "[OK] $Name is ready on port $Port (PID $($listener.OwningProcess))." -ForegroundColor Green
            return
        }
        if ($serviceProcess.HasExited) {
            break
        }
    }

    if (-not $serviceProcess.HasExited) {
        Stop-ManagedProcessTree -RootProcess $serviceProcess
    }
    if (Test-Path -LiteralPath $ErrorLog) {
        Get-Content -LiteralPath $ErrorLog -Tail 30
    }
    throw "$Name failed to start"
}

function Start-DesktopChatServices {
    param([object]$LaunchConfig)

    if (-not $LaunchConfig.chat_enabled) {
        Write-Host "[INFO] Desktop chat is disabled; Core and TTS services are not required."
        return
    }

    if ($LaunchConfig.chat_transport -eq "core") {
        $coreUri = [Uri]$LaunchConfig.core_url
        if ($coreUri.Host -in @("127.0.0.1", "localhost", "::1") -and $coreUri.Port -eq 8000) {
            Start-ManagedPythonService `
                -Name "NachoBot Core" -Port 8000 `
                -WorkingDirectory $coreDirectory `
                -PythonPath (Join-Path $coreDirectory ".venv\Scripts\python.exe") `
                -EntryPoint "bot.py" `
                -OutputLog (Join-Path $logDirectory "core.out.log") `
                -ErrorLog (Join-Path $logDirectory "core.err.log") `
                -OwnCore
        }
        Write-Host "[INFO] Desktop chat connects directly to Core: $($LaunchConfig.core_url)."
        Write-Host "[VOICE] $($LaunchConfig.tts_provider)"
        return
    }

    $backendUri = [Uri]$LaunchConfig.chat_backend_url
    $usesLocalBridge = $backendUri.Port -eq 8789 -and $backendUri.Host -in @("127.0.0.1", "localhost", "::1")
    if (-not $usesLocalBridge) {
        Write-Host "[INFO] Desktop chat uses external backend $($LaunchConfig.chat_backend_url)."
        return
    }

    Start-ManagedPythonService `
        -Name "NachoBot Core" `
        -Port 8000 `
        -WorkingDirectory $coreDirectory `
        -PythonPath (Join-Path $coreDirectory ".venv\Scripts\python.exe") `
        -EntryPoint "bot.py" `
        -OutputLog (Join-Path $logDirectory "core.out.log") `
        -ErrorLog (Join-Path $logDirectory "core.err.log") `
        -OwnCore

    if ($LaunchConfig.chat_play_audio -and (Test-Path -LiteralPath $ttsLauncher -PathType Leaf)) {
        Write-Host "[START] Checking local TTS voice..."
        & $ttsLauncher
        if (-not $?) {
            throw "Local TTS launcher failed"
        }
    }

    $previousDesktopMode = $env:NACHOBOT_LOCAL_HOST_DESKTOP_MODE
    $env:NACHOBOT_LOCAL_HOST_DESKTOP_MODE = "1"
    try {
        Start-ManagedPythonService `
            -Name "desktop chat bridge" `
            -Port 8789 `
            -WorkingDirectory $localHostDirectory `
            -PythonPath (Join-Path $localHostDirectory ".venv\Scripts\python.exe") `
            -EntryPoint "main.py" `
            -OutputLog (Join-Path $localHostDirectory "runtime\desktop-pet-local-host.out.log") `
            -ErrorLog (Join-Path $localHostDirectory "runtime\desktop-pet-local-host.err.log") `
            -TimeoutSeconds 30
    }
    finally {
        $env:NACHOBOT_LOCAL_HOST_DESKTOP_MODE = $previousDesktopMode
    }

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:8789/api/status" -TimeoutSec 3
            if ($status.status -eq "ok" -and $status.core_connected) {
                Write-Host "[OK] Desktop chat is connected to NachoBot Core." -ForegroundColor Green
                return
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Desktop chat bridge started, but it did not connect to NachoBot Core"
}

function Stop-PreviousModeIfNeeded {
    param([string]$Mode, [int]$Port = 8766)

    $listener = Get-ListeningProcess -Port $Port
    if ($null -eq $listener) {
        return $false
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.CommandLine -notlike "*live2d_adapter*") {
        throw "Port $Port is used by another process (PID $($listener.OwningProcess))"
    }
    $previousMode = ""
    if (Test-Path -LiteralPath $modeStatePath -PathType Leaf) {
        $previousMode = (Get-Content -LiteralPath $modeStatePath -Raw).Trim()
    }
    if ($previousMode -eq $Mode) {
        Write-Host "[OK] Live2D $Mode mode is already running (PID $($listener.OwningProcess))." -ForegroundColor Green
        return $true
    }
    Write-Host "[SWITCH] Stopping previous Live2D mode '$previousMode'..."
    Stop-Process -Id $listener.OwningProcess -Force
    return $false
}

function Start-DesktopPet {
    param([string]$ResolvedConfig, [string]$Mode, [object]$LaunchConfig)

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $backendArguments = @(
        "-m", "live2d_adapter", "--config", ('"' + $ResolvedConfig + '"'),
        "--mode", $Mode, "--log-file", ('"' + $structuredLog + '"')
    )
    # Launch Python directly so the supervised process is the renderer itself.
    $processOptions = @{
        FilePath = (Join-Path $adapterDirectory ".venv\Scripts\python.exe")
        ArgumentList = $backendArguments
        WorkingDirectory = $adapterDirectory
        WindowStyle = "Hidden"
        RedirectStandardOutput = $standardOutputLog
        RedirectStandardError = $standardErrorLog
        PassThru = $true
    }
    $previousConsoleColor = $env:NACHOBOT_LIVE2D_CONSOLE_COLOR
    $previousIoEncoding = $env:PYTHONIOENCODING
    $previousUnbuffered = $env:PYTHONUNBUFFERED
    $env:NACHOBOT_LIVE2D_CONSOLE_COLOR = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUNBUFFERED = "1"
    try {
        $process = Start-Process @processOptions
    }
    finally {
        $env:NACHOBOT_LIVE2D_CONSOLE_COLOR = $previousConsoleColor
        $env:PYTHONIOENCODING = $previousIoEncoding
        $env:PYTHONUNBUFFERED = $previousUnbuffered
    }
    $null = $process.Handle

    $ready = $false
    try {
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            Start-Sleep -Milliseconds 250
            $listener = Get-ListeningProcess -Port $LaunchConfig.server_port
            $listenerProcess = Get-OwnedListenerProcess -Listener $listener -RootProcess $process
            if ($null -ne $listenerProcess) {
                Start-Sleep -Seconds 2
                $stableListener = Get-ListeningProcess -Port $LaunchConfig.server_port
                if ($null -ne $stableListener -and $stableListener.OwningProcess -eq $listenerProcess.Id -and -not $listenerProcess.HasExited) {
                    Set-Content -LiteralPath $modeStatePath -Value $Mode -Encoding ascii
                    Write-Host "[OK] Desktop-pet mode is ready (PID $($stableListener.OwningProcess))." -ForegroundColor Green
                    $ready = $true
                    return $listenerProcess
                }
                $listenerProcess.Dispose()
            }
            if ($process.HasExited) {
                break
            }
        }
        if (Test-Path -LiteralPath $standardErrorLog) {
            Get-Content -LiteralPath $standardErrorLog -Tail 40
        }
        throw "Live2D desktop-pet mode failed to become ready"
    }
    finally {
        if (-not $ready) { Stop-ManagedProcessTree -RootProcess $process }
        $process.Dispose()
    }
}

function Start-DesktopLogWindow {
    param(
        [ValidateSet("LIVE2D", "CORE")][string]$SourceMode = "LIVE2D",
        [int]$OwnerProcessId
    )
    if (-not (Test-Path -LiteralPath $desktopLogViewer -PathType Leaf)) {
        Write-Host "[WARN] Desktop log viewer is missing: $desktopLogViewer" -ForegroundColor Yellow
        return
    }
    $existingViewer = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -in @("powershell.exe", "pwsh.exe") -and
            $_.CommandLine -like "*$desktopLogViewer*" -and
            $_.CommandLine -like "*-ShowLogs $SourceMode*" -and
            $_.CommandLine -like "*-OwnerProcessId $OwnerProcessId*"
        } |
        Select-Object -First 1
    if ($null -ne $existingViewer) {
        Write-Host "[OK] Desktop-pet log window is already open (PID $($existingViewer.ProcessId))." -ForegroundColor Green
        return
    }
    $windowsPowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    Start-Process `
        -FilePath $windowsPowerShell `
        -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $desktopLogViewer + '"'),
            "-ShowLogs", $SourceMode, "-OwnerProcessId", $OwnerProcessId
        ) `
        -WorkingDirectory $adapterDirectory `
        -WindowStyle Normal | Out-Null
    Write-Host "[OK] $SourceMode log window is open." -ForegroundColor Green
}

function Stop-OwnedCoreProcesses {
    for ($index = $script:ownedCoreProcesses.Count - 1; $index -ge 0; $index--) {
        $coreProcess = $script:ownedCoreProcesses[$index]
        try {
            if (-not $coreProcess.HasExited) {
                Write-Host "[STOP] Desktop pet exited; stopping its Core (PID $($coreProcess.Id))."
                Stop-ManagedProcessTree -RootProcess $coreProcess
            }
        }
        catch {
            Write-Warning "Could not stop owned Core: $_"
        }
        finally {
            $coreProcess.Dispose()
        }
    }
    $script:ownedCoreProcesses.Clear()
}

function Invoke-DesktopSession {
    param([string]$ResolvedConfig, [object]$LaunchConfig)

    $petProcess = $null
    try {
        Start-DesktopChatServices -LaunchConfig $LaunchConfig
        $petProcess = Start-DesktopPet -ResolvedConfig $ResolvedConfig -Mode "desktop_pet" -LaunchConfig $LaunchConfig
        if ($LaunchConfig.show_desktop_console) {
            Start-DesktopLogWindow -SourceMode "LIVE2D" -OwnerProcessId $petProcess.Id
        }
        if ($LaunchConfig.show_core_console) {
            Start-DesktopLogWindow -SourceMode "CORE" -OwnerProcessId $petProcess.Id
        }
        # This hidden supervisor outlives the launcher and also observes crashes
        # or forced termination, for which Python's finally blocks cannot run.
        $petProcess.WaitForExit()
    }
    finally {
        Stop-OwnedCoreProcesses
        if ($null -ne $petProcess) {
            $petProcess.Dispose()
        }
    }
}

$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
Push-Location -LiteralPath $adapterDirectory
try {
    $launchArguments = @("run")
    $launchArguments += @(
        "python", "-m", "live2d_adapter", "--config", $resolvedConfig
    )
    if ($ModeOverride) {
        $launchArguments += @("--mode", $ModeOverride)
    }
    $launchArguments += "--print-launch-config"
    $launchConfigJson = & $uvCommand.Source @launchArguments
}
finally {
    Pop-Location
}
if ($LASTEXITCODE -ne 0) {
    throw "Could not load Live2D launch configuration"
}
$launchConfig = $launchConfigJson | ConvertFrom-Json
$mode = [string]$launchConfig.mode
Write-Host "[MODE] $mode"

$alreadyRunning = Stop-PreviousModeIfNeeded -Mode $mode -Port $launchConfig.server_port
if ($mode -eq "desktop_pet") {
    if ($alreadyRunning) {
        exit 0
    }
    if ($SuperviseDesktop) {
        # Serialize desktop sessions through shutdown so a new session cannot
        # reuse a Core that the previous session is just about to terminate.
        $hasher = [System.Security.Cryptography.SHA256]::Create()
        try {
            $sessionKey = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($adapterDirectory.ToLowerInvariant()))).Replace("-", "")
        }
        finally {
            $hasher.Dispose()
        }
        $sessionMutex = [System.Threading.Mutex]::new($false, "Local\NachoBotLive2D_$sessionKey")
        $ownsMutex = $false
        try {
            try {
                $ownsMutex = $sessionMutex.WaitOne(15000)
            }
            catch [System.Threading.AbandonedMutexException] {
                $ownsMutex = $true
            }
            if (-not $ownsMutex) {
                throw "The previous desktop-pet session is still running or shutting down"
            }
            if (-not (Stop-PreviousModeIfNeeded -Mode $mode -Port $launchConfig.server_port)) {
                Invoke-DesktopSession -ResolvedConfig $resolvedConfig -LaunchConfig $launchConfig
            }
        }
        finally {
            if ($ownsMutex) {
                $sessionMutex.ReleaseMutex()
            }
            $sessionMutex.Dispose()
        }
        exit 0
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $supervisorErrorLog = Join-Path $logDirectory "desktop-session.err.log"
    $supervisor = Start-Process `
        -FilePath (Get-Command powershell.exe -ErrorAction Stop).Source `
        -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $PSCommandPath + '"'),
            "-ConfigPath", ('"' + $resolvedConfig + '"'), "-ModeOverride", "desktop_pet", "-SuperviseDesktop"
        ) `
        -WorkingDirectory $adapterDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDirectory "desktop-session.out.log") `
        -RedirectStandardError $supervisorErrorLog `
        -PassThru
    for ($attempt = 0; $attempt -lt 240; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($supervisor.HasExited) {
            if (Test-Path -LiteralPath $supervisorErrorLog) {
                Get-Content -LiteralPath $supervisorErrorLog -Tail 30
            }
            throw "Desktop-pet session failed to start"
        }
        if ($null -ne (Get-ListeningProcess -Port $launchConfig.server_port)) {
            Write-Host "[OK] Desktop session started. Closing the pet will stop its newly launched Core." -ForegroundColor Green
            exit 0
        }
    }
    throw "Timed out waiting for the desktop-pet session; check logs\desktop-session.err.log"
}

if ($alreadyRunning) {
    exit 0
}

Set-Content -LiteralPath $modeStatePath -Value $mode -Encoding ascii
Write-Host "[START] Live/broadcast mode runs in this console. Press Ctrl+C to stop."
& $uvCommand.Source run python -m live2d_adapter --config $resolvedConfig --mode $mode --log-file $structuredLog
exit $LASTEXITCODE
