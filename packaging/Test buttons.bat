@echo off
rem Shows the name of every button and hat you press. Ctrl+C to stop.
cd /d "%~dp0"
kneeboard.exe --learn
echo.
pause
