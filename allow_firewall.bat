@echo off
chcp 65001 >nul
title 飞递 Feidi - 防火墙放行

echo.
echo =================== 飞递 Feidi ===================
echo.
echo  正在为 飞递 Feidi 添加 Windows 防火墙放行规则...
echo  如果弹出 UAC 确认框，请点「是」
echo.
echo ======================================================
echo.

:: 删除旧规则（避免重复）
netsh advfirewall firewall delete rule name="SimpleTransfer_HTTP" >nul 2>&1
netsh advfirewall firewall delete rule name="Feidi_HTTP" >nul 2>&1

:: 添加新规则：允许 TCP 9876 端口入站
netsh advfirewall firewall add rule name="Feidi_HTTP" dir=in action=allow protocol=TCP localport=9876

if %errorlevel% equ 0 (
    echo.
    echo [成功] 防火墙规则已添加！端口 9876 已放行。
    echo.
    echo 现在请重新启动 start.bat，然后用手机访问。
) else (
    echo.
    echo [失败] 无法自动添加规则，请手动操作：
    echo   1. 打开「Windows 安全中心」-「防火墙」
    echo   2. 「允许应用通过防火墙」
    echo   3. 找到 Python，勾选「专用」和「公用」
    echo.
)

echo.
pause
