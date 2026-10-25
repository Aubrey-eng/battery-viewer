@echo off
rem 用 pythonw 启动，不弹黑窗；带 --show 会先打开面板，关掉后缩到托盘
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%~dp0app.py" --show
) else (
    start "" python.exe "%~dp0app.py" --show
)
