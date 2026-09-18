@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONPATH="
set "PYTHONHOME="
title Launch TTS + NachoBot
set "FINAL_RC=0"
set "ROOT=%~dp0"
set "NACHOBOT_FFMPEG_DIR=%ROOT%.runtime\ffmpeg"

call :READ_QQ_ADAPTER
if errorlevel 1 (
  echo [FATAL] Invalid qq_adapter in NachoBot\.env. Use napcat or snowluma; a missing key defaults to napcat.
  set "FINAL_RC=1"
  goto :EXIT
)
echo [INFO] QQ adapter: %QQ_ADAPTER%

REM ===== Hugging Face endpoint =====
if not defined NACHOBOT_HF_ENDPOINT (
  set "NACHOBOT_HF_ENDPOINT=https://hf-mirror.com"
)
echo [INFO] NachoBot Hugging Face endpoint: %NACHOBOT_HF_ENDPOINT%

echo ===== Check Git =====
where git >nul 2>&1
if errorlevel 1 (
  echo [INFO] Git not detected. Checking winget...
  winget --version >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] winget is not available. Git must be installed manually.
    echo [INFO] Git download: https://git-scm.com/download/win
    set "FINAL_RC=1"
    goto :EXIT
  )

  echo [INFO] Installing Git with winget...
  winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --silent
  if errorlevel 1 (
    echo [ERROR] Git installation via winget failed.
    echo [INFO] Git download: https://git-scm.com/download/win
    set "FINAL_RC=1"
    goto :EXIT
  )

  REM winget updates the persistent PATH, but this launcher must refresh it for the current process.
  set "PATH=%ProgramFiles%\Git\cmd;%LOCALAPPDATA%\Programs\Git\cmd;%PATH%"
  where git >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Git was installed, but git.exe is not available in the current launcher process.
    echo [INFO] Git download: https://git-scm.com/download/win
    echo [INFO] Restart this launcher and try again.
    set "FINAL_RC=1"
    goto :EXIT
  )
)
for /f "delims=" %%G in ('git --version 2^>nul') do echo [INFO] %%G

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

REM -- .bat-only runtime selection. WebUI does not read [bat_runtime]. --
set "BAT_RUNTIME=gpu"
for /f "usebackq delims=" %%R in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%BASE_TOML%'; if (Test-Path $p) { $c=Get-Content -Raw $p; if ($c -match '(?ms)^\[bat_runtime\]\s*.*?^full\s*=\s*\x22([^\x22]+)\x22') { $Matches[1] } else { 'gpu' } } else { 'gpu' }"`) do set "BAT_RUNTIME=%%R"
if /i "!BAT_RUNTIME!"=="cpu" (
  set "ADAPTER_ENV_DIR=%ADAPTER_DIR%\.venv-cpu"
  set "TTS_TORCH_INDEX=https://download.pytorch.org/whl/cpu"
) else if /i "!BAT_RUNTIME!"=="gpu" (
  set "ADAPTER_ENV_DIR=%ADAPTER_DIR%\.venv"
  set "TTS_TORCH_INDEX=https://download.pytorch.org/whl/cu128"
) else (
  echo [FATAL] Invalid [bat_runtime].full value: !BAT_RUNTIME! ^(expected gpu or cpu^)
  set "TTS_RC=1"
  goto :TTS_FAIL
)
set "ADAPTER_PYTHON=!ADAPTER_ENV_DIR!\Scripts\python.exe"
set "NACHOBOT_TTS_RUNTIME_PROFILE=!BAT_RUNTIME!"
set "NACHOBOT_TTS_TORCH_INDEX=!TTS_TORCH_INDEX!"
echo [INFO] FULL .bat runtime: !BAT_RUNTIME! ^(!ADAPTER_ENV_DIR!^)

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

