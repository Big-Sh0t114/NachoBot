@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Launch NachoBot Universal Voice Adapter
set "PYTHONNOUSERSITE=1"

set "ROOT=%~dp0"
set "UNIVERSALVC_ADAPTER_DIR=%ROOT%NachoBot-UniversalVC-Adapter"

REM ===== check and install uv =====
where uv >nul 2>&1
if errorlevel 1 (
  echo [INFO] uv not found, auto installing...
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  if errorlevel 1 (
    echo [ERROR] uv install failed. Please install manually using pip install uv.
    pause
    exit /b 1
  )
  set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
  where uv >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] uv installed but not found in PATH. Please restart terminal.
    pause
    exit /b 1
  )
  echo [OK] uv installed!
)

REM ===== sync deps =====
echo [SYNC] NachoBot-UniversalVC-Adapter ...
cd /d "%UNIVERSALVC_ADAPTER_DIR%"
uv sync
if errorlevel 1 echo [WARN] UniversalVC Adapter uv sync failed.

REM ===== start NachoBot-UniversalVC-Adapter =====
echo [START] NachoBot-UniversalVC-Adapter ...
start "NachoBot-UniversalVC-Adapter" cmd /k "cd /d ""%UNIVERSALVC_ADAPTER_DIR%"" && uv run python main.py"

echo.
echo [DONE] Universal Voice Adapter launch complete.
echo.
endlocal
