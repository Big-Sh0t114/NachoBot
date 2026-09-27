param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "config.toml")
)

$ErrorActionPreference = "Stop"
$desktopPetDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$repositoryDirectory = Split-Path -Parent $desktopPetDirectory
$live2dDirectory = Join-Path $repositoryDirectory "NachoBot-Live2D-Adapter"
$backendLauncher = Join-Path $live2dDirectory "launch_live2d.ps1"
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path

if (-not (Test-Path -LiteralPath $backendLauncher -PathType Leaf)) {
    throw "Live2D rendering backend not found: $backendLauncher"
}

$uvCommand = Get-Command uv -ErrorAction Stop

function Copy-LegacyStateIfNeeded {
    $stateDirectory = Join-Path $desktopPetDirectory "state"
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null

    $stateMappings = @(
        @{
            Legacy = Join-Path $live2dDirectory "desktop_pet_state.json"
            Current = Join-Path $stateDirectory "desktop_pet_state.json"
        },
        @{
            Legacy = Join-Path $live2dDirectory "desktop_pet_chat_state.json"
            Current = Join-Path $stateDirectory "desktop_pet_chat_state.json"
        },
        @{
            Legacy = Join-Path $live2dDirectory "desktop_pet_chat_history.json"
            Current = Join-Path $stateDirectory "desktop_pet_chat_history.json"
        }
    )

    foreach ($mapping in $stateMappings) {
        if ((Test-Path -LiteralPath $mapping.Legacy -PathType Leaf) -and
            -not (Test-Path -LiteralPath $mapping.Current -PathType Leaf)) {
            Copy-Item -LiteralPath $mapping.Legacy -Destination $mapping.Current
            Write-Host "[MIGRATE] Copied existing desktop-pet state to $($mapping.Current)"
        }
    }
}

Copy-LegacyStateIfNeeded

Write-Host "[DESKTOP PET] Frontend: $desktopPetDirectory"
Write-Host "[DESKTOP PET] Live2D backend: $live2dDirectory"

Push-Location -LiteralPath $live2dDirectory
try {
    Write-Host "[SYNC] Live2D backend dependencies..."
    & $uvCommand.Source sync
    if ($LASTEXITCODE -ne 0) {
        throw "Live2D backend dependency sync failed"
    }
}
finally {
    Pop-Location
}

$powershellCommand = (Get-Command powershell.exe -ErrorAction Stop).Source
& $powershellCommand `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $backendLauncher `
    -ConfigPath $resolvedConfig `
    -ModeOverride "desktop_pet" `
    -DesktopPetRoot $desktopPetDirectory
exit $LASTEXITCODE
