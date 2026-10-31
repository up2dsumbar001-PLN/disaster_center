@echo off
rem GRIDCAST - Viewer
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\dcc-control.ps1" -Action viewer
