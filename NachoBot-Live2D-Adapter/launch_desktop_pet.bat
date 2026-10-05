@echo off
setlocal EnableExtensions
chcp 65001 >nul
title NachoBot Desktop Pet

call "%~dp0launch_live2d.bat" desktop_pet
exit /b %ERRORLEVEL%
