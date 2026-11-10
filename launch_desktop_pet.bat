@echo off
setlocal EnableExtensions
set "DESKTOP_PET_LAUNCHER=%~dp0NachoBot-Desktop-Pet-Adapter\launch_desktop_pet.bat"
if not exist "%DESKTOP_PET_LAUNCHER%" (
    echo [ERROR] Desktop-pet project not found: %DESKTOP_PET_LAUNCHER%
    exit /b 1
)
call "%DESKTOP_PET_LAUNCHER%" %*
exit /b %ERRORLEVEL%
