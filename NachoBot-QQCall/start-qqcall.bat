@echo off
setlocal
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\start-qqcall.ps1"
if errorlevel 1 pause
