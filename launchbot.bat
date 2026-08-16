@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONPATH="
set "PYTHONHOME="
title Launch TTS + NachoBot
set "FINAL_RC=0"
set "ROOT=%~dp0"
set "NACHOBOT_FFMPEG_DIR=%ROOT%.runtime\ffmpeg"

REM ===== Hugging Face endpoint =====
if not defined NACHOBOT_HF_ENDPOINT (
  set "NACHOBOT_HF_ENDPOINT=https://hf-mirror.com"
)
echo [INFO] NachoBot Hugging Face endpoint: %NACHOBOT_HF_ENDPOINT%

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

set "BASE_DIR=%ROOT%"
set "ADAPTER_DIR=%BASE_DIR%NachoBot-Multimodal-Adapter"
set "NAPCAT_DIR=%BASE_DIR%NachoBot-Napcat-Adapter"
set "NAPCAT_SRC=%NAPCAT_DIR%\src"
set "TTS_RUNTIME_MANAGER=%ADAPTER_DIR%\scripts\tts_runtime_manager.py"
set "BASE_TOML=%ADAPTER_DIR%\configs\base.toml"

set "PORT_SOVITS=9880"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%ADAPTER_DIR%\configs\gpt-sovits.toml'; if (Test-Path $p) { $c=Get-Content -Raw $p; if ($c -match '(?ms)^\[tts\]\s*.*?^port\s*=\s*(\d+)') { $Matches[1] } else { '9880' } } else { '9880' }"`) do set "PORT_SOVITS=%%P"
set "PORT_VOX=9880"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%ADAPTER_DIR%\configs\vox.toml'; if (Test-Path $p) { $c=Get-Content -Raw $p; if ($c -match '(?ms)^\[tts\]\s*.*?^port\s*=\s*(\d+)') { $Matches[1] } else { '9880' } } else { '9880' }"`) do set "PORT_VOX=%%P"
set "PORT_ADAPTER=8070"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-Content -Raw '%BASE_TOML%'; if ($c -match '(?ms)^\[server\]\s*.*?^port\s*=\s*(\d+)') { $Matches[1] } else { '8070' }"`) do set "PORT_ADAPTER=%%P"
set "PORT_PERCEPTION=9874"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%ADAPTER_DIR%\configs\perception.toml'; if (Test-Path $p) { $c=Get-Content -Raw $p; if ($c -match '(?ms)^\[perception\]\s*.*?^port\s*=\s*(\d+)') { $Matches[1] } else { '9874' } } else { '9874' }"`) do set "PORT_PERCEPTION=%%P"

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

REM ---- Adapter for GPT-SoVITS ----
:START_ADAPTER_SOVITS
start "Multimodal Adapter (%PORT_ADAPTER%)" cmd /k "chcp 65001>nul && cd /d %ADAPTER_DIR% && uv run python main.py"
call :WAIT_ADAPTER_READY
if errorlevel 1 (
  set "TTS_RC=1"
  goto :TTS_FAIL
)

echo [OK] Starting Perception API (VLM + ASR)...
start "Perception API (%PORT_PERCEPTION%)" cmd /k "chcp 65001>nul && cd /d %ADAPTER_DIR% && uv run python -m nachobot_multimodal.api_server"

echo.
echo All modules started.
echo.
goto :TTS_END

REM ---- Adapter for VoxCPM ----
:START_ADAPTER_VOX
start "Multimodal Adapter (%PORT_ADAPTER%)" cmd /k "chcp 65001>nul && cd /d %ADAPTER_DIR% && uv run python main.py"
call :WAIT_ADAPTER_READY
if errorlevel 1 (
  set "TTS_RC=1"
  goto :TTS_FAIL
)

echo [OK] Starting Perception API (VLM + ASR)...
start "Perception API (%PORT_PERCEPTION%)" cmd /k "chcp 65001>nul && cd /d %ADAPTER_DIR% && uv run python -m nachobot_multimodal.api_server"

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

:WAIT_ADAPTER_READY
set "ADAPTER_READY="
for /l %%I in (1,1,60) do (
  if not defined ADAPTER_READY (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -UseBasicParsing -TimeoutSec 1 'http://127.0.0.1:%PORT_ADAPTER%/api/health'; if ($r.status -eq 'ok' -and $r.mode -eq 'tts') { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
    if not errorlevel 1 (
      set "ADAPTER_READY=1"
      echo [OK] Multimodal relay :%PORT_ADAPTER% is ready in TTS mode.
    ) else (
      timeout /t 1 /nobreak >nul
    )
  )
)
if not defined ADAPTER_READY (
  echo [ERROR] Multimodal relay :%PORT_ADAPTER% did not become ready in TTS mode within 60 seconds.
  exit /b 1
)
exit /b 0

:START_MAIN
setlocal EnableExtensions
title Launch Process
chcp 65001 >nul

set "NACHOBOT_DIR=%ROOT%NachoBot"
set "NACHOBOT_MAIN=bot.py"
set "NACHOBOT_PORT=8000"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%NACHOBOT_DIR%\.env'; if (Test-Path $p) { $m=Get-Content $p | Where-Object { $_ -match '^\s*PORT\s*=\s*(\d+)\s*$' } | Select-Object -First 1; if ($m -and $m -match '^\s*PORT\s*=\s*(\d+)\s*$') { $Matches[1] } else { '8000' } } else { '8000' }"`) do set "NACHOBOT_PORT=%%P"

set "ADAPTER_DIR=%ROOT%NachoBot-Napcat-Adapter"
set "ADAPTER_MAIN=main.py"
set "ADAPTER_PORT=8095"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%ADAPTER_DIR%\config.toml'; if (Test-Path $p) { $c=Get-Content -Raw $p; if ($c -match '(?ms)^\[napcat_server\]\s*.*?^port\s*=\s*(\d+)') { $Matches[1] } else { '8095' } } else { '8095' }"`) do set "ADAPTER_PORT=%%P"

set "NAPCAT_SHELL_DIR=%ROOT%NapCat.Shell"
set "NAPCAT_SHELL_BAT=launcher-user.bat"

set "PYTHON_CMD=uv run python"
set "MAX_WAIT=60"

echo --- Syncing NachoBot...
cd /d "%NACHOBOT_DIR%"
uv sync --python ">=3.11,<=3.13"

echo --- Checking Playwright Chromium...
uv run python scripts\ensure_playwright.py
if errorlevel 1 echo [WARN] Playwright Chromium preparation failed; web search will use HTTP fallback.

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
