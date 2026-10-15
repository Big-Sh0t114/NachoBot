[CmdletBinding()]
param(
    [string]$SdkIncludeRoot,
    [string]$SdkLibRoot,
    [string]$OutDir
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$nativeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$sdkCacheRoot = Join-Path (Split-Path -Parent $nativeRoot) '.runtime\toolchain\sdk'
if (-not $SdkIncludeRoot) {
    $SdkIncludeRoot = Join-Path $sdkCacheRoot 'microsoft.windows.sdk.cpp\c\Include\10.0.26100.0'
    if (-not (Test-Path -LiteralPath $SdkIncludeRoot -PathType Container)) {
        $SdkIncludeRoot = Join-Path $env:TEMP 'windows-sdk-cpp-10.0.26100.8249\c\Include\10.0.26100.0'
    }
}
if (-not $SdkLibRoot) {
    $SdkLibRoot = Join-Path $sdkCacheRoot 'microsoft.windows.sdk.cpp.x64\c'
    if (-not (Test-Path -LiteralPath $SdkLibRoot -PathType Container)) {
        $SdkLibRoot = Join-Path $env:TEMP 'windows-sdk-cpp-x64-10.0.26100.8249\c'
    }
}
$routeRoot = Join-Path $nativeRoot 'audio_route'
$minhookRoot = Join-Path $routeRoot 'vendor\minhook'
if (-not $OutDir) { $OutDir = Join-Path $routeRoot 'build' }
$OutDir = [System.IO.Path]::GetFullPath($OutDir)

$clCandidates = Get-ChildItem -LiteralPath 'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC' `
    -Filter cl.exe -Recurse -File | Where-Object { $_.FullName -match '\\bin\\Hostx64\\x64\\cl\.exe$' } |
    Sort-Object FullName -Descending
if (-not $clCandidates) { throw 'MSVC_x64_cl_not_found' }
$cl = $clCandidates[0].FullName
$clDir = Split-Path -Parent $cl
$msvcRoot = Split-Path (Split-Path (Split-Path $clDir -Parent) -Parent) -Parent
$vcInclude = Join-Path $msvcRoot 'include'
$vcLib = Join-Path $msvcRoot 'lib\x64'
$link = Join-Path $clDir 'link.exe'

$requiredIncludes = @(
    $vcInclude,
    (Join-Path $SdkIncludeRoot 'shared'),
    (Join-Path $SdkIncludeRoot 'um'),
    (Join-Path $SdkIncludeRoot 'ucrt')
)
$requiredLibs = @(
    $vcLib,
    (Join-Path $SdkLibRoot 'um\x64'),
    (Join-Path $SdkLibRoot 'ucrt\x64')
)
foreach ($path in $requiredIncludes + $requiredLibs) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "toolchain_path_missing:$path" }
}
if (-not (Test-Path -LiteralPath (Join-Path $SdkIncludeRoot 'um\mmdeviceapi.h') -PathType Leaf)) {
    throw 'sdk_mmdeviceapi_header_missing'
}
if (-not (Test-Path -LiteralPath (Join-Path $SdkLibRoot 'um\x64\Mmdevapi.lib') -PathType Leaf)) {
    throw 'sdk_mmdevapi_library_missing'
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$common = @(
    '/nologo', '/std:c++17', '/EHsc', '/O2', '/MT', '/guard:cf', '/DWIN32_LEAN_AND_MEAN',
    '/DNOMINMAX', '/DUNICODE', '/D_UNICODE', '/DWINVER=0x0A00', '/D_WIN32_WINNT=0x0A00',
    '/D_CRT_SECURE_NO_WARNINGS', '/I', $routeRoot, '/I', (Join-Path $minhookRoot 'include'),
    '/I', (Join-Path $minhookRoot 'src'), "/Fo:$OutDir\"
)
foreach ($include in $requiredIncludes) { $common += @('/I', $include) }

$minhookSources = @(
    (Join-Path $minhookRoot 'src\buffer.c'),
    (Join-Path $minhookRoot 'src\hook.c'),
    (Join-Path $minhookRoot 'src\trampoline.c'),
    (Join-Path $minhookRoot 'src\hde\hde64.c')
)
$linkArgs = @(
    '/INCREMENTAL:NO', '/OPT:REF', '/OPT:ICF', '/DYNAMICBASE', '/NXCOMPAT', '/HIGHENTROPYVA',
    '/guard:cf', '/MACHINE:X64', "/LIBPATH:$($requiredLibs[0])", "/LIBPATH:$($requiredLibs[1])",
    "/LIBPATH:$($requiredLibs[2])", 'Mmdevapi.lib', 'Ole32.lib', 'Uuid.lib', 'Propsys.lib',
    'Advapi32.lib', 'Shell32.lib', 'User32.lib', 'Winmm.lib', 'Dsound.lib', 'OleAut32.lib', 'Bcrypt.lib', 'OneCore.lib'
)

$nodeOut = Join-Path $OutDir 'QQCallAudioRouteGuard-v2.node'
$nodeSources = @(
    (Join-Path $routeRoot 'node_module.cpp'),
    (Join-Path $routeRoot 'parent_hooks.cpp'),
    (Join-Path $routeRoot 'hook_runtime.cpp')
) + $minhookSources
$nodeArgs = $common + @('/LD') + $nodeSources + @('/link', "/OUT:$nodeOut",
    "/IMPLIB:$OutDir\QQCallAudioRouteGuard-v2-node.lib", "/DEF:$($routeRoot)\node_exports.def") + $linkArgs
& $cl @nodeArgs
if ($LASTEXITCODE -ne 0) { throw "node_build_failed:$LASTEXITCODE" }

$dllOut = Join-Path $OutDir 'QQCallAudioRouteGuard-v2.dll'
$dllSources = @(
    (Join-Path $routeRoot 'child_bootstrap.cpp'),
    (Join-Path $routeRoot 'child_hooks.cpp'),
    (Join-Path $routeRoot 'com_wrappers.cpp'),
    (Join-Path $routeRoot 'hook_runtime.cpp')
) + $minhookSources
$dllArgs = $common + @('/LD') + $dllSources + @('/link', "/OUT:$dllOut",
    "/IMPLIB:$OutDir\QQCallAudioRouteGuard-v2-child.lib", "/DEF:$($routeRoot)\child_exports.def") + $linkArgs
& $cl @dllArgs
if ($LASTEXITCODE -ne 0) { throw "dll_build_failed:$LASTEXITCODE" }

$dumpbin = Join-Path $clDir 'dumpbin.exe'
foreach ($artifact in @($nodeOut, $dllOut)) {
    $loadConfig = & $dumpbin /nologo /loadconfig $artifact 2>&1
    if ($LASTEXITCODE -ne 0) { throw "loadconfig_inspection_failed:$artifact" }
    $text = $loadConfig -join "`n"
    if ($text -notmatch 'CF Instrumented' -or $text -notmatch 'Guard CF function table') {
        throw "cfg_metadata_missing:$artifact"
    }
    $exports = & $dumpbin /nologo /exports $artifact 2>&1
    if ($LASTEXITCODE -ne 0) { throw "export_inspection_failed:$artifact" }
    $exportText = $exports -join "`n"
    if ($artifact -eq $nodeOut -and ($exportText -notmatch 'napi_register_module_v1')) {
        throw 'node_napi_export_missing'
    }
    if ($artifact -eq $dllOut -and ($exportText -notmatch 'QQCallAudioRouteGuardChildStartV1')) {
        throw 'child_start_export_missing'
    }
}

$artifactReport = @(
    [pscustomobject]@{ name = [System.IO.Path]::GetFileName($nodeOut); sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $nodeOut).Hash.ToLowerInvariant(); bytes = (Get-Item -LiteralPath $nodeOut).Length },
    [pscustomobject]@{ name = [System.IO.Path]::GetFileName($dllOut); sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $dllOut).Hash.ToLowerInvariant(); bytes = (Get-Item -LiteralPath $dllOut).Length }
)
$artifactReport | ConvertTo-Json -Compress
$publicManifest = [ordered]@{ schema = 1; qqBuild = '9.9.26-44343'; artifacts = $artifactReport }
$publicManifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutDir 'artifact-manifest.json') -Encoding utf8
