@echo off
setlocal
cd /d "%~dp0"

if not exist "service.toml" (
    echo Missing service.toml. Copy service.example.toml and set a private server.token first.
    exit /b 2
)

if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv\Scripts\python.exe. Create the UniversalVC environment before launching the service.
    exit /b 3
)

".venv\Scripts\python.exe" service_main.py --config service.toml
endlocal
