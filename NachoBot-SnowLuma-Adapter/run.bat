@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo [FATAL] uv is required to run the SnowLuma adapter.
  pause
  endlocal & exit /b 1
)

echo [INFO] Selecting optional QQCall audio dependency...
set "QQCALL_EXTRA=none"
for /f "usebackq delims=" %%E in (`uv run --no-project --python ">=3.11,<=3.13" python "%~dp0..\NachoBot-QQCall\scripts\adapter_extra.py" --adapter snowluma`) do if "%%E"=="qqcall-audio" set "QQCALL_EXTRA=qqcall-audio"
if /i "%QQCALL_EXTRA%"=="qqcall-audio" (
  echo [INFO] Syncing SnowLuma adapter with QQCall audio support...
  uv sync --extra qqcall-audio --python ">=3.11,<=3.13"
) else (
  echo [INFO] Syncing SnowLuma adapter dependencies...
  uv sync --python ">=3.11,<=3.13"
)
if errorlevel 1 (
  echo [FATAL] SnowLuma adapter dependency sync failed.
  pause
  endlocal & exit /b 1
)

uv run --no-sync python main.py
set "RUN_RC=%ERRORLEVEL%"
pause
endlocal & exit /b %RUN_RC%
