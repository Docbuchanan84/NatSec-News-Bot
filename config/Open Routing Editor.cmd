@echo off
setlocal
cd /d "%~dp0.."
echo This opens the legacy tag/concept routing editor for config/routing.
echo Weighted V2 routing uses config/routing_v2 and Discord commands such as /rss rule-help.
echo.
python -m app.routing_editor wizard
echo.
echo Routing editor closed. Press any key to exit.
pause >nul
