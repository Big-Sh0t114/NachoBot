# Starts only the local VoxCPM2 neural-TTS stack.  It never starts or restarts
# NachoBot Core, so it is safe to use while the live-control page is open.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$voxRoot = 'E:\App\VoxCPM'
$voxPython = Join-Path $voxRoot '.venv\Scripts\python.exe'
$voxServer = Join-Path $root 'NachoBot-Multimodal-Adapter\src\tts\backends\Vox\vox_api_server.py'
$modelDir = Join-Path $voxRoot 'models\openbmb__VoxCPM2'
$adapterDir = Join-Path $root 'NachoBot-Multimodal-Adapter'
$adapterPython = Join-Path $adapterDir '.venv\Scripts\python.exe'
$loraWeights = Join-Path $root 'NachoBot-Live2D-Adapter\resources\ncnk3'
$logDir = Join-Path $root 'NachoBot-Local-Host-Adapter\runtime\logs'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-LocalPort([int] $Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Wait-VoxModelReady([int] $TimeoutSeconds = 300) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:9880/health' -TimeoutSec 3
            if ($health.status -eq 'ok' -and $health.model_loaded -eq $true) {
                return $true
            }
        }
        catch {
            # The process opens the port only after loading the 4.5 GB model.
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Wait-TTSBridgeReady([int] $TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8070/api/health' -TimeoutSec 3
            if ($health.status -eq 'ok' -and @($health.tts_backends).Count -gt 0) {
                return $true
            }
        }
        catch {
            # Keep polling while the lightweight bridge initializes.
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $false
}

$voxReady = $false
if (-not (Test-Path -LiteralPath (Join-Path $modelDir 'model.safetensors'))) {
    Write-Warning "VoxCPM2 model is incomplete: $modelDir. Microsoft SAPI will be used."
}
else {
    try {
        if (-not (Test-LocalPort 9880)) {
            $voxArguments = @(
                $voxServer,
                '--host', '127.0.0.1',
                '--port', '9880',
                '--model-dir', $modelDir,
                '--no-denoiser'
            )
            if (
                (Test-Path -LiteralPath (Join-Path $loraWeights 'lora_config.json') -PathType Leaf) -and
                (Test-Path -LiteralPath (Join-Path $loraWeights 'lora_weights.safetensors') -PathType Leaf)
            ) {
                $voxArguments += @('--lora-weights', $loraWeights)
            }
            else {
                Write-Warning "Fixed-voice LoRA is incomplete: $loraWeights. Starting the base VoxCPM2 voice."
            }

            $previousVoxDir = $env:VOXCPM_DIR
            $env:VOXCPM_DIR = $voxRoot
            try {
                Start-Process -FilePath $voxPython `
                    -ArgumentList $voxArguments `
                    -WorkingDirectory $voxRoot -WindowStyle Hidden `
                    -RedirectStandardOutput (Join-Path $logDir 'vox-server.out.log') `
                    -RedirectStandardError (Join-Path $logDir 'vox-server.err.log')
            }
            finally {
                $env:VOXCPM_DIR = $previousVoxDir
            }
            Write-Host "Started local VoxCPM2 with fixed-voice LoRA: $loraWeights"
        }

        Write-Host 'Waiting for VoxCPM2 before enabling the Microsoft SAPI fallback.'
        $voxReady = Wait-VoxModelReady
        if ($voxReady) {
            Write-Host 'VoxCPM2 model is ready and remains the preferred voice.'
        }
        else {
            Write-Warning 'VoxCPM2 did not become ready within 300 seconds. Microsoft SAPI will be used.'
        }
    }
    catch {
        Write-Warning "VoxCPM2 startup failed: $($_.Exception.Message). Microsoft SAPI will be used."
    }
}

try {
    if (-not (Test-LocalPort 8070)) {
        Start-Process -FilePath $adapterPython -ArgumentList @('main.py') `
            -WorkingDirectory $adapterDir -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logDir 'multimodal.out.log') `
            -RedirectStandardError (Join-Path $logDir 'multimodal.err.log')
        Write-Host 'Started the local NachoBot TTS bridge.'
    }

    if (-not (Wait-TTSBridgeReady)) {
        Write-Warning 'The local TTS bridge was not ready within 60 seconds. Microsoft SAPI will be used.'
    }
}
catch {
    Write-Warning "The local TTS bridge failed to start: $($_.Exception.Message). Microsoft SAPI will be used."
}

if ($voxReady) {
    Write-Host 'TTS priority: local VoxCPM2 -> Microsoft SAPI fallback.'
}
else {
    Write-Host 'TTS fallback active: Microsoft SAPI.'
}
