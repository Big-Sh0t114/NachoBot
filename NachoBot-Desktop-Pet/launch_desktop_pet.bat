@echo off
setlocal EnableExtensions
chcp 65001 >nul
title NachoBot Desktop Pet
set "PET_DIR=%~dp0"
set "PYTHONNOUSERSITE=1"

if not exist "%PET_DIR%launch_desktop_pet.ps1" (
    echo [ERROR] Desktop Pet launcher not found: %PET_DIR%launch_desktop_pet.ps1
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PET_DIR%launch_desktop_pet.ps1" -ConfigPath "%PET_DIR%config.toml"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [NachoBot Desktop Pet] Exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
