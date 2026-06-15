@echo off
setlocal
cd /d "%~dp0.."
python -m app.routing_editor wizard
echo.
echo Routing editor closed. Press any key to exit.
pause >nul
