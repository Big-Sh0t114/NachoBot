[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$SelfCharacterId,
    [string]$WorkerPython,
    [string]$PluginConfig,
    [string]$ProbeImage,
    [ValidateRange(0.1, 600.0)][double]$Timeout = 60.0
)

$coreRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$corePython = [System.IO.Path]::GetFullPath((Join-Path $coreRoot '.venv\Scripts\python.exe'))
$cli = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'deploy.py'))
if (-not (Test-Path -LiteralPath $corePython -PathType Leaf)) {
    throw "Core Python was not found: $corePython"
}

$arguments = @($cli, '--data-dir', $DataDir, '--version', $Version, '--self-character-id', $SelfCharacterId)
if (-not [string]::IsNullOrWhiteSpace($WorkerPython)) {
    $arguments += @('--worker-python', $WorkerPython)
}
if (-not [string]::IsNullOrWhiteSpace($PluginConfig)) {
    $arguments += @('--plugin-config', $PluginConfig)
}
if (-not [string]::IsNullOrWhiteSpace($ProbeImage)) {
    $arguments += @('--probe-image', $ProbeImage)
}
$arguments += @('--timeout', $Timeout.ToString([Globalization.CultureInfo]::InvariantCulture), '--install-missing')

& $corePython @arguments
exit $LASTEXITCODE
