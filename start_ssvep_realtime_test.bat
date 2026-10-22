@echo off
cd /d "%~dp0"
python run_ssvep_realtime_test.py %*
if errorlevel 1 pause
