@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "ROOT=%~dp0"
set "ADAPTER_DIR=%ROOT%NachoBot-Douyin-Adapter"

if not exist "%ADAPTER_DIR%\config.toml" (
  echo [ERROR] 缺少配置文件。
  echo 请先复制 NachoBot-Douyin-Adapter\config.example.toml 为 config.toml 并填写配置。
  pause
  exit /b 1
)

where uv >nul 2>&1
if errorlevel 1 (
  echo [ERROR] 未找到 uv，请先安装 uv。
  pause
  exit /b 1
)

cd /d "%ADAPTER_DIR%"
uv sync --python ">=3.11,<3.14"
if errorlevel 1 (
  echo [ERROR] 抖音适配器依赖安装失败。
  pause
  exit /b 1
)

uv run python main.py
set "FINAL_RC=%ERRORLEVEL%"
pause
exit /b %FINAL_RC%
