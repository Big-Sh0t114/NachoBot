@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONPATH="
set "PYTHONHOME="
title Launch NachoBot Potato (No Local Models)
set "FINAL_RC=0"
set "ROOT=%~dp0"
set "NACHOBOT_FFMPEG_DIR=%ROOT%.runtime\ffmpeg"

where uv >nul 2>&1
if errorlevel 1 (
  echo [INFO] uv not detected, installing...
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)
where uv >nul 2>&1
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] uv is not available. Install uv and try again.
  goto :EXIT
)

echo ===== Prepare Shared FFmpeg =====
set "NACHOBOT_DIR=%ROOT%NachoBot"
if not exist "%NACHOBOT_DIR%\pyproject.toml" (
  set "FINAL_RC=1"
  echo [FATAL] NachoBot pyproject.toml not found: %NACHOBOT_DIR%
  goto :EXIT
)
if not exist "%ROOT%NachoBot\ensure_ffmpeg.py" (
  set "FINAL_RC=1"
  echo [FATAL] FFmpeg preparation script not found: %ROOT%NachoBot\ensure_ffmpeg.py
  goto :EXIT
)

echo [INFO] Syncing NachoBot dependencies for FFmpeg preparation...
cd /d "%NACHOBOT_DIR%"
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] NachoBot dependency sync failed.
  goto :EXIT
)

echo [INFO] Checking shared FFmpeg binaries...
uv run python "%ROOT%NachoBot\ensure_ffmpeg.py"
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] Shared FFmpeg download or verification failed.
  goto :EXIT
)

echo.
echo ===== Start Relay (No Local Models) =====
set "ADAPTER_DIR=%ROOT%NachoBot-Multimodal-Adapter"
set "BASE_TOML=%ADAPTER_DIR%\configs\base.toml"
set "PORT_ADAPTER=8070"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-Content -Raw '%BASE_TOML%'; if ($c -match '(?ms)^\[server\]\s*.*?^port\s*=\s*(\d+)') { $Matches[1] } else { '8070' }"`) do set "PORT_ADAPTER=%%P"
if not exist "%ADAPTER_DIR%\pyproject.toml" (
  set "FINAL_RC=1"
  echo [FATAL] Multimodal adapter pyproject.toml not found: %ADAPTER_DIR%
  goto :EXIT
)

echo [INFO] Syncing relay dependencies for port %PORT_ADAPTER% (no model service will be started)...
cd /d "%ADAPTER_DIR%"
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] Relay dependency sync failed for port %PORT_ADAPTER%.
  goto :EXIT
)

echo [INFO] Starting pure message relay on port %PORT_ADAPTER%...
start "Multimodal Relay (%PORT_ADAPTER%)" /D "%ADAPTER_DIR%" cmd /k "chcp 65001>nul && set NACHOBOT_NO_LOCAL_MODELS=1 && set DISABLE_VLM_ASR=1 && uv run python main.py --no-local-models"

set "RELAY_READY="
for /l %%I in (1,1,60) do (
  if not defined RELAY_READY (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -UseBasicParsing -TimeoutSec 1 'http://127.0.0.1:%PORT_ADAPTER%/api/health'; if ($r.status -eq 'ok' -and $r.mode -eq 'relay_only') { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
    if not errorlevel 1 (
      set "RELAY_READY=1"
      echo [OK] Relay :%PORT_ADAPTER% is listening. TTS, VLM, ASR, and other local model loading are disabled.
    ) else (
      timeout /t 1 /nobreak >nul
    )
  )
)
if not defined RELAY_READY (
  set "FINAL_RC=1"
  echo [FATAL] Relay :%PORT_ADAPTER% did not start within 60 seconds.
  goto :EXIT
)

echo.
echo ===== Start Main Bot Component =====
title Launch Process
set "NACHOBOT_MAIN=bot.py"
set "NACHOBOT_PORT=8000"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%NACHOBOT_DIR%\.env'; if (Test-Path $p) { $m=Get-Content $p | Where-Object { $_ -match '^\s*PORT\s*=\s*(\d+)\s*$' } | Select-Object -First 1; if ($m -and $m -match '^\s*PORT\s*=\s*(\d+)\s*$') { $Matches[1] } else { '8000' } } else { '8000' }"`) do set "NACHOBOT_PORT=%%P"
set "NAPCAT_DIR=%ROOT%NachoBot-Napcat-Adapter"
set "NAPCAT_MAIN=main.py"
set "NAPCAT_PORT=8095"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%NAPCAT_DIR%\config.toml'; if (Test-Path $p) { $c=Get-Content -Raw $p; if ($c -match '(?ms)^\[napcat_server\]\s*.*?^port\s*=\s*(\d+)') { $Matches[1] } else { '8095' } } else { '8095' }"`) do set "NAPCAT_PORT=%%P"
set "NAPCAT_SHELL_DIR=%ROOT%NapCat.Shell"
set "NAPCAT_SHELL_BAT=launcher-user.bat"
set "PYTHON_CMD=uv run python"

echo --- Syncing NachoBot...
cd /d "%NACHOBOT_DIR%"
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] NachoBot dependency sync failed.
  goto :EXIT
)

echo --- Checking Playwright Chromium...
uv run python scripts\ensure_playwright.py
if errorlevel 1 echo [WARN] Playwright Chromium preparation failed; web search will use HTTP fallback.

echo --- Syncing NapCat Adapter...
cd /d "%NAPCAT_DIR%"
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] NapCat adapter dependency sync failed.
  goto :EXIT
)

if exist "%NACHOBOT_DIR%\%NACHOBOT_MAIN%" (
  echo --- Start NachoBot...
  start "NachoBot" /D "%NACHOBOT_DIR%" cmd /k "set HOST=127.0.0.1 && set PORT=%NACHOBOT_PORT% && %PYTHON_CMD% %NACHOBOT_MAIN%"
  timeout /t 5 /nobreak >nul
)

if exist "%NAPCAT_DIR%\%NAPCAT_MAIN%" (
  echo --- Start NapCat Adapter...
  start "NachoBot-Napcat" /D "%NAPCAT_DIR%" cmd /k "set HOST=0.0.0.0 && set PORT=%NAPCAT_PORT% && %PYTHON_CMD% %NAPCAT_MAIN%"
  timeout /t 5 /nobreak >nul
)

if exist "%NAPCAT_SHELL_DIR%\%NAPCAT_SHELL_BAT%" (
  echo --- Start NapCat Shell...
  start "NapCatShell" /D "%NAPCAT_SHELL_DIR%" cmd /k "%NAPCAT_SHELL_BAT%"
)

echo.
echo Startup complete. Relay :%PORT_ADAPTER% is running in pure relay mode; no local model service was started.

:EXIT
if %FINAL_RC% NEQ 0 (
  echo Error occurred.
  pause
) else (
  echo All done.
)
endlocal & exit /b %FINAL_RC%
