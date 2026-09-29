@echo off
rem Guided button setup: press an input for each kneeboard action.
cd /d "%~dp0"
kneeboard.exe --bind
echo.
pause
