@echo off
chcp 65001 >nul
title 🚀 一键启动 MaiM TTS 全链路 (GPT-SoVITS API + TTS适配器 + 控制API)

REM =========================
REM 可配置区域（按需修改）
REM =========================
set "GPT_SOVITS_DIR=C:\Users\BigSh0t\GPT-SoVITS\GPT-SoVITS-v2pro-20250604"
set "ADAPTER_DIR=C:\Users\BigSh0t\MaiM-with-u\maimbot_tts_adapter"

REM 选择后端脚本：优先 api_v2.py；若不存在，则用 api.py
set "API_V2_FILE=%GPT_SOVITS_DIR%\api_v2.py"
set "API_FILE=%GPT_SOVITS_DIR%\api.py"

REM 端口定义
set "PORT_SOVITS=9880"     REM GPT-SoVITS 推理 API 端口（与 gpt-sovits.toml 保持一致）
set "PORT_ADAPTER=8070"    REM TTS 适配器主程序端口（Napcat 就连这个）
set "PORT_CONTROL=9872"    REM 你的控制/管理接口端口（api_server.py）

REM Python
set "PY_GPT=%GPT_SOVITS_DIR%\runtime\python.exe"   REM 用官方封装的 runtime Python
set "PY_SYS=python"                                REM 系统 Python（跑适配器）

REM =========================
REM 工具函数
REM =========================
REM 检查端口是否在LISTEN，返回 ERRORLEVEL=0 表示已监听；=1 表示未监听
:check_port
  setlocal ENABLEDELAYEDEXPANSION
  set "_port=%~1"
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":!_port! " ^| findstr /i LISTENING') do (
    endlocal & exit /b 0
  )
  endlocal & exit /b 1

REM 等待端口起来（最多等待N秒）
:wait_for_port
  setlocal ENABLEDELAYEDEXPANSION
  set "_port=%~1"
  set /a "_timeout=%~2"
  if "!_timeout!"=="" set /a _timeout=30
  set /a "_elapsed=0"
  :_wait_loop
    call :check_port "!_port!"
    if %ERRORLEVEL%==0 ( endlocal & exit /b 0 )
    if !_elapsed! GEQ !_timeout! (
      echo ❌ 端口 !_port! 在 !_timeout! 秒内没有监听
      endlocal & exit /b 1
    )
    timeout /t 1 /nobreak >nul
    set /a "_elapsed+=1"
    goto :_wait_loop

REM HTTP 探活（使用 PowerShell），返回 0 表示成功
:http_probe
  setlocal
  set "_url=%~1"
  powershell -NoProfile -Command ^
    "try{ $r=Invoke-WebRequest -Uri '%_url%' -UseBasicParsing -TimeoutSec 5; if($r.StatusCode -ge 200 -and $r.StatusCode -lt 500){exit 0}else{exit 1} }catch{ exit 1 }"
  set "_rc=%ERRORLEVEL%"
  endlocal & exit /b %_rc%

REM 启动一个新窗口并保持
:start_window
  setlocal
  set "TITLE=%~1"
  set "CMDLINE=%~2"
  start "%TITLE%" cmd /k %CMDLINE%
  endlocal & exit /b 0

echo.
echo =============================================
echo  路径:
echo    GPT-SoVITS   = %GPT_SOVITS_DIR%
echo    TTS适配器    = %ADAPTER_DIR%
echo  端口:
echo    SoVITS API   = %PORT_SOVITS%
echo    TTS适配器    = %PORT_ADAPTER%
echo    控制API      = %PORT_CONTROL%
echo =============================================
echo.

REM =========================
REM 1) 启动 GPT-SoVITS 推理 API
REM =========================
call :check_port %PORT_SOVITS%
if %ERRORLEVEL%==0 (
  echo ✅ 检测到 GPT-SoVITS API 已在端口 %PORT_SOVITS% 监听，跳过启动
) else (
  if exist "%API_V2_FILE%" (
    echo [1/3] 启动 GPT-SoVITS API (api_v2.py) 端口 %PORT_SOVITS% ...
    call :start_window "GPT-SoVITS API" "\"%PY_GPT%\" -I \"%API_V2_FILE%\" --port %PORT_SOVITS%"
  ) else if exist "%API_FILE%" (
    echo [1/3] 启动 GPT-SoVITS API (api.py) 端口 %PORT_SOVITS% ...
    call :start_window "GPT-SoVITS API" "\"%PY_GPT%\" -I \"%API_FILE%\" --port %PORT_SOVITS%"
  ) else (
    echo ❌ 未找到 %API_V2_FILE% 或 %API_FILE% ；请检查路径
    pause
    exit /b 1
  )
  call :wait_for_port %PORT_SOVITS% 30 || (echo 👉 请检查端口占用或启动失败 & pause & exit /b 1)
  call :http_probe http://127.0.0.1:%PORT_SOVITS%/docs && (
    echo ✅ SoVITS API /docs 探活成功
  ) || (
    echo ⚠️ SoVITS API 未响应 /docs（可能该版本未带 Swagger，但端口已监听）
  )
)

REM =========================
REM 2) 启动 TTS 适配器主程序（Napcat 连接的对象）
REM =========================
call :check_port %PORT_ADAPTER%
if %ERRORLEVEL%==0 (
  echo ✅ 检测到 TTS 适配器已在端口 %PORT_ADAPTER% 监听，跳过启动
) else (
  echo [2/3] 启动 TTS 适配器主程序 (main.py) 端口 %PORT_ADAPTER% ...
  pushd "%ADAPTER_DIR%"
  call :start_window "TTS Adapter" "%PY_SYS% main.py"
  popd
  call :wait_for_port %PORT_ADAPTER% 20 || (echo 👉 TTS 适配器未在 %PORT_ADAPTER% 监听，检查 main.py 日志 & pause & exit /b 1)
  echo ✅ TTS 适配器端口 %PORT_ADAPTER% 已监听
)

REM =========================
REM 3) 启动 控制/管理 API（你的 api_server.py）
REM =========================
call :check_port %PORT_CONTROL%
if %ERRORLEVEL%==0 (
  echo ✅ 检测到 控制API 已在端口 %PORT_CONTROL% 监听，跳过启动
) else (
  echo [3/3] 启动 控制API (api_server.py) 端口 %PORT_CONTROL% ...
  set "PYTHONPATH=%ADAPTER_DIR%\src"
  pushd "%ADAPTER_DIR%"
  call :start_window "TTS Control API" "%PY_SYS% -m plugins.GPT_Sovits.api_server"
  popd
  call :wait_for_port %PORT_CONTROL% 15 || (echo 👉 控制API未在 %PORT_CONTROL% 监听，检查 api_server 日志 & pause & exit /b 1)
  echo ✅ 控制API端口 %PORT_CONTROL% 已监听
)

echo.
echo =============================================
echo ✅ 全链路已就绪：
echo    %PORT_SOVITS% → GPT-SoVITS 推理 API
echo    %PORT_ADAPTER% → TTS 适配器主程序（Napcat 连接这里）
echo    %PORT_CONTROL% → 控制/管理 API（/load_model、/infer）
echo =============================================
echo.
echo ❗ 提示：请把 Napcat 指向 ws://127.0.0.1:%PORT_ADAPTER%
echo.
pause
