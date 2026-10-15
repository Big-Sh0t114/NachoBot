[CmdletBinding()]
param(
    [string]$SdkIncludeRoot = (Join-Path $env:TEMP 'windows-sdk-cpp-10.0.26100.8249\c\Include\10.0.26100.0'),
    [string]$SdkLibRoot = (Join-Path $env:TEMP 'windows-sdk-cpp-x64-10.0.26100.8249\c'),
    [string]$OutDir
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$testRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$nativeRoot = Split-Path -Parent (Split-Path -Parent $testRoot)
$repoRoot = Split-Path -Parent $nativeRoot
$routeRoot = Join-Path $nativeRoot 'audio_route'
$minhookRoot = Join-Path $routeRoot 'vendor\minhook'
if (-not $OutDir) { $OutDir = Join-Path $testRoot 'build' }
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
$dumpbin = Join-Path $clDir 'dumpbin.exe'
$includes = @($vcInclude, (Join-Path $SdkIncludeRoot 'shared'),
    (Join-Path $SdkIncludeRoot 'um'), (Join-Path $SdkIncludeRoot 'ucrt'))
$libs = @($vcLib, (Join-Path $SdkLibRoot 'um\x64'), (Join-Path $SdkLibRoot 'ucrt\x64'))
foreach ($path in $includes + $libs) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "toolchain_path_missing:$path" }
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$exe = Join-Path $OutDir 'QQCallAudioRouteMockHarness-v1.exe'
$sources = @(
    (Join-Path $testRoot 'route_mock_tests.cpp'),
    (Join-Path $routeRoot 'com_wrappers.cpp'),
    (Join-Path $routeRoot 'child_hooks.cpp'),
    (Join-Path $routeRoot 'hook_runtime.cpp'),
    (Join-Path $minhookRoot 'src\buffer.c'),
    (Join-Path $minhookRoot 'src\hook.c'),
    (Join-Path $minhookRoot 'src\trampoline.c'),
    (Join-Path $minhookRoot 'src\hde\hde64.c')
)
$args = @('/nologo', '/std:c++17', '/EHsc', '/O2', '/MT', '/guard:cf', '/DWIN32_LEAN_AND_MEAN',
    '/DNOMINMAX', '/DUNICODE', '/D_UNICODE', '/DWINVER=0x0A00', '/D_WIN32_WINNT=0x0A00',
    '/DQAR_TESTING', '/D_CRT_SECURE_NO_WARNINGS', '/I', $routeRoot,
    '/I', (Join-Path $minhookRoot 'include'), '/I', (Join-Path $minhookRoot 'src'))
foreach ($include in $includes) { $args += @('/I', $include) }
$args += $sources
$args += @('/link', "/OUT:$exe", '/SUBSYSTEM:CONSOLE', '/INCREMENTAL:NO', '/OPT:REF', '/OPT:ICF',
    '/DYNAMICBASE', '/NXCOMPAT', '/HIGHENTROPYVA', '/guard:cf', '/MACHINE:X64',
    "/LIBPATH:$($libs[0])", "/LIBPATH:$($libs[1])", "/LIBPATH:$($libs[2])",
    'Mmdevapi.lib', 'Ole32.lib', 'OleAut32.lib', 'Uuid.lib', 'Propsys.lib', 'Winmm.lib',
    'Dsound.lib', 'Advapi32.lib', 'Shell32.lib', 'User32.lib')
& $cl @args
if ($LASTEXITCODE -ne 0) { throw "mock_harness_build_failed:$LASTEXITCODE" }

$loadConfig = & $dumpbin /nologo /loadconfig $exe 2>&1
if ($LASTEXITCODE -ne 0) { throw 'mock_harness_loadconfig_inspection_failed' }
$loadConfigText = $loadConfig -join "`n"
if ($loadConfigText -notmatch 'CF Instrumented' -or $loadConfigText -notmatch 'Guard CF function table') {
    throw 'mock_harness_cfg_metadata_missing'
}
$imports = & $dumpbin /nologo /imports $exe 2>&1
if ($LASTEXITCODE -ne 0) { throw 'mock_harness_import_inspection_failed' }
$exportReport = & $dumpbin /nologo /exports $exe 2>&1
if ($LASTEXITCODE -ne 0) { throw 'mock_harness_export_inspection_failed' }
& $exe
if ($LASTEXITCODE -ne 0) { throw "mock_harness_failed:$LASTEXITCODE" }

[pscustomobject]@{
    harness = $exe
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
    bytes = (Get-Item -LiteralPath $exe).Length
    cfgMetadata = 'present'
    imports = @($imports | Select-String -Pattern '^\s+[A-Za-z0-9_.-]+\.dll$' | ForEach-Object { $_.Line.Trim() })
} | ConvertTo-Json -Depth 3 -Compress
