param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "config.toml")
)

$ErrorActionPreference = "Stop"
$adapterDirectory = $PSScriptRoot
$repositoryDirectory = Split-Path -Parent $adapterDirectory
$coreDirectory = Join-Path $repositoryDirectory "NachoBot"
$localHostDirectory = Join-Path $repositoryDirectory "NachoBot-Local-Host-Adapter"
$ttsLauncher = Join-Path $repositoryDirectory "launch_local_neural_tts.ps1"
$logDirectory = Join-Path $adapterDirectory "logs"
$modeStatePath = Join-Path $logDirectory "live2d-mode.txt"
$standardOutputLog = Join-Path $logDirectory "live2d.out.log"
$standardErrorLog = Join-Path $logDirectory "live2d.err.log"
$uvCommand = Get-Command uv -ErrorAction Stop

function Get-ListeningProcess {
    param([int]$Port)
    Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
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
        [int]$TimeoutSeconds = 75
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
    $serviceProcess = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @($EntryPoint) `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutputLog `
        -RedirectStandardError $ErrorLog `
        -PassThru

    $checks = [Math]::Max(1, $TimeoutSeconds * 2)
    for ($check = 0; $check -lt $checks; $check++) {
        Start-Sleep -Milliseconds 500
        $listener = Get-ListeningProcess -Port $Port
        if ($null -ne $listener) {
            Write-Host "[OK] $Name is ready on port $Port (PID $($listener.OwningProcess))." -ForegroundColor Green
            return
        }
        if ($serviceProcess.HasExited) {
            break
        }
    }

    if (-not $serviceProcess.HasExited) {
        Stop-Process -Id $serviceProcess.Id -Force
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
        -OutputLog (Join-Path $coreDirectory "logs\desktop-pet-core.out.log") `
        -ErrorLog (Join-Path $coreDirectory "logs\desktop-pet-core.err.log")

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
    param([string]$Mode)

    $listener = Get-ListeningProcess -Port 8766
    if ($null -eq $listener) {
        return $false
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.CommandLine -notlike "*live2d_adapter*") {
        throw "Port 8766 is used by another process (PID $($listener.OwningProcess))"
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
    param([string]$ResolvedConfig, [string]$Mode)

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $process = Start-Process `
        -FilePath $uvCommand.Source `
        -ArgumentList @("run", "python", "-m", "live2d_adapter", "--config", $ResolvedConfig) `
        -WorkingDirectory $adapterDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $standardOutputLog `
        -RedirectStandardError $standardErrorLog `
        -PassThru

    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        Start-Sleep -Milliseconds 250
        $listener = Get-ListeningProcess -Port 8766
        if ($null -ne $listener) {
            Start-Sleep -Seconds 2
            $stableListener = Get-ListeningProcess -Port 8766
            if ($null -ne $stableListener -and -not $process.HasExited) {
                Set-Content -LiteralPath $modeStatePath -Value $Mode -Encoding ascii
                Write-Host "[OK] Desktop-pet mode is ready (PID $($stableListener.OwningProcess))." -ForegroundColor Green
                return
            }
        }
        if ($process.HasExited) {
            break
        }
    }
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
    if (Test-Path -LiteralPath $standardErrorLog) {
        Get-Content -LiteralPath $standardErrorLog -Tail 40
    }
    throw "Live2D desktop-pet mode failed to become ready"
}

$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
Push-Location -LiteralPath $adapterDirectory
try {
    $launchConfigJson = & $uvCommand.Source run python -m live2d_adapter --config $resolvedConfig --print-launch-config
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

$alreadyRunning = Stop-PreviousModeIfNeeded -Mode $mode
if ($mode -eq "desktop_pet") {
    Start-DesktopChatServices -LaunchConfig $launchConfig
    if ($alreadyRunning) {
        exit 0
    }
    Start-DesktopPet -ResolvedConfig $resolvedConfig -Mode $mode
    exit 0
}

if ($alreadyRunning) {
    exit 0
}

Set-Content -LiteralPath $modeStatePath -Value $mode -Encoding ascii
Write-Host "[START] Live/broadcast mode runs in this console. Press Ctrl+C to stop."
& $uvCommand.Source run python -m live2d_adapter --config $resolvedConfig
exit $LASTEXITCODE
