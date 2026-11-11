@echo off
setlocal EnableExtensions
title NachoBot Independent Live2D
pushd "%~dp0"
call uv sync --locked
if errorlevel 1 goto :sync_failed
call uv run --no-sync python -m live2d_adapter %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

:sync_failed
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
