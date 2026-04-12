@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONPATH="
set "PYTHONHOME="
title Launch NachoBot Lite
set "FINAL_RC=0"
set "ROOT=%~dp0"

echo ===== Start TTS Component =====
call :START_TTS
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] TTS component failed to start.
  goto :EXIT
)

echo.
echo ===== Start Main Bot Component =====
call :START_MAIN
set "FINAL_RC=%ERRORLEVEL%"
goto :EXIT

:START_TTS
setlocal EnableDelayedExpansion
title TTS Launch
chcp 65001 >nul
set "TTS_RC=0"

set "DISABLE_VLM_ASR=1"

set "BASE_DIR=%ROOT%"
set "ADAPTER_DIR=%BASE_DIR%NachoBot-TTS-Adapter"
set "NAPCAT_DIR=%BASE_DIR%NachoBot-Napcat-Adapter"
set "NAPCAT_SRC=%NAPCAT_DIR%\src"
set "SOVITS_DIR=C:\Users\BigSh0t\GPT-SoVITS\GPT-SoVITS-v2pro-20250604"

set "PORT_SOVITS=9880"
set "PORT_ADAPTER=8070"
set "PORT_CONTROL=9872"

set "PY_GPT=%SOVITS_DIR%\runtime\python.exe"
set "PY_ADAPTER=%ADAPTER_DIR%\.venv\Scripts\python.exe"

set "PYTHONNOUSERSITE=1"
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "NO_PROXY=127.0.0.1,localhost"

set "LOG_DIR=%ADAPTER_DIR%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "SETUP_LOG=%LOG_DIR%\boot_setup.log"
echo ==== RUN %date% %time% ==== >> "%SETUP_LOG%"

where uv >nul 2>&1
if errorlevel 1 (
  echo [INFO] uv not detected, installing... >> "%SETUP_LOG%"
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

echo [INFO] Syncing dependencies (Locking Python 3.11~3.13)... >> "%SETUP_LOG%"
cd /d "%ADAPTER_DIR%"
uv sync --python ">=3.11,<=3.13" >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
  echo [FATAL] uv sync failed. Please check Python installation. >> "%SETUP_LOG%"
  echo [FATAL] uv sync failed.
  set "TTS_RC=1"
  goto :TTS_FAIL
)

set "API_FILE=%SOVITS_DIR%\api_v2.py"
if not exist "%API_FILE%" set "API_FILE=%SOVITS_DIR%\api.py"

REM ── 从 gpt-sovits.toml 读取 TTS 目标显卡 ──
set "TTS_GPU_ID=0"
set "TTS_TOML=%ADAPTER_DIR%\configs\gpt-sovits.toml"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ADAPTER_DIR%\get_gpu_id.ps1" -TomlPath "%TTS_TOML%" > "%TEMP%\_gpu_id.txt" 2>nul
set /p TTS_GPU_ID=<"%TEMP%\_gpu_id.txt"
del "%TEMP%\_gpu_id.txt" 2>nul
echo [INFO] TTS (SoVITS) will use GPU: %TTS_GPU_ID%

echo.
echo ========== Start SoVITS / Adapter / Control ==========
echo.

start "SoVITS API (%PORT_SOVITS%)" cmd /k "chcp 65001>nul && set CUDA_VISIBLE_DEVICES=%TTS_GPU_ID% && set PYTHONPATH=%SOVITS_DIR%;%SOVITS_DIR%\GPT_SoVITS && cd /d %SOVITS_DIR% && %PY_GPT% -s %API_FILE% --port %PORT_SOVITS%"

set "READY="
for /l %%I in (1,1,60) do (
  netstat -ano | findstr /r /c:":%PORT_SOVITS% " | findstr /i LISTENING >nul
  if not errorlevel 1 (
    set "READY=1"
    goto :TTS_SOVITS_READY
  )
  timeout /t 1 /nobreak >nul
)
echo [ERROR] SoVITS timeout.
set "TTS_RC=1"
goto :TTS_FAIL

:TTS_SOVITS_READY
echo [OK] SoVITS ready.

start "TTS Adapter (%PORT_ADAPTER%)" cmd /k "chcp 65001>nul && cd /d %ADAPTER_DIR% && uv run python main.py"

echo [OK] Starting Control...
start "Control API (%PORT_CONTROL%)" cmd /k "chcp 65001>nul && cd /d %ADAPTER_DIR% && uv run python -m tts_src.plugins.GPT_Sovits.api_server"

echo.
echo All modules started.
echo.
goto :TTS_END

:TTS_FAIL
echo Error during initialization.
pause
set "TTS_RC=1"

:TTS_END
endlocal & exit /b %TTS_RC%

:START_MAIN
setlocal EnableExtensions
title Launch Process
chcp 65001 >nul

set "NACHOBOT_DIR=%ROOT%NachoBot"
set "NACHOBOT_MAIN=bot.py"
set "NACHOBOT_PORT=8000"

set "ADAPTER_DIR=%ROOT%NachoBot-Napcat-Adapter"
set "ADAPTER_MAIN=main.py"
set "ADAPTER_PORT=8095"

set "NAPCAT_SHELL_DIR=%ROOT%NapCat.Shell"
set "NAPCAT_SHELL_BAT=launcher-user.bat"

set "PYTHON_CMD=uv run python"
set "MAX_WAIT=60"

echo --- Syncing NachoBot...
cd /d "%NACHOBOT_DIR%"
uv sync --python ">=3.11,<=3.13"

echo --- Syncing Adapter...
cd /d "%ADAPTER_DIR%"
uv sync --python ">=3.11,<=3.13"

if exist "%NACHOBOT_DIR%\%NACHOBOT_MAIN%" (
  echo --- Start NachoBot...
  start "NachoBot" /D "%NACHOBOT_DIR%" cmd /k "set HOST=127.0.0.1 && set PORT=%NACHOBOT_PORT% && %PYTHON_CMD% %NACHOBOT_MAIN%"
  timeout /t 5 /nobreak >nul
)

if exist "%ADAPTER_DIR%\%ADAPTER_MAIN%" (
  echo --- Start Adapter...
  start "NachoBot-Napcat" /D "%ADAPTER_DIR%" cmd /k "set HOST=0.0.0.0 && set PORT=%ADAPTER_PORT% && %PYTHON_CMD% %ADAPTER_MAIN%"
  timeout /t 5 /nobreak >nul
)

if exist "%NAPCAT_SHELL_DIR%\%NAPCAT_SHELL_BAT%" (
  echo --- Start NapCat Shell...
  start "NapCatShell" /D "%NAPCAT_SHELL_DIR%" cmd /k "%NAPCAT_SHELL_BAT%"
)

echo.
echo Startup complete.
endlocal & exit /b 0

:EXIT
if %FINAL_RC% NEQ 0 (
  echo Error occurred.
  pause
) else (
  echo All done.
)
endlocal & exit /b %FINAL_RC%
