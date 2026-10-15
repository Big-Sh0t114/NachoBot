[CmdletBinding()]
param(
    [string]$MsvcBin = 'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64'
)

$ErrorActionPreference = 'Stop'
$cl = Join-Path $MsvcBin 'cl.exe'
$link = Join-Path $MsvcBin 'link.exe'
$lib = Join-Path $MsvcBin 'lib.exe'
foreach ($tool in @($cl, $link, $lib)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "Required MSVC tool not found: $tool" }
}

$testDir = $PSScriptRoot
$nativeDir = Split-Path -Parent $testDir
$outDir = Join-Path $testDir 'build\x64'
New-Item -ItemType Directory -Path $outDir -Force | Out-Null
$kernelLib = Join-Path $outDir 'kernel32_minimal.lib'
$kernelDef = Join-Path $nativeDir 'kernel32_minimal.def'
& $lib /nologo "/def:$kernelDef" /machine:x64 "/out:$kernelLib"
if ($LASTEXITCODE -ne 0) { throw "Test-only Kernel32 import library generation failed with exit code $LASTEXITCODE" }

$shimTestObj = Join-Path $outDir 'qqcall_shim_test.obj'
$shimTestDll = Join-Path $outDir 'QQCallShim.dll'
$shimTestDef = Join-Path $testDir 'qqcall_shim_test.def'
& $cl /nologo /X /c /TP /std:c++14 /W4 /WX /O1 /Oi /GS- /GR- /EHs-c- /Zl /DQQCALL_SHIM_TEST_HOOKS "/Fo:$shimTestObj" (Join-Path $nativeDir 'qqcall_shim.cpp')
if ($LASTEXITCODE -ne 0) { throw "Test shim compile failed with exit code $LASTEXITCODE" }
& $link /nologo /dll /machine:x64 /nodefaultlib /entry:DllMain "/def:$shimTestDef" "/out:$shimTestDll" /incremental:no $shimTestObj $kernelLib
if ($LASTEXITCODE -ne 0) { throw "Test shim link failed with exit code $LASTEXITCODE" }

$mockObj = Join-Path $outDir 'mock_qqnt.obj'
$mockDll = Join-Path $outDir 'QQNT.dll'
$mockImport = Join-Path $outDir 'QQNT.lib'
$mockDef = Join-Path $testDir 'mock_qqnt.def'
& $lib /nologo "/def:$mockDef" /machine:x64 "/out:$mockImport"
if ($LASTEXITCODE -ne 0) { throw "Mock QQNT import library generation failed with exit code $LASTEXITCODE" }
& $cl /nologo /X /c /TP /std:c++14 /W4 /WX /wd4100 /O1 /Oi /GS- /GR- /EHs-c- /Zl "/Fo:$mockObj" (Join-Path $testDir 'mock_qqnt.cpp')
if ($LASTEXITCODE -ne 0) { throw "Mock QQNT compile failed with exit code $LASTEXITCODE" }
& $link /nologo /dll /machine:x64 /nodefaultlib /entry:DllMain "/def:$mockDef" "/out:$mockDll" /incremental:no $mockObj $kernelLib
if ($LASTEXITCODE -ne 0) { throw "Mock QQNT link failed with exit code $LASTEXITCODE" }

$wrapperObj = Join-Path $outDir 'wrapper_node.obj'
$wrapperDll = Join-Path $outDir 'wrapper.node'
$wrapperImport = Join-Path $outDir 'wrapper_node.lib'
$wrapperDef = Join-Path $testDir 'wrapper_node.def'
& $cl /nologo /X /c /TP /std:c++14 /W4 /WX /wd4100 /O1 /Oi /GS- /GR- /EHs-c- /Zl "/Fo:$wrapperObj" (Join-Path $testDir 'wrapper_node.cpp')
if ($LASTEXITCODE -ne 0) { throw "Mock wrapper.node compile failed with exit code $LASTEXITCODE" }
& $link /nologo /dll /machine:x64 /nodefaultlib /entry:DllMain "/def:$wrapperDef" "/out:$wrapperDll" "/implib:$wrapperImport" /incremental:no $wrapperObj $mockImport $kernelLib
if ($LASTEXITCODE -ne 0) { throw "Mock wrapper.node link failed with exit code $LASTEXITCODE" }

$harnessObj = Join-Path $outDir 'qqcall_harness.obj'
$harnessExe = Join-Path $outDir 'qqcall_harness.exe'
& $cl /nologo /X /c /TP /std:c++14 /W4 /WX /wd4100 /O1 /Oi /GS- /GR- /EHs-c- /Zl "/Fo:$harnessObj" (Join-Path $testDir 'qqcall_harness.cpp')
if ($LASTEXITCODE -ne 0) { throw "Harness compile failed with exit code $LASTEXITCODE" }
& $link /nologo /machine:x64 /nodefaultlib /entry:HarnessMain /subsystem:console "/out:$harnessExe" /incremental:no $harnessObj $kernelLib
if ($LASTEXITCODE -ne 0) { throw "Harness link failed with exit code $LASTEXITCODE" }

Write-Output "Built isolated harness at $harnessExe"
