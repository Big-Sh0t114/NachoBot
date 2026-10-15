[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('run', 'check', 'plan', 'status', 'stop')]
    [string] $Action = 'status',

    [Parameter(Position = 1)]
    [string] $Config = ''
)

$qqcallProjectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $qqcallProjectRoot
$pythonProjectRoot = Join-Path $workspaceRoot 'NachoBot'
$managerPath = Join-Path $qqcallProjectRoot 'service\manager.py'
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uvCommand) {
    [Console]::Error.WriteLine('uv executable is unavailable.')
    exit 127
}

if ([string]::IsNullOrWhiteSpace($Config)) {
    $resolvedConfig = Join-Path $workspaceRoot 'NachoBot\plugins\qqcall_plugin\config.toml'
}
elseif ([IO.Path]::IsPathRooted($Config)) {
    $resolvedConfig = [IO.Path]::GetFullPath($Config)
}
else {
    $resolvedConfig = [IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Config))
}

& uv run --project $pythonProjectRoot --locked python $managerPath $Action --config $resolvedConfig
if ($null -eq $LASTEXITCODE) {
    exit 1
}
exit $LASTEXITCODE
