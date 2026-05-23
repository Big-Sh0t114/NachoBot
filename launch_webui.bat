@echo off
chcp 65001 >nul
title NachoBot WebUI
cd /d "%~dp0\webui"

echo ===== NachoBot WebUI =====
echo.

REM ===== Check uv =====
where uv >nul 2>&1
if errorlevel 1 (
    echo [INFO] uv not found, installing...
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

REM ===== Sync dependencies =====
echo [INFO] Syncing dependencies...
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
    echo [ERROR] uv sync failed.
    pause
    exit /b 1
)

echo.
for /f "usebackq tokens=*" %%i in (`uv run python -c "from webui_config import webui_config; h = '127.0.0.1' if webui_config.host == '0.0.0.0' else webui_config.host; print(f'http://{h}:{webui_config.port}')"`) do set "WEBUI_URL=%%i"
echo [OK] Opening browser at %WEBUI_URL%
start %WEBUI_URL%

echo [INFO] Starting WebUI server...
uv run python server.py
