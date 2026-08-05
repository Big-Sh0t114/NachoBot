@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Launch NachoBot Universal Voice Adapter
set "PYTHONNOUSERSITE=1"

set "ROOT=%~dp0"
set "NACHOBOT_FFMPEG_DIR=%ROOT%.runtime\ffmpeg"
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
if errorlevel 1 (
  echo [ERROR] UniversalVC Adapter uv sync failed.
  pause
  exit /b 1
)

if not exist "%ROOT%scripts\ensure_ffmpeg.py" (
  echo [ERROR] FFmpeg preparation script not found: %ROOT%scripts\ensure_ffmpeg.py
  pause
  exit /b 1
)

echo [FFMPEG] Checking shared FFmpeg binaries...
uv run python "%ROOT%scripts\ensure_ffmpeg.py"
if errorlevel 1 (
  echo [ERROR] Shared FFmpeg download or verification failed.
  pause
  exit /b 1
)

REM ===== download models =====
echo [MODELS] Checking and downloading required ML models...
uv run python download_models.py
if errorlevel 1 (
    echo [ERROR] Model download failed. The adapter may not work correctly.
    pause
)

REM ===== start NachoBot-UniversalVC-Adapter =====
echo [START] NachoBot-UniversalVC-Adapter ...
start "NachoBot-UniversalVC-Adapter" cmd /k "cd /d ""%UNIVERSALVC_ADAPTER_DIR%"" && uv run python main.py"

echo.
echo [DONE] Universal Voice Adapter launch complete.
echo.
endlocal
