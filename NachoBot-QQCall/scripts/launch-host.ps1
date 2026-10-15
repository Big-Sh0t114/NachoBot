[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $QQExePath,
    [Parameter(Mandatory = $true)] [string] $AppPath,
    [Parameter(Mandatory = $true)] [string] $NapCatShellRoot,
    [string] $RuntimeDirectory = (Join-Path $PSScriptRoot "..\.runtime\standalone-host"),
    [ValidateRange(1, 65535)] [int] $ListenPort = 6111,
    [ValidateRange(1, 65535)] [int] $OutputPort = 6110,
    [Parameter(Mandatory = $true)] [ValidateRange(1, 2147483647)] [int] $HostGeneration,
    [string] $SessionName = "qqcall_av_host",
    [string] $AudioRouteGuardConfig,
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"
$stage = "validate_arguments"
$runRoot = $null

function Fail([string] $Code) {
    throw [System.InvalidOperationException]::new($Code)
}

function Resolve-ExistingPath([string] $Value, [string] $Code, [switch] $RequireDirectory) {
    if (-not [System.IO.Path]::IsPathRooted($Value) -or -not (Test-Path -LiteralPath $Value)) { Fail $Code }
    $item = Get-Item -LiteralPath $Value
    if ($RequireDirectory -and -not $item.PSIsContainer) { Fail $Code }
    if (-not $RequireDirectory -and $item.PSIsContainer) { Fail $Code }
    return $item.FullName
}

function Is-Under([string] $Candidate, [string] $Root) {
    $candidateFull = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    return $candidateFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidateFull.StartsWith(($rootFull + '\'), [System.StringComparison]::OrdinalIgnoreCase)
}

try {
    if ($ListenPort -eq $OutputPort) { Fail "ports_must_differ" }
    if ($SessionName -notmatch '^[A-Za-z0-9_.-]{1,80}$') { Fail "session_name_invalid" }

    $qqExe = Resolve-ExistingPath $QQExePath "qq_exe_missing"
    $appRoot = Resolve-ExistingPath $AppPath "app_path_missing" -RequireDirectory
    $shellRoot = Resolve-ExistingPath $NapCatShellRoot "napcat_shell_missing" -RequireDirectory
    $launcher = Resolve-ExistingPath (Join-Path $shellRoot "NapCatWinBootMain.exe") "napcat_launcher_missing"
    $hook = Resolve-ExistingPath (Join-Path $shellRoot "NapCatWinBootHook.dll") "napcat_hook_missing"
    $packageSource = Resolve-ExistingPath (Join-Path $shellRoot "qqnt.json") "napcat_package_metadata_missing"
    $plugin = Resolve-ExistingPath (Join-Path $appRoot "avsdk\AVSDKPlugin.dll") "avsdk_plugin_missing"

    $sourcePackage = $null
    try { $sourcePackage = Get-Content -LiteralPath $packageSource -Raw | ConvertFrom-Json }
    catch { Fail "napcat_package_metadata_invalid" }
    if (-not $sourcePackage -or $sourcePackage.name -ne "qq-chat" -or
        $sourcePackage.version -notmatch '^\d+\.\d+\.\d+-\d+$' -or
        $sourcePackage.verHash -notmatch '^[a-fA-F0-9]{8}$' -or
        $sourcePackage.platform -ne "win32" -or $sourcePackage.eleArch -ne "x64" -or
        $sourcePackage.main -notmatch '^\./[^/\\]+\.js$' -or
        -not ($sourcePackage.isPureShell -is [bool]) -or -not ($sourcePackage.isByteCodeShell -is [bool])) {
        Fail "napcat_package_metadata_unsupported"
    }

    $projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    $runtimeRoot = [System.IO.Path]::GetFullPath($RuntimeDirectory)
    $privateRuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".runtime"))
    if (-not (Is-Under $runtimeRoot $privateRuntimeRoot)) { Fail "runtime_must_be_inside_private_runtime" }
    if ((Is-Under $runtimeRoot $appRoot) -or (Is-Under $appRoot $runtimeRoot) -or
        (Is-Under $runtimeRoot $shellRoot) -or (Is-Under $shellRoot $runtimeRoot)) {
        Fail "runtime_path_overlaps_install"
    }

    $runtimeBase = [System.IO.Path]::GetFullPath($runtimeRoot)
    $runName = if ($DryRun) { "dryrun-host-$HostGeneration-$([guid]::NewGuid().ToString('N'))" } else { "host-$HostGeneration" }
    $runRoot = Join-Path $runtimeBase $runName
    $stage = "prepare_owned_runtime"
    if (Test-Path -LiteralPath $runRoot) { Fail "host_runtime_already_exists" }
    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
    $profile = Join-Path $runRoot "profile"
    $packageRoot = Join-Path $runRoot "package"
    New-Item -ItemType Directory -Path $profile,$packageRoot -Force | Out-Null

    $report = Join-Path $runRoot "host"
    $planPath = Join-Path $runRoot "launch-plan.json"
    $packagePath = Join-Path $packageRoot "qqnt.json"
    $loaderPath = Join-Path $packageRoot "earlyloader.js"
    $earlyMainPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\host\early-main.cjs"))
    $earlyLoaderSource = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\host\earlyloader.cjs"))
    $guardConfigPath = $null
    if ($AudioRouteGuardConfig) {
        $guardSource = Resolve-ExistingPath $AudioRouteGuardConfig "audio_guard_config_missing"
        if ((Get-Item -LiteralPath $guardSource).Length -gt 16384) { Fail "audio_guard_config_invalid" }
        $guardConfigPath = Join-Path $runRoot "audio-guard.private.json"
        Copy-Item -LiteralPath $guardSource -Destination $guardConfigPath
    }

    $plan = [ordered]@{
        schema = 1
        appPath = $appRoot
        runtimePath = $runRoot
        reportPath = $report
        profilePath = $profile
        listenPort = $ListenPort
        outputPort = $OutputPort
        hostGeneration = $HostGeneration
        sessionName = $SessionName
    }
    if ($guardConfigPath) { $plan.audioRouteGuardConfigPath = $guardConfigPath }
    $manifest = [ordered]@{
        name = "qq-chat"
        verHash = [string]$sourcePackage.verHash
        version = [string]$sourcePackage.version
        private = $true
        description = "QQCall standalone AVSDK Host loader"
        productName = "QQ"
        sideEffects = $true
        # The Hook intercepts the shell's original loadNapCat.js main path and
        # loads NAPCAT_LOAD_PATH as its early replacement script.
        main = [string]$sourcePackage.main
        buildVersion = [string]$sourcePackage.buildVersion
        isPureShell = [bool]$sourcePackage.isPureShell
        isByteCodeShell = [bool]$sourcePackage.isByteCodeShell
        platform = "win32"
        eleArch = "x64"
    }
    if ($sourcePackage.linuxVersion -is [string]) { $manifest.linuxVersion = $sourcePackage.linuxVersion }
    if ($sourcePackage.linuxVerHash -is [string]) { $manifest.linuxVerHash = $sourcePackage.linuxVerHash }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($planPath, (($plan | ConvertTo-Json -Depth 5) + "`n"), $utf8NoBom)
    [System.IO.File]::WriteAllText($packagePath, (($manifest | ConvertTo-Json -Depth 5) + "`n"), $utf8NoBom)
    Copy-Item -LiteralPath $earlyLoaderSource -Destination $loaderPath

    $result = [ordered]@{
        ok = $true
        dryRun = [bool]$DryRun
        hostGeneration = $HostGeneration
        listenPort = $ListenPort
        outputPort = $OutputPort
        runDirectory = $runRoot
        profilePath = $profile
        launchPlanPath = $planPath
        packagePath = $packagePath
        loaderPath = $loaderPath
        qqExePath = $qqExe
        appPath = $appRoot
        avsdkPluginPath = $plugin
        configGeneratedInsideHost = $true
        processLaunched = $false
    }

    if (-not $DryRun) {
        $stage = "prepare_process_environment"
        $environmentNames = @(
            "NAPCAT_PATCH_PACKAGE", "NAPCAT_LOAD_PATH", "NAPCAT_INJECT_PATH",
            "QQCALL_HOST_LAUNCH_PLAN", "QQCALL_HOST_MAIN", "QQCALL_AUDIO_ROUTE_GUARD_CONFIG"
        )
        $oldEnvironment = @{}
        foreach ($name in $environmentNames) { $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
        try {
            $env:NAPCAT_PATCH_PACKAGE = $packagePath
            $env:NAPCAT_LOAD_PATH = $loaderPath
            $env:NAPCAT_INJECT_PATH = $hook
            $env:QQCALL_HOST_LAUNCH_PLAN = $planPath
            $env:QQCALL_HOST_MAIN = $earlyMainPath
            [Environment]::SetEnvironmentVariable("QQCALL_AUDIO_ROUTE_GUARD_CONFIG", $guardConfigPath, "Process")
            $arguments = @(
                ('"{0}"' -f $qqExe),
                ('"{0}"' -f $hook),
                "--no-sandbox",
                ('--user-data-dir="{0}"' -f $profile)
            )
            $stdoutLog = Join-Path $runRoot "launcher.stdout.log"
            $stderrLog = Join-Path $runRoot "launcher.stderr.log"
            $stage = "start_process"
            $process = Start-Process -FilePath $launcher -WorkingDirectory $shellRoot -ArgumentList $arguments `
                -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -WindowStyle Hidden -PassThru
            $result.processLaunched = $true
            $result.launcherPid = $process.Id
            $result.launcherStdoutPath = $stdoutLog
            $result.launcherStderrPath = $stderrLog
        }
        finally {
            foreach ($name in $environmentNames) {
                [Environment]::SetEnvironmentVariable($name, $oldEnvironment[$name], "Process")
            }
        }
    }

    [Console]::Out.WriteLine(($result | ConvertTo-Json -Depth 5 -Compress))
}
catch {
    $code = "host_launcher_failed"
    if ($_.Exception.Message -match '^[a-z][a-z0-9_]{1,63}$') { $code = $_.Exception.Message }
    $safeExceptionType = "unavailable"
    if ($_.Exception -and $_.Exception.GetType().Name -match '^[A-Za-z][A-Za-z0-9_]{0,63}$') {
        $safeExceptionType = $_.Exception.GetType().Name
    }
    $safeErrorId = "unavailable"
    $errorId = [string]$_.FullyQualifiedErrorId
    if ($errorId -match '^[A-Za-z0-9_.,-]{1,192}$') { $safeErrorId = $errorId }
    $line = 0
    if ($_.InvocationInfo -and $_.InvocationInfo.ScriptLineNumber -gt 0) { $line = [int]$_.InvocationInfo.ScriptLineNumber }
    $diagnosticWritten = $false
    try {
        if ($runRoot -and (Test-Path -LiteralPath $runRoot -PathType Container)) {
            $projectRootForFailure = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
            $privateRootForFailure = [System.IO.Path]::GetFullPath((Join-Path $projectRootForFailure ".runtime"))
            if (Is-Under $runRoot $privateRootForFailure) {
                $failure = [ordered]@{
                    schema = 1
                    stage = $stage
                    exceptionType = $safeExceptionType
                    fullyQualifiedErrorId = $safeErrorId
                    line = $line
                    code = $code
                }
                $failurePath = Join-Path $runRoot "launcher.failure.json"
                [System.IO.File]::WriteAllText($failurePath, (($failure | ConvertTo-Json -Depth 3 -Compress) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
                $diagnosticWritten = $true
            }
        }
    }
    catch { $diagnosticWritten = $false }
    [Console]::Error.WriteLine((@{ ok = $false; code = $code; stage = $stage; failureDiagnosticWritten = $diagnosticWritten } | ConvertTo-Json -Compress))
    exit 1
}