echo [INFO] Syncing !BAT_RUNTIME! dependencies (Locking Python 3.11~3.13)... >> "%SETUP_LOG%"
cd /d "%ADAPTER_DIR%"
set "SYNC_RC=0"
set "UV_PROJECT_ENVIRONMENT=!ADAPTER_ENV_DIR!"
if /i "!BAT_RUNTIME!"=="cpu" (
  if not exist "%ADAPTER_DIR%\pyproject.toml.cpu" (
    echo [FATAL] CPU runtime spec not found: %ADAPTER_DIR%\pyproject.toml.cpu >> "%SETUP_LOG%"
    set "TTS_RC=1"
    set "UV_PROJECT_ENVIRONMENT="
    goto :TTS_FAIL
  )
  set "CPU_PROJECT=%ADAPTER_DIR%\.runtime\bat-cpu"
  if not exist "!CPU_PROJECT!" mkdir "!CPU_PROJECT!"
  copy /y "%ADAPTER_DIR%\pyproject.toml.cpu" "!CPU_PROJECT!\pyproject.toml" >nul
  uv sync --project "!CPU_PROJECT!" --python ">=3.11,<3.13" --no-install-project >> "%SETUP_LOG%" 2>&1
  set "SYNC_RC=!ERRORLEVEL!"
) else (
  uv sync --project "%ADAPTER_DIR%" --python ">=3.11,<3.13" >> "%SETUP_LOG%" 2>&1
  set "SYNC_RC=!ERRORLEVEL!"
)
set "UV_PROJECT_ENVIRONMENT="
if not "!SYNC_RC!"=="0" (
  echo [FATAL] !BAT_RUNTIME! runtime uv sync failed. >> "%SETUP_LOG%"
  echo [FATAL] !BAT_RUNTIME! runtime uv sync failed.
  set "TTS_RC=1"
  goto :TTS_FAIL
)
if not exist "!ADAPTER_PYTHON!" (
  echo [FATAL] Runtime Python not found: !ADAPTER_PYTHON! >> "%SETUP_LOG%"
  echo [FATAL] Runtime Python not found: !ADAPTER_PYTHON!
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
start "SoVITS API (%PORT_SOVITS%)" /D "%ADAPTER_DIR%" cmd /k ""!ADAPTER_PYTHON!" scripts\tts_runtime_manager.py serve --engine gpt-sovits --port %PORT_SOVITS%"

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
start "VoxCPM API (%PORT_VOX%)" /D "%ADAPTER_DIR%" cmd /k ""!ADAPTER_PYTHON!" scripts\tts_runtime_manager.py serve --engine voxcpm --port %PORT_VOX%"

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
start "Multimodal Adapter (%PORT_ADAPTER%)" /D "%ADAPTER_DIR%" cmd /k ""!ADAPTER_PYTHON!" main.py"
call :WAIT_ADAPTER_READY
if errorlevel 1 (
  set "TTS_RC=1"
  goto :TTS_FAIL
)

echo [OK] Starting Perception API (VLM + ASR)...
start "Perception API (%PORT_PERCEPTION%)" /D "%ADAPTER_DIR%" cmd /k ""!ADAPTER_PYTHON!" -m nachobot_multimodal.api_server"

echo.
echo All modules started.
echo.
goto :TTS_END

REM ---- Adapter for VoxCPM ----
:START_ADAPTER_VOX
start "Multimodal Adapter (%PORT_ADAPTER%)" /D "%ADAPTER_DIR%" cmd /k ""!ADAPTER_PYTHON!" main.py"
call :WAIT_ADAPTER_READY
if errorlevel 1 (
  set "TTS_RC=1"
  goto :TTS_FAIL
)

echo [OK] Starting Perception API (VLM + ASR)...
start "Perception API (%PORT_PERCEPTION%)" /D "%ADAPTER_DIR%" cmd /k ""!ADAPTER_PYTHON!" -m nachobot_multimodal.api_server"

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

set "ADAPTER_MAIN=main.py"
if /i "%QQ_ADAPTER%"=="snowluma" (
  set "ADAPTER_DIR=%ROOT%NachoBot-SnowLuma-Adapter"
) else (
  set "ADAPTER_DIR=%ROOT%NachoBot-Napcat-Adapter"
  set "ADAPTER_PORT=8095"
  for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%ROOT%NachoBot-Napcat-Adapter\config.toml'; if (Test-Path $p) { $c=Get-Content -Raw $p; if ($c -match '(?ms)^\[napcat_server\]\s*.*?^port\s*=\s*(\d+)') { $Matches[1] } else { '8095' } } else { '8095' }"`) do set "ADAPTER_PORT=%%P"
)

set "NAPCAT_SHELL_DIR=%ROOT%NapCat.Shell"
set "NAPCAT_SHELL_BAT=launcher-user.bat"

set "PYTHON_CMD=uv run python"
set "MAX_WAIT=60"

if /i "%QQ_ADAPTER%"=="snowluma" (
  call :VERIFY_SNOWLUMA_COMPONENTS
  if errorlevel 1 (
    endlocal & exit /b 1
  )
)

echo --- Syncing NachoBot...
cd /d "%NACHOBOT_DIR%"
uv sync --python ">=3.11,<=3.13"

echo --- Checking Playwright Chromium...
uv run python scripts\ensure_playwright.py
if errorlevel 1 echo [WARN] Playwright Chromium preparation failed; web search will use HTTP fallback.

echo --- Syncing Adapter...
if not exist "%ADAPTER_DIR%\." (
  echo [FATAL] %QQ_ADAPTER% adapter setup failed: project directory not found.
  endlocal & exit /b 1
)
if not exist "%ADAPTER_DIR%\pyproject.toml" (
  echo [FATAL] %QQ_ADAPTER% adapter setup failed: pyproject.toml not found.
  endlocal & exit /b 1
)
cd /d "%ADAPTER_DIR%"
if errorlevel 1 (
  echo [FATAL] %QQ_ADAPTER% adapter setup failed: project directory could not be entered.
  endlocal & exit /b 1
)
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
  echo [FATAL] %QQ_ADAPTER% adapter dependency sync failed.
  endlocal & exit /b 1
)

