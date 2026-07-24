@echo off
setlocal EnableExtensions
chcp 65001 >nul
title NachoBot Live2D Adapter
set "PYTHONNOUSERSITE=1"

set "ROOT=%~dp0"
set "LIVE2D_DIR=%ROOT%"

if not exist "%LIVE2D_DIR%\" (
    echo [ERROR] Live2D Adapter directory not found: %LIVE2D_DIR%
    pause
    exit /b 1
)

REM ===== check and install uv =====
where uv >nul 2>&1
if errorlevel 1 (
    echo [INFO] uv not found, auto installing...
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    if errorlevel 1 (
        echo [ERROR] uv install failed. Please install manually using: pip install uv
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
    echo [OK] uv installed.
)

REM ===== sync dependencies =====
cd /d "%LIVE2D_DIR%"
echo [SYNC] NachoBot-Live2D-Adapter ...
uv sync
if errorlevel 1 (
    echo [ERROR] Live2D Adapter uv sync failed.
    pause
    exit /b 1
)

echo [NachoBot Live2D Adapter] Starting...
uv run python -m live2d_adapter --config "config.toml"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [NachoBot Live2D Adapter] Exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
