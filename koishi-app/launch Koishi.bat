@echo off

cd /d "%~dp0"

echo 正在配置网络代理...
set HTTPS_PROXY=http://127.0.0.1:7897

echo 正在启动 Koishi...
corepack yarn start
