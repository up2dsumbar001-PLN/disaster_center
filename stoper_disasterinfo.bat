@echo off
rem Disaster Information Center - Stop
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\dcc-control.ps1" -Action stop
