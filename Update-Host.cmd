@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update-Host.ps1"
if errorlevel 1 echo Update failed. Read the message above.
pause
