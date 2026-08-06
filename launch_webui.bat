@echo off
setlocal EnableExtensions
chcp 65001 >nul
title NachoBot WebUI
set "ROOT=%~dp0"
set "NACHOBOT_DIR=%ROOT%NachoBot"
set "WEBUI_DIR=%ROOT%webui"
set "NACHOBOT_FFMPEG_DIR=%ROOT%.runtime\ffmpeg"

echo ===== NachoBot WebUI =====
echo.

REM ===== Check uv =====
where uv >nul 2>&1
if errorlevel 1 (
    echo [INFO] uv not found, installing...
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

REM ===== Prepare shared FFmpeg =====
if not exist "%NACHOBOT_DIR%\pyproject.toml" (
    echo [ERROR] NachoBot pyproject.toml not found: %NACHOBOT_DIR%
    pause
    exit /b 1
)
if not exist "%ROOT%scripts\ensure_ffmpeg.py" (
    echo [ERROR] FFmpeg preparation script not found: %ROOT%scripts\ensure_ffmpeg.py
    pause
    exit /b 1
)

echo [INFO] Syncing NachoBot dependencies for FFmpeg preparation...
cd /d "%NACHOBOT_DIR%"
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
    echo [ERROR] NachoBot uv sync failed.
    pause
    exit /b 1
)

echo [INFO] Checking shared FFmpeg binaries...
uv run python "%ROOT%scripts\ensure_ffmpeg.py"
if errorlevel 1 (
    echo [ERROR] Shared FFmpeg download or verification failed.
    pause
    exit /b 1
)

REM ===== Sync WebUI dependencies =====
echo [INFO] Syncing WebUI dependencies...
cd /d "%WEBUI_DIR%"
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
    echo [ERROR] WebUI uv sync failed.
    pause
    exit /b 1
)

echo.
for /f "usebackq tokens=*" %%i in (`uv run python -c "from webui_config import webui_config; h = '127.0.0.1' if webui_config.host == '0.0.0.0' else webui_config.host; print(f'http://{h}:{webui_config.port}')"`) do set "WEBUI_URL=%%i"
echo [OK] Opening browser at %WEBUI_URL%
start %WEBUI_URL%

echo [INFO] Starting WebUI server...
uv run python server.py
