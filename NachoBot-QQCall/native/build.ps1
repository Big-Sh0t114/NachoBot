[CmdletBinding()]
param(
    [string]$MsvcBin = 'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64',
    [switch]$RecoveryAttempt,
    [string]$PythonExe = 'python'
)

$ErrorActionPreference = 'Stop'
$cl = Join-Path $MsvcBin 'cl.exe'
$link = Join-Path $MsvcBin 'link.exe'
$lib = Join-Path $MsvcBin 'lib.exe'
$dumpbin = Join-Path $MsvcBin 'dumpbin.exe'
foreach ($tool in @($cl, $link, $lib, $dumpbin)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) {
        throw "Required MSVC tool not found: $tool"
    }
}

$sourceDir = $PSScriptRoot
$outDir = Join-Path $sourceDir 'build\x64'
New-Item -ItemType Directory -Path $outDir -Force | Out-Null
$publishDir = $outDir
if ($RecoveryAttempt) {
    # Build and verify a candidate before touching the artifact used by a live service.
    $outDir = Join-Path $publishDir ('candidate-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}
$kernelLib = Join-Path $outDir 'kernel32_minimal.lib'
$kernelDef = Join-Path $sourceDir 'kernel32_minimal.def'
$artifactName = if ($RecoveryAttempt) { 'QQCallRecoveryShim2' } else { 'QQCallShim' }
$shimDef = Join-Path $sourceDir 'qqcall_shim.def'
if ($RecoveryAttempt) {
    $shimDef = Join-Path $outDir ($artifactName + '.def')
    $definition = "LIBRARY $artifactName`r`nEXPORTS`r`n  QQCallShimStart`r`n  QQCallShimCancel`r`n  QQCallShimGetStatus`r`n"
    [System.IO.File]::WriteAllText($shimDef, $definition, [System.Text.Encoding]::ASCII)
}
$object = Join-Path $outDir ($artifactName + '.obj')
$dll = Join-Path $outDir ($artifactName + '.dll')
$shimImport = Join-Path $outDir ($artifactName + '.lib')

& $lib /nologo "/def:$kernelDef" /machine:x64 "/out:$kernelLib"
if ($LASTEXITCODE -ne 0) { throw "Kernel32 import library generation failed with exit code $LASTEXITCODE" }

& $cl /nologo /X /c /TP /std:c++14 /W4 /WX /O1 /Oi /GS- /GR- /EHs-c- /Zl "/Fo:$object" (Join-Path $sourceDir 'qqcall_shim.cpp')
if ($LASTEXITCODE -ne 0) { throw "QQCallShim compile failed with exit code $LASTEXITCODE" }

& $link /nologo /dll /machine:x64 /nodefaultlib /entry:DllMain "/def:$shimDef" "/out:$dll" "/implib:$shimImport" /dynamicbase /highentropyva /nxcompat /incremental:no $object $kernelLib
if ($LASTEXITCODE -ne 0) { throw "QQCallShim link failed with exit code $LASTEXITCODE" }

if ($RecoveryAttempt) {
    & $PythonExe (Join-Path $sourceDir 'normalize_recovery_shim.py') $dll
    if ($LASTEXITCODE -ne 0) { throw 'Recovery Shim candidate failed pinned-release verification; existing artifact preserved.' }
    $publishedDll = Join-Path $publishDir ($artifactName + '.dll')
    if (-not (Test-Path -LiteralPath $publishedDll -PathType Leaf) -or
        (Get-FileHash -LiteralPath $publishedDll -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash) {
        Copy-Item -LiteralPath $dll -Destination $publishedDll -Force
    }
    $dll = $publishedDll
}

Write-Output "Built $dll"
& $dumpbin /nologo /headers $dll | Select-String -Pattern 'machine \(x64\)|DLL'
& $dumpbin /nologo /exports $dll
& $dumpbin /nologo /imports $dll | Select-String -Pattern 'KERNEL32.dll|QQCallShim|GetProcAddress|VirtualProtect|CreateToolhelp32Snapshot'
