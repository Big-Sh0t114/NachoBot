@echo off
setlocal EnableExtensions
set "NACHOBOT_RENDERER_BACKEND=sprite"
call "%~dp0launch_desktop_pet.bat" desktop_pet
exit /b %ERRORLEVEL%
