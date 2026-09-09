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
                return
            }
        }
        catch {
            # The process opens the port only after loading the 4.5 GB model.
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw 'VoxCPM2 在 300 秒内未完成模型加载，请查看 runtime/logs/vox-server.err.log。'
}

function Wait-TTSBridgeReady([int] $TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8070/api/health' -TimeoutSec 3
            if ($health.status -eq 'ok' -and @($health.tts_backends).Count -gt 0) {
                return
            }
        }
        catch {
            # Keep polling while the lightweight bridge initializes.
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw '本地 TTS 桥接服务在 60 秒内未就绪，请查看 runtime/logs/multimodal.err.log。'
}

if (-not (Test-Path -LiteralPath (Join-Path $modelDir 'model.safetensors'))) {
    throw "VoxCPM2 模型尚未下载完成：$modelDir"
}

if (-not (Test-LocalPort 9880)) {
    $previousVoxDir = $env:VOXCPM_DIR
    $env:VOXCPM_DIR = $voxRoot
    try {
        Start-Process -FilePath $voxPython `
            -ArgumentList @($voxServer, '--host', '127.0.0.1', '--port', '9880', '--model-dir', $modelDir, '--no-denoiser') `
            -WorkingDirectory $voxRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logDir 'vox-server.out.log') `
            -RedirectStandardError (Join-Path $logDir 'vox-server.err.log')
    }
    finally {
        $env:VOXCPM_DIR = $previousVoxDir
    }
    Write-Host '已启动本机 VoxCPM2，首次加载模型需要约一分钟。'
}

Write-Host '正在等待 VoxCPM2 模型完全加载；完成前不会启用机械浏览器语音…'
Wait-VoxModelReady
Write-Host 'VoxCPM2 模型已就绪。'

if (-not (Test-LocalPort 8070)) {
    Start-Process -FilePath $adapterPython -ArgumentList @('main.py') `
        -WorkingDirectory $adapterDir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir 'multimodal.out.log') `
        -RedirectStandardError (Join-Path $logDir 'multimodal.err.log')
    Write-Host '已启动 NachoBot 本机 TTS 桥接服务。'
}

Wait-TTSBridgeReady
Write-Host '本地神经语音链路已就绪：http://127.0.0.1:8070/api/health'
