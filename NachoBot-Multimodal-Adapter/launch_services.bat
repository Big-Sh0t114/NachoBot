@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
where uv >nul 2>&1
if errorlevel 1 exit /b 1
uv run --no-project --python 3.12 python -u scripts\service_launcher.py ensure
exit /b %ERRORLEVEL%
