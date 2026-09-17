@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo [FATAL] uv is required to run the SnowLuma adapter.
  pause
  endlocal & exit /b 1
)

echo [INFO] Syncing SnowLuma adapter dependencies...
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
  echo [FATAL] SnowLuma adapter dependency sync failed.
  pause
  endlocal & exit /b 1
)

uv run --no-sync python main.py
set "RUN_RC=%ERRORLEVEL%"
pause
endlocal & exit /b %RUN_RC%
