@echo off
REM Starts the manager dashboard, then open:
REM     http://127.0.0.1:5000
cd /d "%~dp0"
py dashboard.py
pause
