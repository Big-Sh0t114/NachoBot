@echo off
setlocal

set "BASE_DIR=%~dp0"

rem ---- Start Koishi in a new window ----
echo Starting Koishi...
start "Koishi" cmd /k "cd /d ""%BASE_DIR%koishi-app"" && set HTTPS_PROXY=http://127.0.0.1:7897 && set HTTP_PROXY=http://127.0.0.1:7897 && npm start"

rem ---- Wait for Koishi to bring up server-onebot ----
timeout /t 5 /nobreak >nul

rem ---- Start NachoBot-Koishi-Adapter in a new window ----
echo Starting NachoBot-Koishi-Adapter...
start "NachoBot-Koishi-Adapter" cmd /k "cd /d ""%BASE_DIR%"" && python NachoBot-Koishi-Adapter\main.py"

rem ---- Start NachoBot-DiscordVC-Adapter in a new window ----
echo Starting NachoBot-DiscordVC-Adapter...
start "NachoBot-DiscordVC-Adapter" cmd /k "cd /d ""%BASE_DIR%"" && python NachoBot-DiscordVC-Adapter\main.py"

endlocal
