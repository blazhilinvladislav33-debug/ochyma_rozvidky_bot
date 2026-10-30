@echo off
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0export_host.py"
) else (
    py -3 "%~dp0export_host.py"
)
if errorlevel 1 echo ERROR: Bundle creation failed. Check the message above.
pause
