param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "config.toml")
)

$ErrorActionPreference = "Stop"
$adapterDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$uvCommand = Get-Command uv -ErrorAction Stop

Push-Location -LiteralPath $adapterDirectory
try {
    Write-Host "[SYNC] NachoBot-Live2D-Adapter..."
    & $uvCommand.Source sync
    if ($LASTEXITCODE -ne 0) {
        throw "Live2D adapter dependency sync failed"
    }
}
finally {
    Pop-Location
}

$powershellCommand = (Get-Command powershell.exe -ErrorAction Stop).Source
& $powershellCommand `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File (Join-Path $adapterDirectory "launch_live2d.ps1") `
    -ConfigPath $resolvedConfig `
    -ModeOverride "desktop_pet"
exit $LASTEXITCODE
