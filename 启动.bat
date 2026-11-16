@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先用 pythonw 启动（无控制台窗口）；找不到就退回 python
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0hosts_speedup.py"
) else (
    python "%~dp0hosts_speedup.py"
)
