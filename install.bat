@echo off
REM One-time setup: installs the Python packages the tool needs.
cd /d "%~dp0"
py -m pip install -r requirements.txt
pause
