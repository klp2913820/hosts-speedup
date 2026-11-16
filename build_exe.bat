@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] 安装 / 更新 PyInstaller ...
pip install --upgrade pyinstaller || goto :fail

echo.
echo [2/2] 打包单文件 exe ...
pyinstaller --noconfirm --clean --onefile --noconsole ^
    --name HostsSpeedup ^
    hosts_speedup.py || goto :fail

echo.
echo 完成：dist\HostsSpeedup.exe
goto :eof

:fail
echo.
echo 打包失败，请检查上面的报错信息。
exit /b 1
