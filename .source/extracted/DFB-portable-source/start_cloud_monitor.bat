@echo off
cd /d "%~dp0"
"%~dp0runtime\python.exe" -B monitor_cloud.py
if errorlevel 1 pause
