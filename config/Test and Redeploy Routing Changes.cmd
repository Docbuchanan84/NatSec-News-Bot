@echo off
setlocal
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File ".\ops\apply-routing-changes.ps1"
echo.
echo Test and redeploy command finished. Press any key to exit.
pause >nul
