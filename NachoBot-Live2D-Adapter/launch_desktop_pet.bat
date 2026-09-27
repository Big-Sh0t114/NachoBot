@echo off
setlocal EnableExtensions
chcp 65001 >nul
title NachoBot Desktop Pet

call "%~dp0..\NachoBot-Desktop-Pet\launch_desktop_pet.bat"
exit /b %ERRORLEVEL%
