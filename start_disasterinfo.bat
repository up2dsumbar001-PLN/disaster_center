@echo off
rem Disaster Information Center - Start
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\dcc-control.ps1" -Action start