if /i "%QQ_ADAPTER%"=="snowluma" (
  call :START_SNOWLUMA_RUNTIME
  if errorlevel 1 (
    echo [FATAL] SnowLuma Runtime did not become ready; adapter startup aborted.
    endlocal & exit /b 1
  )
)

if exist "%NACHOBOT_DIR%\%NACHOBOT_MAIN%" (
  echo --- Start NachoBot...
  start "NachoBot" /D "%NACHOBOT_DIR%" cmd /k "set HOST=127.0.0.1 && set PORT=%NACHOBOT_PORT% && %PYTHON_CMD% %NACHOBOT_MAIN%"
  timeout /t 5 /nobreak >nul
)

if exist "%ADAPTER_DIR%\%ADAPTER_MAIN%" (
  echo --- Start %QQ_ADAPTER% Adapter...
  if /i "%QQ_ADAPTER%"=="snowluma" (
    start "NachoBot-SnowLuma" /D "%ADAPTER_DIR%" cmd /k "%PYTHON_CMD% %ADAPTER_MAIN%"
  ) else (
    start "NachoBot-Napcat" /D "%ADAPTER_DIR%" cmd /k "set HOST=0.0.0.0 && set PORT=%ADAPTER_PORT% && %PYTHON_CMD% %ADAPTER_MAIN%"
  )
  timeout /t 5 /nobreak >nul
)

if /i "%QQ_ADAPTER%"=="napcat" if exist "%NAPCAT_SHELL_DIR%\%NAPCAT_SHELL_BAT%" (
  echo --- Start NapCat Shell...
  start "NapCatShell" /D "%NAPCAT_SHELL_DIR%" cmd /k "%NAPCAT_SHELL_BAT%"
)

echo.
echo Startup complete.
endlocal & exit /b 0

