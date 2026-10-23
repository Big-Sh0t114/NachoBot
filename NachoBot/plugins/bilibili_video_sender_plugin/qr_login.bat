@echo off
setlocal
pushd "%~dp0..\.." || (
    echo [ERROR] Cannot access NachoBot root.
    pause
    exit /b 1
)
where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv is not available on PATH.
    popd
    pause
    exit /b 1
)
uv run --with "qrcode[pil]" python "%~dp0qr_login.py" --config "%~dp0config.toml" --qr-output "%~dp0qr_login.png"
set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] QR login exited with code %EXIT_CODE%.
) else (
    echo [OK] QR login finished.
)
pause
exit /b %EXIT_CODE%
