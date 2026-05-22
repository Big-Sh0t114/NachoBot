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
echo [OK] Opening browser at http://127.0.0.1:8088
start http://127.0.0.1:8088

echo [INFO] Starting WebUI server...
uv run python server.py
