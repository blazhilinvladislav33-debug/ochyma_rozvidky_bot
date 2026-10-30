@echo off
chcp 65001 >nul
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo First run Install-Host.cmd, then stop the bot using Host-Control.cmd option 3.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Dependency installation failed. Check your internet connection.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0reader_setup.py"
pause
