@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 静默测速并更新 hosts（需要以管理员身份运行，或由计划任务调用）
python "%~dp0hosts_speedup.py" --update
echo.
echo 退出码: %errorlevel%
