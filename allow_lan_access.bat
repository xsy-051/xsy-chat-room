@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0allow_lan_access.ps1"
if errorlevel 1 pause
