@echo off
setlocal EnableExtensions
chcp 65001 >nul
title NachoBot Desktop Pet Adapter
set "PYTHONNOUSERSITE=1"
set "DESKTOP_PET_DIR=%~dp0"
set "DESKTOP_PET_CONFIG=%DESKTOP_PET_DIR%config.toml"

if not exist "%DESKTOP_PET_CONFIG%" (
    echo [ERROR] Desktop-pet configuration not found: %DESKTOP_PET_CONFIG%
    pause
    exit /b 1
)

set "LAUNCH_ARGS="
set "LOG_VIEW_ONLY="
if "%~1"=="" goto arguments_valid
if /I "%~1"=="desktop_pet" goto desktop_pet_mode
if /I "%~1"=="live" goto live_mode
if /I "%~1"=="logs" goto logs_mode
goto bad_arguments

:desktop_pet_mode
if not "%~2"=="" goto bad_arguments
set "LAUNCH_ARGS=--mode desktop_pet"
goto arguments_valid

:live_mode
if not "%~2"=="" goto bad_arguments
set "LAUNCH_ARGS=--mode live"
goto arguments_valid

:logs_mode
if /I "%~2"=="ALL" set "LAUNCH_ARGS=--show-logs ALL"
if /I "%~2"=="LIVE2D" set "LAUNCH_ARGS=--show-logs LIVE2D"
if /I "%~2"=="CORE" set "LAUNCH_ARGS=--show-logs CORE"
if "%LAUNCH_ARGS%"=="" goto bad_arguments
if not "%~3"=="" goto bad_arguments
set "LOG_VIEW_ONLY=1"
goto arguments_valid

:bad_arguments
echo [ERROR] Invalid launcher arguments.
echo Usage: launch_desktop_pet.bat [desktop_pet^|live^|logs ALL^|LIVE2D^|CORE]
exit /b 2

:arguments_valid
where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv is required to launch the desktop-pet adapter.
    echo Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run this file again.
    pause
    exit /b 1
)

cd /d "%DESKTOP_PET_DIR%"
echo [SYNC] NachoBot-Desktop-Pet-Adapter base dependencies ...
call uv sync --locked
if errorlevel 1 (
    echo [ERROR] Desktop-pet base dependency sync failed.
    pause
    exit /b 1
)

if "%LOG_VIEW_ONLY%"=="1" goto launch

set "SELECTED_BACKEND="
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$raw = & uv run --locked --no-sync python -m desktop_pet_adapter --print-launch-config --config $env:DESKTOP_PET_CONFIG; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; try { ($raw | ConvertFrom-Json -ErrorAction Stop).renderer_backend } catch { exit 1 }"`) do set "SELECTED_BACKEND=%%A"
if not defined SELECTED_BACKEND (
    echo [ERROR] Could not determine the configured renderer backend.
    pause
    exit /b 1
)

if /I "%SELECTED_BACKEND%"=="live2d" goto sync_live2d
goto launch

:sync_live2d
echo [SYNC] Optional Live2D renderer dependencies ...
call uv sync --project "%DESKTOP_PET_DIR%renderers\live2d" --locked
if errorlevel 1 (
    echo [ERROR] Optional Live2D dependency sync failed.
    pause
    exit /b 1
)

:launch
call uv run --locked --no-sync python -u "%DESKTOP_PET_DIR%scripts\launch_desktop_pet.py" --config "%DESKTOP_PET_CONFIG%" %LAUNCH_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [NachoBot Desktop Pet Adapter] Exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