:VERIFY_SNOWLUMA_COMPONENTS
setlocal EnableExtensions EnableDelayedExpansion
set "SNOWLUMA_DIR="
for /f "usebackq delims=" %%D in (`uv run --project "%ROOT%NachoBot" python "%ROOT%webUI\snowluma_locator.py" --root "%ROOT%" --field path 2^>nul`) do if not defined SNOWLUMA_DIR set "SNOWLUMA_DIR=%%D"
if not defined SNOWLUMA_DIR (
  echo [FATAL] SnowLuma Runtime discovery failed; deploy exactly one valid SnowLuma 1.14.x directory.
  echo [INFO] SnowLuma download: https://github.com/SnowLuma/SnowLuma/releases/latest
  endlocal & exit /b 1
)
set "SNOWLUMA_DIR=%SNOWLUMA_DIR%"
for %%D in ("%SNOWLUMA_DIR%") do set "SNOWLUMA_NAME=%%~nxD"
set "SNOWLUMA_ADAPTER_DIR=%ROOT%NachoBot-SnowLuma-Adapter"
set "SNOWLUMA_MISSING="
for %%F in (package.json index.mjs utils-tSVKpzEf.js logger-BAozzyTt.js config-GJCFWjtq.js server-CLw7fwOG.js launcher.bat client\index.html native\snowluma-win32-x64.dll native\snowluma-win32-x64.node native\websocket-win32-x64.node) do (
  if not exist "!SNOWLUMA_DIR!\%%F" set "SNOWLUMA_MISSING=!SNOWLUMA_MISSING! !SNOWLUMA_NAME!/%%F;"
)
if not exist "!SNOWLUMA_ADAPTER_DIR!\main.py" set "SNOWLUMA_MISSING=!SNOWLUMA_MISSING! NachoBot-SnowLuma-Adapter/main.py;"
if not exist "!SNOWLUMA_ADAPTER_DIR!\pyproject.toml" set "SNOWLUMA_MISSING=!SNOWLUMA_MISSING! NachoBot-SnowLuma-Adapter/pyproject.toml;"
if defined SNOWLUMA_MISSING (
  echo [FATAL] SnowLuma components are incomplete: !SNOWLUMA_MISSING!
  echo [INFO] Redeploy from https://github.com/SnowLuma/SnowLuma/releases/latest
  endlocal & exit /b 1
)
uv run --project "%ROOT%NachoBot" python "%ROOT%webUI\snowluma_locator.py" --root "%ROOT%" --runtime-path "!SNOWLUMA_DIR!" --check-credentials >nul 2>&1
if errorlevel 1 (
  echo [FATAL] SnowLuma credentials are missing or inconsistent; redeploy SnowLuma before starting the Runtime or adapter.
  endlocal & exit /b 1
)
endlocal & set "SNOWLUMA_DIR=%SNOWLUMA_DIR%" & set "SNOWLUMA_NAME=%SNOWLUMA_NAME%" & exit /b 0

