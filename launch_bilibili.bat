@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Launch NachoBot Bilibili
set "PYTHONNOUSERSITE=1"

set "ROOT=%~dp0"
set "NACHOBOT_FFMPEG_DIR=%ROOT%.runtime\ffmpeg"
set "NACHOBOT_DIR=%ROOT%NachoBot"
set "BILIBILI_DIR=%ROOT%NachoBot-Bilibili-Adapter"
set "LIVE2D_DIR=%ROOT%NachoBot-Live2D-Adapter"
set "LIVE2D_LAUNCHER=%LIVE2D_DIR%\launch_live2d.bat"
set "LIVE2D_HOST=127.0.0.1"
set "LIVE2D_PORT=8766"

REM ===== validate directories =====
if not exist "%NACHOBOT_DIR%\" (
  echo [ERROR] NachoBot directory not found: %NACHOBOT_DIR%
  pause
  exit /b 1
)
if not exist "%BILIBILI_DIR%\" (
  echo [ERROR] Bilibili Adapter directory not found: %BILIBILI_DIR%
  pause
  exit /b 1
)
REM Live2D is opt-in: only an explicit true enables connection/autostart.
set "LIVE2D_ENABLED=0"
findstr /i /r /c:"^[ ]*enable_live2D[ ]*=[ ]*true" "%BILIBILI_DIR%\config.toml" >nul
if not errorlevel 1 set "LIVE2D_ENABLED=1"

if "%LIVE2D_ENABLED%"=="1" (
  if not exist "%LIVE2D_DIR%\" (
    echo [ERROR] Live2D Adapter directory not found: %LIVE2D_DIR%
    pause
    exit /b 1
  )
  if not exist "%LIVE2D_LAUNCHER%" (
    echo [ERROR] Live2D launcher not found: %LIVE2D_LAUNCHER%
    pause
    exit /b 1
  )
)

REM ===== check and install uv =====
where uv >nul 2>&1
if errorlevel 1 (
  echo [INFO] uv not found, auto installing...
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  if errorlevel 1 (
    echo [ERROR] uv install failed. Please install manually using: pip install uv
    pause
    exit /b 1
  )
  set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
  where uv >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] uv installed but not found in PATH. Please restart terminal.
    pause
    exit /b 1
  )
  echo [OK] uv installed.
)

REM ===== sync dependencies =====
echo [SYNC] NachoBot ...
cd /d "%NACHOBOT_DIR%"
uv sync
if errorlevel 1 (
  echo [ERROR] NachoBot uv sync failed.
  pause
  exit /b 1
)

echo [PLAYWRIGHT] Checking Chromium...
uv run python scripts\ensure_playwright.py
if errorlevel 1 (
  echo [WARN] Playwright Chromium preparation failed; web search will use HTTP fallback.
)

if not exist "%ROOT%NachoBot\ensure_ffmpeg.py" (
  echo [ERROR] FFmpeg preparation script not found: %ROOT%NachoBot\ensure_ffmpeg.py
  pause
  exit /b 1
)

echo [FFMPEG] Checking shared FFmpeg binaries...
uv run python "%ROOT%NachoBot\ensure_ffmpeg.py"
if errorlevel 1 (
  echo [ERROR] Shared FFmpeg download or verification failed.
  pause
  exit /b 1
)

if "%LIVE2D_ENABLED%"=="1" (
  echo [INFO] Live2D dependencies will be synced by launch_live2d.bat.
)

echo [SYNC] NachoBot-Bilibili-Adapter ...
cd /d "%BILIBILI_DIR%"
uv sync
if errorlevel 1 (
  echo [ERROR] Bilibili Adapter uv sync failed.
  pause
  exit /b 1
)

REM ===== reuse or start standalone Live2D Adapter =====
echo.
if "%LIVE2D_ENABLED%"=="0" (
  echo [INFO] Live2D is disabled in %BILIBILI_DIR%\config.toml.
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $client=New-Object System.Net.Sockets.TcpClient; $client.Connect('%LIVE2D_HOST%',%LIVE2D_PORT%); $client.Close(); exit 0 } catch { exit 1 }"
  if not errorlevel 1 (
    echo [OK] Existing Live2D Adapter detected at %LIVE2D_HOST%:%LIVE2D_PORT%.
  ) else (
    echo [START] NachoBot-Live2D-Adapter ...
    start "NachoBot-Live2D" /D "%LIVE2D_DIR%" "%ComSpec%" /k call "%LIVE2D_LAUNCHER%"

  echo [WAIT] Waiting for Live2D Adapter at %LIVE2D_HOST%:%LIVE2D_PORT% ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddSeconds(30); do { try { $client=New-Object System.Net.Sockets.TcpClient; $client.Connect('%LIVE2D_HOST%',%LIVE2D_PORT%); $client.Close(); exit 0 } catch { Start-Sleep -Milliseconds 500 } } while ((Get-Date) -lt $deadline); exit 1"
  if errorlevel 1 (
    echo [WARN] Live2D Adapter did not become ready within 30 seconds.
    echo [WARN] Bilibili Adapter will still start and retry the WebSocket connection automatically.
  ) else (
    echo [OK] Live2D Adapter is listening.
  )
  )
)

REM ===== start Bilibili Adapter =====
echo.
echo [START] NachoBot-Bilibili-Adapter ...
set "PYTHONPATH=%NACHOBOT_DIR%;%BILIBILI_DIR%"
start "NachoBot-Bilibili" /D "%BILIBILI_DIR%" "%ComSpec%" /k uv run python main.py

echo.
echo [DONE] Launch sequence complete.
echo.
endlocal
