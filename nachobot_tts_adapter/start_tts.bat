@echo off
chcp 65001 >nul
title TTS Launch
setlocal ENABLEDELAYEDEXPANSION

REM ===== 基本路径（如你改过目录，只需改这里）=====
set "BASE_DIR=C:\Users\BigSh0t\Nacho-with-u"
set "ADAPTER_DIR=%BASE_DIR%\nachobot_tts_adapter"
set "NAPCAT_SRC=%BASE_DIR%\NachoBot-Napcat-Adapter\src"
set "SOVITS_DIR=C:\Users\BigSh0t\GPT-SoVITS\GPT-SoVITS-v2pro-20250604"

REM ===== 端口（如你改过 SoVITS 端口，这里也要改）=====
set "PORT_SOVITS=9880"
set "PORT_ADAPTER=8070"
set "PORT_CONTROL=9872"

REM ===== 解释器路径 =====
set "PY_GPT=%SOVITS_DIR%\runtime\python.exe"
set "PY_ADAPTER=%ADAPTER_DIR%\.venv\Scripts\python.exe"

REM ★ 修改点：全局隔离用户site-packages + 不走代理（子窗口继承）
set "PYTHONNOUSERSITE=1"
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "NO_PROXY=127.0.0.1,localhost"

REM ===== 日志（仅安装初始化日志；运行日志看三个窗口）=====
set "LOG_DIR=%ADAPTER_DIR%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "SETUP_LOG=%LOG_DIR%\boot_setup.log"
echo ==== RUN %date% %time% ==== >> "%SETUP_LOG%"

REM ===== 选择系统 Python（创建 venv 时用）=====
set "PY_BOOT="
where py >nul 2>&1 && py -3.10 -V >nul 2>&1 && set "PY_BOOT=py -3.10"
if not defined PY_BOOT (
  where python >nul 2>&1 && python -V >nul 2>&1 && set "PY_BOOT=python"
)
if not defined PY_BOOT (
  echo [FATAL] 未找到可用的 Python（py -3.10 / python）。>> "%SETUP_LOG%"
  echo [FATAL] 未找到可用的 Python（请安装 Python 3.10+ 后重试）。
  pause & exit /b 1
)

REM ===== 自动创建/修复 venv 并安装依赖 =====
if not exist "%PY_ADAPTER%" (
  echo [INFO] 创建 venv ... >> "%SETUP_LOG%"
  cd /d "%ADAPTER_DIR%"
  %PY_BOOT% -m venv .venv >> "%SETUP_LOG%" 2>&1
  if errorlevel 1 goto :FAIL
) else (
  echo [OK] venv 存在：%PY_ADAPTER% >> "%SETUP_LOG%"
)

echo [INFO] 升级 pip ... >> "%SETUP_LOG%"
"%PY_ADAPTER%" -s -m pip install --upgrade pip >> "%SETUP_LOG%" 2>&1

echo [INFO] 安装/补全依赖 ... >> "%SETUP_LOG%"
"%PY_ADAPTER%" -s -m pip install fastapi uvicorn requests toml pydantic loguru websockets aiohttp >> "%SETUP_LOG%" 2>&1

REM ===== 选择 SoVITS 启动文件 =====
set "API_FILE=%SOVITS_DIR%\api_v2.py"
if not exist "%API_FILE%" set "API_FILE=%SOVITS_DIR%\api.py"
if not exist "%API_FILE%" (
  echo [FATAL] 未找到 SoVITS 启动文件 api_v2.py / api.py >> "%SETUP_LOG%"
  echo [FATAL] SoVITS 启动文件缺失，请检查 %SOVITS_DIR%
  pause & exit /b 1
)

echo.
echo ========== 启动 SoVITS / Adapter / Control ==========
echo.

REM ===== 窗口 1：SoVITS（可见，实时日志）=====
REM ★ 修改点：python 增加 -s；继承了 PYTHONNOUSERSITE/代理清空
start "SoVITS API (%PORT_SOVITS%)" cmd /k ^
  "chcp 65001>nul && set PYTHONPATH=%SOVITS_DIR%;%SOVITS_DIR%\GPT_SoVITS && cd /d %SOVITS_DIR% && echo [CMD] %PY_GPT% -s %API_FILE% --port %PORT_SOVITS% && %PY_GPT% -s %API_FILE% --port %PORT_SOVITS%"

