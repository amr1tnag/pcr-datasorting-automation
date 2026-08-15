@echo off
REM Starts the Photo Circle sorter. Leave this window open.
REM It checks the drop folder every minute and keeps going if
REM something goes wrong. Closing the window stops it safely.
cd /d "%~dp0"
py run.py
REM If it stops unexpectedly the window stays open so you can read why.
pause
