@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Launch NachoBot Bilibili
set "PYTHONNOUSERSITE=1"

set "ROOT=%~dp0"
set "BILIBILI_DIR=%ROOT%NachoBot-Bilibili-Adapter"
set "NACHOBOT_DIR=%ROOT%NachoBot"

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
echo [SYNC] NachoBot ...
cd /d "%NACHOBOT_DIR%"
uv sync
if errorlevel 1 echo [WARN] NachoBot uv sync failed.

echo [SYNC] NachoBot-Bilibili-Adapter ...
cd /d "%BILIBILI_DIR%"
uv sync
if errorlevel 1 echo [WARN] Bilibili Adapter uv sync failed.

REM ===== start Bilibili Adapter =====
echo.
echo [START] NachoBot-Bilibili-Adapter ...
start "NachoBot-Bilibili" /D "%BILIBILI_DIR%" cmd /k "chcp 65001>nul && set PYTHONPATH=%NACHOBOT_DIR%;%BILIBILI_DIR% && uv run --project ""%NACHOBOT_DIR%"" python main.py"

echo.
echo [DONE] Launch sequence complete.
echo.
endlocal