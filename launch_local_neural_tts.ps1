param(
    [string]$VoxRoot = ""
)

# Starts only the local VoxCPM2 neural-TTS stack.  It never starts or restarts
# NachoBot Core, so it is safe to use while the live-control page is open.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($VoxRoot)) {
    $VoxRoot = $env:NACHOBOT_VOXCPM_ROOT
}
if ([string]::IsNullOrWhiteSpace($VoxRoot)) {
    $VoxRoot = Join-Path $root 'VoxCPM'
}
$voxRoot = [System.IO.Path]::GetFullPath($VoxRoot)
$voxPython = Join-Path $voxRoot '.venv\Scripts\python.exe'
$voxServer = Join-Path $root 'NachoBot-Multimodal-Adapter\src\tts\backends\Vox\vox_api_server.py'
$modelDir = Join-Path $voxRoot 'models\openbmb__VoxCPM2'
$adapterDir = Join-Path $root 'NachoBot-Multimodal-Adapter'
$adapterPython = Join-Path $adapterDir '.venv\Scripts\python.exe'
# The trained Nacho voice lives with the multimodal adapter's model assets.
# The old Live2D resources path was stale, so launches silently fell back to
# the generic VoxCPM base voice even though the fixed voice files were present.
$loraWeights = Join-Path $adapterDir 'models\tts\voxcpm\ncnk3'
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

function Warm-TTSBridge {
    <#
    The first VoxCPM request after CUDA/model startup includes kernel and
    decoder warm-up.  Run the same stream route once before the desktop pet
    accepts chat so that the user's first sentence is not mistaken for a
    broken TTS engine and delays the first spoken reply.
    #>
    $warmupBody = @{ text = 'warmup'; platform = 'local.host.cute'; text_lang = 'auto'; voice_preset = 'default'; split_method = 'cut0' } | ConvertTo-Json -Compress
    $previousProgressPreference = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        $warmup = Invoke-WebRequest `
            -Uri 'http://127.0.0.1:8070/api/tts-stream' `
            -Method Post `
            -ContentType 'application/json; charset=utf-8' `
            -Body $warmupBody `
            -TimeoutSec 90 `
            -UseBasicParsing
        if ($warmup.StatusCode -eq 200 -and $warmup.RawContentLength -gt 0) {
            Write-Host "Warmed the local VoxCPM stream ($($warmup.RawContentLength) bytes)."
            return $true
        }
        Write-Warning 'VoxCPM warm-up returned an empty response. The runtime will keep audio silent.'
    }
    catch {
        Write-Warning "VoxCPM warm-up failed: $($_.Exception.Message). The runtime will keep audio silent."
    }
    finally {
        $ProgressPreference = $previousProgressPreference
    }
    return $false
}

$voxReady = $false
if (-not (Test-Path -LiteralPath (Join-Path $modelDir 'model.safetensors'))) {
    Write-Warning "VoxCPM2 model is incomplete: $modelDir. Desktop-pet audio will remain silent."
}
else {
    try {
        if (-not (Test-LocalPort 9880)) {
            $usingFixedVoiceLora = $false
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
                $usingFixedVoiceLora = $true
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
            if ($usingFixedVoiceLora) {
                Write-Host "Started local VoxCPM2 with fixed-voice LoRA: $loraWeights"
            }
            else {
                Write-Host 'Started base VoxCPM2; the desktop pet keeps one fixed reference preset and seed.'
            }
        }

        Write-Host 'Waiting for VoxCPM2 before enabling desktop-pet speech.'
        $voxReady = Wait-VoxModelReady
        if ($voxReady) {
            Write-Host 'VoxCPM2 model is ready and remains the preferred voice.'
        }
        else {
            Write-Warning 'VoxCPM2 did not become ready within 300 seconds. Desktop-pet audio will remain silent.'
        }
    }
    catch {
        Write-Warning "VoxCPM2 startup failed: $($_.Exception.Message). Desktop-pet audio will remain silent."
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

    $ttsBridgeReady = Wait-TTSBridgeReady
    if (-not $ttsBridgeReady) {
        Write-Warning 'The local TTS bridge was not ready within 60 seconds. Desktop-pet audio will remain silent.'
    }
    elseif ($voxReady) {
        [void](Warm-TTSBridge)
    }
}
catch {
    Write-Warning "The local TTS bridge failed to start: $($_.Exception.Message). Desktop-pet audio will remain silent."
}

if ($voxReady) {
    Write-Host 'TTS source: local VoxCPM2 only.'
}
else {
    Write-Host 'TTS unavailable: desktop-pet audio remains muted until VoxCPM2 is ready.'
}
