@echo off
setlocal EnableExtensions
chcp 65001 >nul
title NachoBot 本机 AI 主播控制台

set "ROOT=%~dp0"
set "ADAPTER_DIR=%ROOT%NachoBot-Local-Host-Adapter"
set "LOCAL_TTS_LAUNCHER=%ROOT%launch_local_neural_tts.ps1"

if not exist "%ADAPTER_DIR%\config.toml" (
  copy /y "%ADAPTER_DIR%\config.example.toml" "%ADAPTER_DIR%\config.toml" >nul
  echo [INFO] 已创建 config.toml。请按需修改 TTS、Live2D 与字幕路径后重新启动。
)

where uv >nul 2>&1
if errorlevel 1 (
  echo [ERROR] 未找到 uv。请先安装 uv 后重试。
  pause
  exit /b 1
)

cd /d "%ADAPTER_DIR%"
uv sync --python ">=3.11,<3.14"
if errorlevel 1 (
  echo [ERROR] 本机 AI 主播控制台依赖安装失败。
  pause
  exit /b 1
)

if exist "%LOCAL_TTS_LAUNCHER%" (
  echo [INFO] 正在提交本机 VoxCPM2 语音服务启动…
  rem 优先使用 PowerShell 7，避免 Windows PowerShell 5 对 UTF-8 中文脚本的编码解析错误。
  where pwsh >nul 2>&1
  if not errorlevel 1 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%LOCAL_TTS_LAUNCHER%"
  ) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCAL_TTS_LAUNCHER%"
  )
  if errorlevel 1 (
    echo [ERROR] 本机 VoxCPM2 未能就绪。为避免播放机械浏览器语音，本次不启动主播控制台。
    pause
    exit /b 1
  )
)

start "" "http://127.0.0.1:8789"
uv run python main.py
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%
