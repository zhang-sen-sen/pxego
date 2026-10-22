@echo off
rem PXEGo 安装脚本：把解压内容安装到用户目录并启动启动器
setlocal
set DEST=%USERPROFILE%\PXEGo
if not exist "%DEST%" mkdir "%DEST%"
echo 正在安装 PXEGo 到 %DEST% ...
xcopy /E /I /Y "%~dp0." "%DEST%\" >nul
echo 安装完成。
cd /d "%DEST%"
start "" "%DEST%\launcher.cmd"
