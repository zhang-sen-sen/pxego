@echo off
rem ============================================================
rem  PXE 网络引导服务器启动器（自动请求管理员权限）
rem  优先使用 EXE 版本；不存在时回退 PowerShell 版本
rem  DHCP(67)/TFTP(69) 端口需要管理员权限，双击本文件即可
rem ============================================================
cd /d "%~dp0"

if exist "%~dp0PXEGO-Launcher.exe" (
    start "" "%~dp0PXEGO-Launcher.exe"
    exit /b
)

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 需要管理员权限，正在请求提升...
    powershell -NoProfile -Command "Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%~dp0launcher.ps1\"' -Verb RunAs"
    exit /b
)

start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0launcher.ps1"