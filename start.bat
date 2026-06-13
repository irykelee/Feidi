@echo off
title 飞递 Feidi
cd /d "%~dp0"

echo.
echo =================== 飞递 Feidi ===================
echo.
echo  正在启动传输服务...
echo.

python "%~dp0transfer.py"
if %errorlevel% equ 0 goto :end

python3 "%~dp0transfer.py"
if %errorlevel% equ 0 goto :end

py "%~dp0transfer.py"
if %errorlevel% equ 0 goto :end

echo.
echo [错误] 未找到可用的 Python，请先安装 Python 3.7+
echo 下载地址: https://www.python.org/downloads/

:end
echo.
echo 飞递 Feidi 已停止。
pause