:START_SNOWLUMA_RUNTIME
setlocal EnableExtensions EnableDelayedExpansion
if not defined SNOWLUMA_DIR (
  for /f "usebackq delims=" %%D in (`uv run --project "%ROOT%NachoBot" python "%ROOT%webUI\snowluma_locator.py" --root "%ROOT%" --field path 2^>nul`) do if not defined SNOWLUMA_DIR set "SNOWLUMA_DIR=%%D"
)
if not defined SNOWLUMA_DIR (
  echo [FATAL] SnowLuma Runtime discovery failed; adapter startup aborted.
  echo [INFO] SnowLuma download: https://github.com/SnowLuma/SnowLuma/releases/latest
  endlocal & exit /b 1
)
set "SNOWLUMA_PORT=5099"
set "SNOWLUMA_HOST=127.0.0.1"
set "SNOWLUMA_ONEBOT_PORT=3001"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path '%SNOWLUMA_DIR%' 'config\runtime.json'; if (Test-Path -LiteralPath $p) { try { $j=Get-Content -Raw -LiteralPath $p | ConvertFrom-Json; $v=[int]$j.webuiPort; if ($v -ge 1 -and $v -le 65535) { $v } else { 5099 } } catch { 5099 } } else { 5099 }"`) do set "SNOWLUMA_PORT=%%P"
for /f "usebackq delims=" %%H in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path '%SNOWLUMA_DIR%' 'config\runtime.json'; $h='127.0.0.1'; if (Test-Path -LiteralPath $p) { try { $j=Get-Content -Raw -LiteralPath $p | ConvertFrom-Json; if ($null -ne $j.webuiHost -and -not [string]::IsNullOrWhiteSpace([string]$j.webuiHost)) { $h=([string]$j.webuiHost).Trim().ToLowerInvariant() } } catch { $h='__INVALID__' } }; if ($h -ne '127.0.0.1') { '__INVALID__' } else { $h }"`) do set "SNOWLUMA_HOST=%%H"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%ROOT%NachoBot-SnowLuma-Adapter\config.toml'; if (Test-Path -LiteralPath $p) { $c=Get-Content -Raw -LiteralPath $p; if ($c -match '(?ms)^\[snowluma\]\s*.*?^port\s*=\s*(\d+)') { $v=[int]$Matches[1]; if ($v -ge 1 -and $v -le 65535) { $v } else { 3001 } } else { 3001 } } else { 3001 }"`) do set "SNOWLUMA_ONEBOT_PORT=%%P"
if /i "!SNOWLUMA_HOST!"=="__INVALID__" (
  echo [FATAL] SnowLuma webuiHost must be a loopback address; refusing to start.
  endlocal & exit /b 1
)
if not exist "!SNOWLUMA_DIR!\index.mjs" (
  echo [FATAL] SnowLuma index.mjs not found: !SNOWLUMA_DIR!\index.mjs
  endlocal & exit /b 1
)
call :CHECK_SNOWLUMA_PORT_FREE "!SNOWLUMA_PORT!" "WebUI"
if errorlevel 1 (
  endlocal & exit /b 1
)
call :CHECK_SNOWLUMA_PORT_FREE "!SNOWLUMA_ONEBOT_PORT!" "OneBot"
if errorlevel 1 (
  endlocal & exit /b 1
)
echo --- Start SnowLuma Runtime via launcher.bat on port !SNOWLUMA_PORT!...
start "SnowLuma Runtime" /D "!SNOWLUMA_DIR!" /b cmd /d /s /c "call launcher.bat <nul"
set "SNOWLUMA_READY="
for /l %%I in (1,1,60) do (
  if not defined SNOWLUMA_READY (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$client=New-Object System.Net.Sockets.TcpClient; try { $client.Connect('127.0.0.1',!SNOWLUMA_PORT!); exit 0 } catch { exit 1 } finally { $client.Dispose() }" >nul 2>&1
    if not errorlevel 1 (
      set "SNOWLUMA_READY=1"
      echo [OK] SnowLuma Runtime :!SNOWLUMA_PORT! is ready.
    ) else (
      timeout /t 1 /nobreak >nul
    )
  )
)
if not defined SNOWLUMA_READY (
  echo [FATAL] SnowLuma Runtime :!SNOWLUMA_PORT! did not become ready within 60 seconds.
  endlocal & exit /b 1
)
endlocal & exit /b 0

:CHECK_SNOWLUMA_PORT_FREE
setlocal EnableExtensions
set "CHECK_PORT=%~1"
set "CHECK_LABEL=%~2"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$client=New-Object System.Net.Sockets.TcpClient; try { $client.Connect('127.0.0.1',%~1); exit 0 } catch { exit 1 } finally { $client.Dispose() }" >nul 2>&1
if not errorlevel 1 (
  echo [FATAL] SnowLuma %CHECK_LABEL% port :%CHECK_PORT% is already occupied; refusing to start bundled runtime.
  endlocal & exit /b 1
)
endlocal & exit /b 0

:READ_QQ_ADAPTER
set "QQ_ADAPTER="
for /f "usebackq delims=" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path '%ROOT%' 'NachoBot\.env'; if (!(Test-Path -LiteralPath $p)) { 'napcat'; exit 0 }; $values=@(); foreach($line in (Get-Content -LiteralPath $p)) { if($line -match '^\s*([^#=][^=]*?)\s*=\s*(.*?)\s*$' -and $Matches[1].Trim().ToLowerInvariant() -eq 'qq_adapter') { $values += $Matches[2].Trim() } }; if($values.Count -eq 0) { 'napcat'; exit 0 }; if($values.Count -gt 1) { exit 1 }; $value=$values[0].ToLowerInvariant(); if([string]::IsNullOrWhiteSpace($value) -or $value -notin @('napcat','snowluma')) { exit 1 }; $value"`) do set "QQ_ADAPTER=%%A"
if not defined QQ_ADAPTER exit /b 1
exit /b 0

:EXIT
if %FINAL_RC% NEQ 0 (
  echo Error occurred.
  pause
) else (
  echo All done.
)
endlocal & exit /b %FINAL_RC%
