@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONPATH="
set "PYTHONHOME="
title Launch NachoBot Lite
set "FINAL_RC=0"
set "ROOT=%~dp0"
set "NACHOBOT_FFMPEG_DIR=%ROOT%.runtime\ffmpeg"

REM ===== Hugging Face mirror =====
if not defined HF_ENDPOINT (
  set "HF_ENDPOINT=https://hf-mirror.com"
)
echo [INFO] Hugging Face endpoint: %HF_ENDPOINT%

echo ===== Prepare Shared FFmpeg =====
call :ENSURE_FFMPEG
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] Shared FFmpeg preparation failed.
  goto :EXIT
)

echo.
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

:ENSURE_FFMPEG
setlocal EnableExtensions
set "NACHOBOT_DIR=%ROOT%NachoBot"

where uv >nul 2>&1
if errorlevel 1 (
  echo [INFO] uv not detected, installing...
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

if not exist "%NACHOBOT_DIR%\pyproject.toml" (
  echo [FATAL] NachoBot pyproject.toml not found: %NACHOBOT_DIR%
  endlocal & exit /b 1
)

if not exist "%ROOT%NachoBot\ensure_ffmpeg.py" (
  echo [FATAL] FFmpeg preparation script not found: %ROOT%NachoBot\ensure_ffmpeg.py
  endlocal & exit /b 1
)

echo [INFO] Syncing NachoBot dependencies for FFmpeg preparation...
cd /d "%NACHOBOT_DIR%"
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
  echo [FATAL] NachoBot dependency sync failed.
  endlocal & exit /b 1
)

echo [INFO] Checking shared FFmpeg binaries...
uv run python "%ROOT%NachoBot\ensure_ffmpeg.py"
if errorlevel 1 (
  echo [FATAL] Shared FFmpeg download or verification failed.
  endlocal & exit /b 1
)

endlocal & exit /b 0

:START_TTS
setlocal EnableDelayedExpansion
title TTS Launch
chcp 65001 >nul
set "TTS_RC=0"

set "DISABLE_VLM_ASR=1"

set "BASE_DIR=%ROOT%"
set "ADAPTER_DIR=%BASE_DIR%NachoBot-Multimodal-Adapter"
set "NAPCAT_DIR=%BASE_DIR%NachoBot-Napcat-Adapter"
set "NAPCAT_SRC=%NAPCAT_DIR%\src"
set "TTS_RUNTIME_MANAGER=%ADAPTER_DIR%\scripts\tts_runtime_manager.py"

set "PORT_SOVITS=9880"
set "PORT_VOX=9880"
set "PORT_ADAPTER=8070"
set "PORT_PERCEPTION=9874"

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

REM -- Read base.toml enabled_tts to decide which TTS engine to start --
set "TTS_ENGINE=GPT_Sovits"
set "BASE_TOML=%ADAPTER_DIR%\configs\base.toml"
for /f "usebackq tokens=*" %%L in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-Content '%BASE_TOML%' | Select-String 'enabled\s*=').Line"`) do (
  echo %%L | findstr /i "Vox" >nul
  if not errorlevel 1 (
    echo %%L | findstr /r /c:"\"Vox\".*\"GPT_Sovits\"" >nul
    if not errorlevel 1 (
      set "TTS_ENGINE=Vox"
    ) else (
      echo %%L | findstr /r /c:"\"Vox\"" >nul
      if not errorlevel 1 (
        echo %%L | findstr /i "GPT_Sovits" >nul
        if errorlevel 1 (
          set "TTS_ENGINE=Vox"
        )
      )
    )
  )
)
echo [INFO] Detected TTS engine: %TTS_ENGINE%

echo.
echo ========== Start TTS Backend ==========
echo.

if "%TTS_ENGINE%"=="Vox" goto :START_VOX

REM ---- GPT-SoVITS managed runtime ----
if not exist "%TTS_RUNTIME_MANAGER%" (
  echo [ERROR] TTS runtime manager not found: %TTS_RUNTIME_MANAGER%
  set "TTS_RC=1"
  goto :TTS_FAIL
)

echo [INFO] Starting managed GPT-SoVITS runtime...
start "SoVITS API (%PORT_SOVITS%)" /D "%ADAPTER_DIR%" cmd /k "chcp 65001>nul && uv run python scripts\tts_runtime_manager.py serve --engine gpt-sovits --port %PORT_SOVITS%"

set "READY="
for /l %%I in (1,1,180) do (
  netstat -ano | findstr /r /c:":%PORT_SOVITS% " | findstr /i LISTENING >nul
  if not errorlevel 1 (
    set "READY=1"
    goto :TTS_SOVITS_READY
  )
  timeout /t 1 /nobreak >nul
)
echo [ERROR] SoVITS timeout. If this is your first startup, wait for the model download to finish, then restart this service.
set "TTS_RC=1"
goto :TTS_FAIL

:TTS_SOVITS_READY
echo [OK] SoVITS ready.
goto :START_ADAPTER_SOVITS

REM ---- VoxCPM managed runtime ----
:START_VOX
if not exist "%TTS_RUNTIME_MANAGER%" (
  echo [ERROR] TTS runtime manager not found: %TTS_RUNTIME_MANAGER%
  set "TTS_RC=1"
  goto :TTS_FAIL
)

echo [INFO] Starting managed VoxCPM runtime...
start "VoxCPM API (%PORT_VOX%)" /D "%ADAPTER_DIR%" cmd /k "chcp 65001>nul && uv run python scripts\tts_runtime_manager.py serve --engine voxcpm --port %PORT_VOX%"

set "READY="
for /l %%I in (1,1,180) do (
  netstat -ano | findstr /r /c:":%PORT_VOX% " | findstr /i LISTENING >nul
  if not errorlevel 1 (
    set "READY=1"
    goto :TTS_VOX_READY
  )
  timeout /t 1 /nobreak >nul
)
echo [ERROR] VoxCPM timeout. If this is your first startup, wait for the model download to finish, then restart this service.
set "TTS_RC=1"
goto :TTS_FAIL

:TTS_VOX_READY
echo [OK] VoxCPM API ready.
goto :START_ADAPTER_VOX

REM ---- Adapter for GPT-SoVITS (Lite: TTS only, no Perception) ----
:START_ADAPTER_SOVITS
start "Multimodal Adapter (%PORT_ADAPTER%)" cmd /k "chcp 65001>nul && cd /d %ADAPTER_DIR% && set DISABLE_VLM_ASR=1 && uv run python main.py"

echo.
echo All modules started. (Lite mode, Perception skipped)
echo.
goto :TTS_END

REM ---- Adapter for VoxCPM (Lite: TTS only, no Perception) ----
:START_ADAPTER_VOX
start "Multimodal Adapter (%PORT_ADAPTER%)" cmd /k "chcp 65001>nul && cd /d %ADAPTER_DIR% && set DISABLE_VLM_ASR=1 && uv run python main.py"

echo.
echo All modules started. (Lite mode, Perception skipped)
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