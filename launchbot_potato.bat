@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONPATH="
set "PYTHONHOME="
title Launch NachoBot Potato (No Local Models)
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

if /i "%QQ_ADAPTER%"=="snowluma" (
  call :VERIFY_SNOWLUMA_COMPONENTS
  if errorlevel 1 (
    set "FINAL_RC=1"
    goto :EXIT
  )
)

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

echo --- Syncing %QQ_ADAPTER% Adapter...
if not exist "%ADAPTER_DIR%\." (
  set "FINAL_RC=1"
  echo [FATAL] %QQ_ADAPTER% adapter setup failed: project directory not found.
  goto :EXIT
)
if not exist "%ADAPTER_DIR%\pyproject.toml" (
  set "FINAL_RC=1"
  echo [FATAL] %QQ_ADAPTER% adapter setup failed: pyproject.toml not found.
  goto :EXIT
)
cd /d "%ADAPTER_DIR%"
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] %QQ_ADAPTER% adapter setup failed: project directory could not be entered.
  goto :EXIT
)
uv sync --python ">=3.11,<=3.13"
if errorlevel 1 (
  set "FINAL_RC=1"
  echo [FATAL] %QQ_ADAPTER% adapter dependency sync failed.
  goto :EXIT
)

if /i "%QQ_ADAPTER%"=="snowluma" (
  call :START_SNOWLUMA_RUNTIME
  if errorlevel 1 (
    set "FINAL_RC=1"
    echo [FATAL] SnowLuma Runtime did not become ready; adapter startup aborted.
    goto :EXIT
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
echo Startup complete. Relay :%PORT_ADAPTER% is running in pure relay mode; no local model service was started.

:EXIT
if %FINAL_RC% NEQ 0 (
  echo Error occurred.
  pause
) else (
  echo All done.
)
endlocal & exit /b %FINAL_RC%

:VERIFY_SNOWLUMA_COMPONENTS
setlocal EnableExtensions EnableDelayedExpansion
set "SNOWLUMA_DIR="
for /f "usebackq delims=" %%D in (`uv run --project "%ROOT%NachoBot" python "%ROOT%webUI\snowluma_locator.py" --root "%ROOT%" --field path 2^>nul`) do if not defined SNOWLUMA_DIR set "SNOWLUMA_DIR=%%D"
if not defined SNOWLUMA_DIR (
  echo [FATAL] SnowLuma Runtime discovery failed; deploy exactly one valid SnowLuma 1.14.x directory.
  echo [INFO] SnowLuma download: https://github.com/SnowLuma/SnowLuma/releases/latest
  endlocal & exit /b 1
)
for %%D in ("!SNOWLUMA_DIR!") do set "SNOWLUMA_NAME=%%~nxD"
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
endlocal & exit /b 0

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
