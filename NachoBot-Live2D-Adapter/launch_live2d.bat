@echo off
setlocal EnableExtensions
chcp 65001 >nul
title NachoBot Live2D Adapter
set "PYTHONNOUSERSITE=1"
set "LIVE2D_DIR=%~dp0"

if not exist "%LIVE2D_DIR%config.toml" (
    echo [ERROR] Live2D configuration not found: %LIVE2D_DIR%config.toml
    pause
    exit /b 1
)

where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv is required to launch the Live2D adapter.
    echo Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run this file again.
    pause
    exit /b 1
)

cd /d "%LIVE2D_DIR%"
echo [SYNC] NachoBot-Live2D-Adapter ...
call uv sync --locked
if errorlevel 1 (
    echo [ERROR] Live2D Adapter dependency sync failed.
    pause
    exit /b 1
)

set "LAUNCH_ARGS="
if "%~1"=="" goto launch
if /I "%~1"=="desktop_pet" goto desktop_pet
if /I "%~1"=="live" goto live
if /I "%~1"=="logs" goto logs
echo Usage: launch_live2d.bat [desktop_pet^|live^|logs ALL^|LIVE2D^|CORE]
exit /b 2

:desktop_pet
if not "%~2"=="" goto bad_arguments
set "LAUNCH_ARGS=--mode desktop_pet"
goto launch

:live
if not "%~2"=="" goto bad_arguments
set "LAUNCH_ARGS=--mode live"
goto launch

:logs
if /I "%~2"=="ALL" set "LAUNCH_ARGS=--show-logs ALL"
if /I "%~2"=="LIVE2D" set "LAUNCH_ARGS=--show-logs LIVE2D"
if /I "%~2"=="CORE" set "LAUNCH_ARGS=--show-logs CORE"
if "%LAUNCH_ARGS%"=="" goto bad_arguments
if not "%~3"=="" goto bad_arguments
goto launch

:bad_arguments
echo [ERROR] Invalid launcher arguments.
echo Usage: launch_live2d.bat [desktop_pet^|live^|logs ALL^|LIVE2D^|CORE]
exit /b 2

:launch
call uv run --locked python -u "%LIVE2D_DIR%scripts\launch_desktop_pet.py" --config "%LIVE2D_DIR%config.toml" %LAUNCH_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [NachoBot Live2D Adapter] Exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
