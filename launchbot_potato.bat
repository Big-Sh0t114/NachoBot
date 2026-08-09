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
echo ===== Start 8070 Relay (No Local Models) =====
set "ADAPTER_DIR=%ROOT%NachoBot-Multimodal-Adapter"
set "PORT_ADAPTER=8070"
if not exist "%ADAPTER_DIR%\pyproject.toml" (
  set "FINAL_RC=1"
  echo [FATAL] Multimodal adapter pyproject.toml not found: %ADAPTER_DIR%
  goto :EXIT
)

echo [INFO] Syncing 8070 relay dependencies (no model service will be started)...
cd /d "%ADAPTER_DIR%"
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] 8070 relay dependency sync failed.
  goto :EXIT
)

echo [INFO] Starting 8070 pure message relay...
start "Multimodal Relay (%PORT_ADAPTER%)" /D "%ADAPTER_DIR%" cmd /k "chcp 65001>nul && set NACHOBOT_NO_LOCAL_MODELS=1 && set DISABLE_VLM_ASR=1 && uv run python main.py --no-local-models"

set "RELAY_READY="
for /l %%I in (1,1,60) do (
  if not defined RELAY_READY (
    curl.exe -fsS --max-time 1 "http://127.0.0.1:%PORT_ADAPTER%/api/health" | findstr /c:"relay_only" >nul
    if not errorlevel 1 (
      set "RELAY_READY=1"
      echo [OK] 8070 relay is listening. TTS, VLM, ASR, and other local model loading are disabled.
    ) else (
      timeout /t 1 /nobreak >nul
    )
  )
)
if not defined RELAY_READY (
  set "FINAL_RC=1"
  echo [FATAL] 8070 relay did not start within 60 seconds.
  goto :EXIT
)

echo.
echo ===== Start Main Bot Component =====
title Launch Process
set "NACHOBOT_MAIN=bot.py"
set "NACHOBOT_PORT=8000"
set "NAPCAT_DIR=%ROOT%NachoBot-Napcat-Adapter"
set "NAPCAT_MAIN=main.py"
set "NAPCAT_PORT=8095"
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
echo Startup complete. 8070 is running in pure relay mode; no local model service was started.

:EXIT
if %FINAL_RC% NEQ 0 (
  echo Error occurred.
  pause
) else (
  echo All done.
)
endlocal & exit /b %FINAL_RC%
