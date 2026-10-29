@echo off
cd /d "%~dp0"
"%~dp0runtime\python.exe" -B monitor_channels.py
if errorlevel 1 pause
