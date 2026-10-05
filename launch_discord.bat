@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Launch NachoBot Discord
set "PYTHONNOUSERSITE=1"

set "ROOT=%~dp0"
set "NACHOBOT_FFMPEG_DIR=%ROOT%.runtime\ffmpeg"
set "DISCORD_ADAPTER_DIR=%ROOT%NachoBot-Discord-Adapter"

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
echo [SYNC] NachoBot-Discord-Adapter ...
cd /d "%DISCORD_ADAPTER_DIR%"
uv sync --locked
if errorlevel 1 (
  echo [ERROR] Discord Adapter uv sync failed.
  pause
  exit /b 1
)

if not exist "%ROOT%NachoBot\ensure_ffmpeg.py" (
  echo [ERROR] FFmpeg preparation script not found: %ROOT%NachoBot\ensure_ffmpeg.py
  pause
  exit /b 1
)

echo [FFMPEG] Checking shared FFmpeg binaries...
uv run python "%ROOT%NachoBot\ensure_ffmpeg.py"
if errorlevel 1 (
  echo [ERROR] Shared FFmpeg download or verification failed.
  pause
  exit /b 1
)

REM ===== start the unified Discord adapter only =====
echo [START] NachoBot-Discord-Adapter ...
start "NachoBot-Discord-Adapter" cmd /k "cd /d ""%DISCORD_ADAPTER_DIR%"" && uv run python main.py"

echo.
echo [DONE] Launch sequence complete.
echo.
endlocal