REM ===== 等待 SoVITS 端口 LISTEN（最多 60 秒）=====
set "READY="
for /l %%I in (1,1,60) do (
  for /f "tokens=1-5" %%a in ('netstat -ano ^| findstr /r /c:":%PORT_SOVITS% " ^| findstr /i LISTENING') do set "READY=1"
  if defined READY goto :SOVITS_READY
  timeout /t 1 /nobreak >nul
)
echo [ERROR] SoVITS %PORT_SOVITS% 60 秒内未监听，查看 SoVITS 窗口日志或端口占用。
pause & exit /b 1

:SOVITS_READY
echo [OK] SoVITS 已监听 %PORT_SOVITS%，进行 HTTP 健康检查 ...

REM ★ 修改点：健康检查 /openapi.json（无该路由则跳过）
set "HC_OK="
for /l %%I in (1,1,10) do (
  powershell -Command "try{Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:%PORT_SOVITS%/openapi.json -TimeoutSec 1|Out-Null; exit 0}catch{exit 1}"
  if !errorlevel! EQU 0 ( set "HC_OK=1" & goto :HC_PASS )
  timeout /t 1 >nul
)
:HC_PASS
if defined HC_OK (
  echo [OK] SoVITS HTTP 就绪。
) else (
  echo [WARN] SoVITS 未暴露 /openapi.json，略过 HTTP 健康检查。
)

echo [OK] SoVITS 已就绪，启动 Adapter ...

REM ===== 窗口 2：Adapter（可见，强制使用 venv；PYTHONPATH 指向 本项目+Napcat 源码）=====
REM ★ 修改点：python 增加 -s；继承隔离环境与代理
start "TTS Adapter (%PORT_ADAPTER%)" cmd /k ^
  "chcp 65001>nul && set PYTHONPATH=%ADAPTER_DIR%;%ADAPTER_DIR%\src;%NAPCAT_SRC% && cd /d %ADAPTER_DIR% && echo [CMD] %PY_ADAPTER% -s main.py && %PY_ADAPTER% -s main.py"

REM ===== 等待 Adapter 端口（最多 30 秒，不通过也继续）=====
set "READY="
for /l %%I in (1,1,30) do (
  for /f "tokens=1-5" %%a in ('netstat -ano ^| findstr /r /c:":%PORT_ADAPTER% " ^| findstr /i LISTENING') do set "READY=1"
  if defined READY goto :ADAPTER_READY
  timeout /t 1 /nobreak >nul
)
echo [WARN] Adapter %PORT_ADAPTER% 未在 30 秒内监听，请看 Adapter 窗口。>> "%SETUP_LOG%"

:ADAPTER_READY
echo [OK] Adapter 阶段完成，启动 Control ...

REM ===== 窗口 3：Control（可见，同样使用 venv + PYTHONPATH）=====
REM ★ 修改点：python 增加 -s；继承隔离环境与代理
start "Control API (%PORT_CONTROL%)" cmd /k ^
  "chcp 65001>nul && set PYTHONPATH=%ADAPTER_DIR%;%ADAPTER_DIR%\src;%NAPCAT_SRC% && cd /d %ADAPTER_DIR% && echo [CMD] %PY_ADAPTER% -s -m plugins.GPT_Sovits.api_server && %PY_ADAPTER% -s -m plugins.GPT_Sovits.api_server"

echo.
echo ✅ 已打开三个实时日志窗口：
echo  - SoVITS API        : http://127.0.0.1:%PORT_SOVITS%
echo  - TTS Adapter (WS)  : ws://127.0.0.1:%PORT_ADAPTER%
echo  - Control API       : http://127.0.0.1:%PORT_CONTROL%
echo.
goto :END

:FAIL
echo ❌ 初始化失败，查看日志：%SETUP_LOG%
powershell -NoProfile -Command "Get-Content -LiteralPath '%SETUP_LOG%' -Tail 40"
echo.
pause

:END
endlocal
